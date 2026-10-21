"""End to end against a real SQL Server (skipped unless SQLSC_TEST_SERVER is set)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from coop_sqlsc.cli import main

from .conftest import SAMPLE, run_script

pytestmark = pytest.mark.integration


def link(tmp_path: Path, server: str, database: str, *, name: str = "dev") -> Path:
    config = tmp_path / "sqlsc.yml"
    config.write_text(
        "folder: database\n"
        f"target: {name}\n"
        "targets:\n"
        f"  {name}:\n"
        "    kind: sql_server\n"
        f"    server: '{server}'\n"
        f"    database: {database}\n"
        "    auth: sql\n"
        "    user: sa\n"
        "    trust_server_certificate: true\n",
        encoding="utf-8",
    )
    return config


def sqlsc(config: Path, *args: str) -> dict:
    result = CliRunner().invoke(main, ["--json", "--config", str(config), *args])
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


def test_script_then_status_is_clean(tmp_path, server, make_db):
    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db)

    first = sqlsc(config, "status")
    assert first["summary"]["new"] == 9, first["changes"]
    assert {c["object"] for c in first["changes"]} >= {
        "[sales]",
        "[sales].[Customer]",
        "[sales].[Order]",
        "[sales].[vCustomerTotals]",
        "[dbo].[fn_Double]",
        "[sales].[usp_GetCustomer]",
        "[sales].[trCustomer_Touch]",
    }

    written = sqlsc(config, "script")
    assert len(written["files"]) == 9
    assert (tmp_path / "database" / "Tables" / "sales.Order.sql").is_file()

    again = sqlsc(config, "status")
    assert again["changes"] == []


def test_get_latest_rebuilds_an_identical_database(tmp_path, server, make_db):
    """The files are complete: get-latest into an empty database scripts back the same."""
    source = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, source)
    sqlsc(config, "script")

    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    config_copy = link(copy_dir, server, make_db())
    (copy_dir / "database").symlink_to(tmp_path / "database", target_is_directory=True)

    result = sqlsc(config_copy, "get-latest")
    assert result["ok"], result
    assert result["failed"] == [] and result["transaction"] is True
    assert result["still_different"] == []
    assert sqlsc(config_copy, "status")["changes"] == []


def test_changed_and_deleted_objects_show_up(tmp_path, server, make_db):
    import pyodbc

    from coop_sqlsc.connect import pick_driver

    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db)
    sqlsc(config, "script")

    import os

    conn = pyodbc.connect(
        f"Driver={{{pick_driver(pyodbc.drivers())}}};Server={server};Database={db};UID=sa;"
        f"PWD={os.environ['SQLSC_PASSWORD']};Encrypt=yes;TrustServerCertificate=yes;",
        autocommit=True,
    )
    run_script(
        conn,
        "ALTER PROCEDURE sales.usp_GetCustomer @CustomerId int AS SELECT 1;\nGO\n"
        "DROP FUNCTION dbo.fn_Double;\nGO\nALTER TABLE sales.Customer ADD Phone varchar(20) NULL;\nGO",
    )
    conn.close()

    status = sqlsc(config, "status")
    got = {(c["action"], c["object"]) for c in status["changes"]}
    assert got == {
        ("changed", "[sales].[usp_GetCustomer]"),
        ("deleted", "[dbo].[fn_Double]"),
        ("changed", "[sales].[Customer]"),
    }

    diff = sqlsc(config, "diff", "sales.Customer")
    assert len(diff["changes"]) == 1
    assert "+     ,Phone varchar(20) NULL" in diff["changes"][0]["diff"]

    sqlsc(config, "script")
    assert not (tmp_path / "database" / "Functions" / "dbo.fn_Double.sql").exists()
    assert sqlsc(config, "status")["changes"] == []


def test_a_quoted_identifier_off_module_does_not_leak_into_the_next(tmp_path, server, make_db):
    """#36: dbo.a (OFF) runs before dbo.b (ON) on one connection; dbo.b must keep ON."""
    from .conftest import connect

    source = make_db(
        "CREATE TABLE dbo.T (Name int NULL)\nGO\n"
        "SET QUOTED_IDENTIFIER OFF\nGO\n"
        "CREATE PROCEDURE dbo.a AS SELECT 1 AS n\nGO\n"
        "SET QUOTED_IDENTIFIER ON\nGO\n"
        'CREATE PROCEDURE dbo.b AS SELECT "Name" FROM dbo.T\nGO\n'
    )
    config = link(tmp_path, server, source)
    sqlsc(config, "script")

    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    copy = make_db()
    config_copy = link(copy_dir, server, copy)
    (copy_dir / "database").symlink_to(tmp_path / "database", target_is_directory=True)
    result = sqlsc(config_copy, "get-latest")
    assert result["ok"] and result["still_different"] == [], result

    conn = connect(server, copy)
    rows = (
        conn.cursor()
        .execute("SELECT OBJECT_NAME(object_id), uses_quoted_identifier FROM sys.sql_modules ORDER BY 1")
        .fetchall()
    )
    conn.close()
    assert [(r[0], bool(r[1])) for r in rows] == [("a", False), ("b", True)]
    assert sqlsc(config_copy, "status")["changes"] == []


def test_a_release_that_cannot_be_recorded_changes_nothing(tmp_path, server, make_db):
    """#34: the sqlsc.Deployment row shares the release's transaction."""
    import subprocess

    from .conftest import connect

    db = make_db("CREATE VIEW dbo.v AS SELECT 1 AS n\nGO\n")
    config = link(tmp_path, server, db)
    sqlsc(config, "script")
    for args in (["init", "-q"], ["config", "user.email", "ci@example.com"], ["config", "user.name", "CI"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "v1"], cwd=tmp_path, check=True)
    view = tmp_path / "database" / "Views" / "dbo.v.sql"
    view.write_text(view.read_text().replace("SELECT 1 AS n", "SELECT 2 AS n"))
    subprocess.run(["git", "commit", "-qam", "v2"], cwd=tmp_path, check=True)

    conn = connect(server, db)
    # A history table whose INSERT always fails: the release must roll back with it.
    run_script(
        conn,
        "CREATE SCHEMA sqlsc\nGO\n"
        "CREATE TABLE sqlsc.Deployment (DeploymentId int IDENTITY(1, 1) PRIMARY KEY, DeployedAt datetime2 NOT NULL,"
        " CommitId nvarchar(40) NOT NULL, Ref nvarchar(200) NULL, Repo nvarchar(400) NULL,"
        " PreparedBy nvarchar(256) NULL, LoginName nvarchar(256) NULL, Changes int NOT NULL,"
        " CONSTRAINT CK_never CHECK (Changes < 0))\nGO\n",
    )
    result = sqlsc(config, "deploy", "--ref", "HEAD")
    assert result["ok"] is False and result["recorded"] is False, result
    assert result["failed"][0]["object"] == "[sqlsc].[Deployment]"
    text = conn.cursor().execute("SELECT OBJECT_DEFINITION(OBJECT_ID('dbo.v'))").fetchone()[0]
    conn.close()
    assert "SELECT 1 AS n" in text
