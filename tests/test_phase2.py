"""commit and get-latest against a real SQL Server (skipped unless SQLSC_TEST_SERVER is set)."""

from __future__ import annotations

import subprocess

import pytest

from .conftest import SAMPLE, connect, run_script
from .test_integration import link, sqlsc

pytestmark = pytest.mark.integration


def git(cwd, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def edit(path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, text
    path.write_text(text.replace(old, new))


# The same edits in each table shape a repo can hold: DacFx's (SSMS 22, and sqlsc since
# 1.0.11) and sqlsc's own from before 1.0.11.
EDITS = {
    "dacfx": [
        ("[Name]        NVARCHAR (100)  NOT NULL,", "[Name]        NVARCHAR (200)  NOT NULL,"),
        ("DEFAULT ('A')", "DEFAULT ('I')"),
        (
            "    [CreditLimit] DECIMAL (19, 4) NULL,\n",
            "    [CreditLimit] DECIMAL (19, 4) NULL,\n    [Phone] VARCHAR (20) NULL,\n",
        ),
        ("    CONSTRAINT [CK_Customer_Status] CHECK ([Status]='I' OR [Status]='A'),\n", ""),
    ],
    "sqlsc": [
        ("     ,Name nvarchar(100) NOT NULL\n", "     ,Name nvarchar(200) NOT NULL\n"),
        ("DEFAULT ('A')", "DEFAULT ('I')"),
        (
            "     ,CreditLimit decimal(19, 4) NULL\n",
            "     ,CreditLimit decimal(19, 4) NULL\n     ,Phone varchar(20) NULL\n",
        ),
        (
            "ALTER TABLE sales.Customer ADD CONSTRAINT CK_Customer_Status CHECK ([Status]='I' OR [Status]='A');\nGO\n",
            "",
        ),
    ],
}
INDEX = {
    "dacfx": "CREATE NONCLUSTERED INDEX [IX_Customer_Email]\n    ON [sales].[Customer]([Email] ASC);\n\n\nGO\n",
    "sqlsc": "CREATE NONCLUSTERED INDEX IX_Customer_Email ON sales.Customer (Email);\nGO\n",
}


def to_sqlsc_shape(folder) -> None:
    """Rewrites every table file the way sqlsc scripted tables before 1.0.11."""
    from coop_sqlsc.tablefile import sqlsc_shape

    for path in (folder / "Tables").glob("*.sql"):
        path.write_text(sqlsc_shape(path.read_text()))


@pytest.mark.parametrize("shape", ["dacfx", "sqlsc"])
def test_get_latest_alters_tables_and_keeps_their_rows(tmp_path, server, make_db, shape):
    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db)
    sqlsc(config, "script")
    if shape == "sqlsc":
        to_sqlsc_shape(tmp_path / "database")
        assert sqlsc(config, "status")["changes"] == []  # an older repo reads clean
    conn = connect(server, db)
    run_script(conn, "INSERT INTO sales.Customer (Name, Email) VALUES ('Ada', 'ada@example.com'), ('Bo', NULL);")

    customer = tmp_path / "database" / "Tables" / "sales.Customer.sql"
    for old, new in EDITS[shape]:
        edit(customer, old, new)
    with customer.open("a") as handle:
        handle.write(INDEX[shape])
    view = tmp_path / "database" / "Views" / "sales.vCustomerTotals.sql"
    edit(view, "SUM(o.Amount) AS Total", "SUM(o.Amount) AS Total\n     ,COUNT(*) AS Orders")

    plan = sqlsc(config, "get-latest", "--dry-run")
    assert {(s["action"], s["object"]) for s in plan["steps"]} == {
        ("alter", "[sales].[Customer]"),
        ("alter", "[sales].[vCustomerTotals]"),
    }
    assert "ALTER VIEW sales.vCustomerTotals" in plan["script"]

    result = sqlsc(config, "get-latest")
    assert result["ok"] and result["failed"] == [], result["failed"]
    assert result["still_different"] == []
    assert sqlsc(config, "status")["changes"] == []
    rows = conn.cursor().execute("SELECT Name FROM sales.Customer ORDER BY Name").fetchall()
    assert [r[0] for r in rows] == ["Ada", "Bo"]


def test_drops_need_allow_drops(tmp_path, server, make_db):
    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db)
    sqlsc(config, "script")
    (tmp_path / "database" / "Stored Procedures" / "sales.usp_GetCustomer.sql").unlink()
    edit(
        tmp_path / "database" / "Tables" / "sales.Customer.sql",
        "    [Notes]       NVARCHAR (MAX)  COLLATE Latin1_General_100_CI_AS NULL,\n",
        "",
    )

    held = sqlsc(config, "get-latest")
    assert {s["object"] for s in held["held_back"]} == {"[sales].[usp_GetCustomer]", "[sales].[Customer]"}
    assert len(sqlsc(config, "status")["changes"]) == 2  # nothing was dropped

    dropped = sqlsc(config, "get-latest", "--allow-drops")
    assert dropped["failed"] == [] and dropped["still_different"] == []


def test_a_failing_change_rolls_everything_back(tmp_path, server, make_db):
    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db)
    sqlsc(config, "script")
    edit(tmp_path / "database" / "Functions" / "dbo.fn_Double.sql", "RETURN @x * 2;", "RETURN @x * 3;")
    edit(
        tmp_path / "database" / "Views" / "sales.vAllTotals.sql", "FROM sales.vCustomerTotals", "FROM sales.NoSuchView"
    )

    result = sqlsc(config, "get-latest")
    assert result["ok"] is False
    assert result["failed"][0]["object"] == "[sales].[vAllTotals]"
    assert "Invalid object name" in result["failed"][0]["error"]
    status = {c["object"] for c in sqlsc(config, "status")["changes"]}
    assert status == {"[dbo].[fn_Double]", "[sales].[vAllTotals]"}  # the function change was undone too


def test_protected_targets_only_get_a_script(tmp_path, server, make_db):
    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db, name="prod")
    sqlsc(config, "script")
    edit(tmp_path / "database" / "Functions" / "dbo.fn_Double.sql", "RETURN @x * 2;", "RETURN @x * 3;")

    from click.testing import CliRunner

    from coop_sqlsc.cli import main

    refused = CliRunner().invoke(main, ["--json", "--config", str(config), "get-latest"])
    assert refused.exit_code == 2 and '"protected"' in refused.output
    out = tmp_path / "release.sql"
    written = sqlsc(config, "get-latest", "--output", str(out))
    assert written["output"] == str(out) and "ALTER FUNCTION dbo.fn_Double" in out.read_text()


def test_commit_commits_only_object_files(tmp_path, server, make_db):
    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db)
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "dev@example.com")
    git(tmp_path, "config", "user.name", "Dev")
    (tmp_path / "notes.txt").write_text("work in progress")
    git(tmp_path, "add", "notes.txt")

    first = sqlsc(config, "commit", "-m", "Initial script")
    assert first["commit"]
    assert git(tmp_path, "log", "--format=%s") == "Initial script"
    committed = git(tmp_path, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert "database/Tables/sales.Order.sql" in committed and "notes.txt" not in committed
    assert git(tmp_path, "diff", "--cached", "--name-only") == "notes.txt"  # still staged, untouched

    run_script(connect(server, db), "DROP VIEW sales.vAllTotals;")
    second = sqlsc(config, "commit")
    assert git(tmp_path, "log", "-1", "--format=%s") == "Update 1 object(s) from dev: [sales].[vAllTotals]"
    assert second["summary"] == {"new": 0, "changed": 0, "deleted": 1}
    assert sqlsc(config, "commit")["commit"] is None
