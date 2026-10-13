"""Static data, deploy and history."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from coop_sqlsc.cli import main
from coop_sqlsc.data import DataColumn, identity_off, render
from coop_sqlsc.objects import ObjectKey, batches, normalize

from .conftest import SAMPLE, connect, run_script
from .test_integration import link, sqlsc
from .test_phase2 import edit, git

# --- units ----------------------------------------------------------------------------------


def test_data_script_writes_control_characters_and_quotes_safely():
    key = ObjectKey("data", "dbo", "Note")
    columns = [
        DataColumn("Id", "int", "int", True, "num"),
        DataColumn("Text", "nvarchar", "nvarchar(50)", False, "nstr"),
    ]
    text = render(key, columns, ["Id"], [(1, "it's\r\nok  "), (2, None)])
    assert "(1, N'it''s' + NCHAR(13) + NCHAR(10) + N'ok  ')" in text
    assert "(2, NULL)" in text
    assert normalize(text) == text  # nothing the repo's normalizing could change
    assert identity_off(text) == "SET IDENTITY_INSERT dbo.Note OFF"
    assert len(batches(text)) == 3


def test_empty_table_data_is_a_delete():
    text = render(ObjectKey("data", "dbo", "Empty"), [DataColumn("Id", "int", "int", False, "num")], ["Id"], [])
    assert "DELETE FROM dbo.Empty;" in text and identity_off(text) is None


# --- against a live SQL Server ----------------------------------------------------------------

LOOKUPS = """
CREATE TABLE sales.Status (Code char(1) NOT NULL CONSTRAINT PK_Status PRIMARY KEY, Label nvarchar(20) NOT NULL);
GO
CREATE TABLE sales.Reason
(
    ReasonID int IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Reason PRIMARY KEY
   ,Code char(1) NOT NULL CONSTRAINT FK_Reason_Status REFERENCES sales.Status (Code)
   ,Reason nvarchar(100) NULL
);
GO
INSERT sales.Status (Code, Label) VALUES ('A', N'Active'), ('I', N'Inactive');
INSERT sales.Reason (Code, Reason) VALUES ('A', N'Signed up'), ('I', N'Moved ''away'''), ('I', NULL);
"""


def add_target(config, name: str, server: str, database: str) -> None:
    config.write_text(
        config.read_text() + f"  {name}:\n    kind: sql_server\n    server: '{server}'\n    database: {database}\n"
        "    auth: sql\n    user: sa\n    trust_server_certificate: true\n"
    )


def rows(server: str, database: str, sql: str) -> list[tuple]:
    return [tuple(r) for r in connect(server, database).cursor().execute(sql).fetchall()]


@pytest.mark.integration
def test_static_data_tracks_commits_and_rebuilds(tmp_path, server, make_db):
    db = make_db(SAMPLE.read_text() + "\nGO\n" + LOOKUPS)
    config = link(tmp_path, server, db)
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "dev@example.com")
    git(tmp_path, "config", "user.name", "Dev")
    sqlsc(config, "commit", "-m", "Schema")

    added = sqlsc(config, "data", "add", "sales.Status", "Reason")
    assert {(t["object"], t["rows"]) for t in added["tracked"]} == {("[sales].[Status]", 2), ("[sales].[Reason]", 3)}
    assert (tmp_path / "database" / "Data" / "sales.Status.sql").is_file()
    assert sqlsc(config, "status")["changes"] == []  # the files match the rows
    sqlsc(config, "commit", "-m", "Track lookups")
    assert sqlsc(config, "status")["changes"] == []

    # A row changed in the database (case only) shows up and commits.
    run_script(connect(server, db), "UPDATE sales.Status SET Label = N'ACTIVE' WHERE Code = 'A'")
    changes = sqlsc(config, "status")["changes"]
    assert [(c["type"], c["object"], c["action"]) for c in changes] == [("data", "[sales].[Status]", "changed")]
    assert "N'ACTIVE'" in sqlsc(config, "diff", "sales.Status")["changes"][0]["diff"]
    sqlsc(config, "commit", "-m", "Shout")
    assert "N'ACTIVE'" in (tmp_path / "database" / "Data" / "sales.Status.sql").read_text()

    # An empty database gets the tables and the rows; Reason sorts before Status
    # but refers to it, so its MERGE has to wait for Status's.
    fresh = make_db()
    add_target(config, "fresh", server, fresh)
    built = sqlsc(config, "get-latest", "-t", "fresh")
    assert built["ok"] and built["failed"] == [] and built["still_different"] == []
    assert rows(server, fresh, "SELECT Code, Label FROM sales.Status ORDER BY Code") == [
        ("A", "ACTIVE"),
        ("I", "Inactive"),
    ]
    assert rows(server, fresh, "SELECT ReasonID, Reason FROM sales.Reason ORDER BY ReasonID") == [
        (1, "Signed up"),
        (2, "Moved 'away'"),
        (3, None),
    ]

    # Rows edited in the repo reach the database: one updated, one added, one removed.
    reasons = tmp_path / "database" / "Data" / "sales.Reason.sql"
    edit(reasons, "N'Signed up'", "N'Joined'")
    edit(reasons, "(3, 'I', NULL)", "(4, 'A', N'Came back')")
    plan = sqlsc(config, "get-latest", "--dry-run")
    assert [(s["action"], s["object"]) for s in plan["steps"]] == [("data", "[sales].[Reason]")]
    applied = sqlsc(config, "get-latest")
    assert applied["ok"] and applied["still_different"] == []
    assert rows(server, db, "SELECT ReasonID, Reason FROM sales.Reason ORDER BY ReasonID") == [
        (1, "Joined"),
        (2, "Moved 'away'"),
        (4, "Came back"),
    ]

    removed = sqlsc(config, "data", "remove", "sales.Reason")
    assert removed["removed"][0]["object"] == "[sales].[Reason]" and not reasons.exists()


@pytest.mark.integration
def test_data_add_needs_a_key(tmp_path, server, make_db):
    db = make_db("CREATE TABLE dbo.Heap (Id int NOT NULL, Name nvarchar(10) NULL)")
    config = link(tmp_path, server, db)
    result = CliRunner().invoke(main, ["--json", "--config", str(config), "data", "add", "dbo.Heap"])
    assert result.exit_code == 2
    assert "no primary key or unique constraint" in json.loads(result.output)["error"]["message"]


@pytest.mark.integration
def test_deploy_releases_a_commit_and_records_it(tmp_path, server, make_db):
    dev = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, dev)
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "dev@example.com")
    git(tmp_path, "config", "user.name", "Dev")
    sqlsc(config, "commit", "-m", "v1")
    git(tmp_path, "tag", "v1")
    view = tmp_path / "database" / "Views" / "sales.vCustomerTotals.sql"
    edit(view, "SUM(o.Amount) AS Total", "SUM(o.Amount) AS Total\n     ,COUNT(*) AS Orders")
    git(tmp_path, "commit", "-qam", "v2: order count")
    edit(view, "COUNT(*) AS Orders", "COUNT(*) AS Orders\n     ,MAX(o.Amount) AS Biggest")  # not committed

    prod = make_db()
    add_target(config, "prod", server, prod)
    refused = CliRunner().invoke(main, ["--json", "--config", str(config), "deploy", "-t", "prod", "--ref", "v1"])
    assert refused.exit_code == 2 and "--confirm prod" in json.loads(refused.output)["error"]["message"]

    script = tmp_path / "v1.sql"
    written = sqlsc(config, "deploy", "-t", "prod", "--ref", "v1", "-o", str(script))
    assert written["commit"] == git(tmp_path, "rev-parse", "v1") and not written["applied"]
    text = script.read_text()
    assert text.startswith("-- Release of ") and "at v1 (" in text
    assert "INSERT INTO sqlsc.Deployment" in text and "COUNT(*)" not in text
    assert rows(server, prod, "SELECT COUNT(*) FROM sys.tables") == [(0,)]  # nothing ran

    first = sqlsc(config, "deploy", "-t", "prod", "--ref", "v1", "--confirm", "prod", "--as", "release-bot")
    assert first["ok"] and first["applied"] and first["still_different"] == []
    assert sqlsc(config, "deploy", "-t", "prod", "--ref", "v1", "--dry-run")["steps"] == []

    second = sqlsc(config, "deploy", "-t", "prod", "--confirm", "prod")  # HEAD: committed v2, not the working copy
    assert [(s["action"], s["object"]) for s in second["steps"]] == [("alter", "[sales].[vCustomerTotals]")]
    definition = rows(server, prod, "SELECT OBJECT_DEFINITION(OBJECT_ID('sales.vCustomerTotals'))")[0][0]
    assert "COUNT(*) AS Orders" in definition and "Biggest" not in definition

    releases = sqlsc(config, "history", "-t", "prod")["releases"]
    assert [(r["ref"], r["prepared_by"], r["login"]) for r in releases] == [
        ("HEAD", "dev@example.com", "sa"),
        ("v1", "release-bot", "sa"),
    ]
    assert releases[1]["changes"] > 5
    # The record table is the tool's own: never scripted, never compared.
    assert all("sqlsc" not in c["object"] for c in sqlsc(config, "status", "-t", "prod")["changes"])
