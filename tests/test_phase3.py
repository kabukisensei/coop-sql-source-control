"""checkout, checkin, locks and the edit guard."""

from __future__ import annotations

import json
import uuid

import pytest
from click.testing import CliRunner

from coop_sqlsc.cli import main
from coop_sqlsc.config import build_target, load
from coop_sqlsc.locks import LockError, check_lock_target, key_from_name, normalize_remote
from coop_sqlsc.objects import ObjectKey

from .conftest import SAMPLE, connect
from .test_integration import link, sqlsc
from .test_phase2 import git

# --- units ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Kabukisensei/coop-sql.git",
        "https://github.com/kabukisensei/coop-sql",
        "https://token@github.com/kabukisensei/coop-sql.git/",
        "ssh://git@github.com/kabukisensei/coop-sql.git",
    ],
)
def test_every_remote_form_names_the_same_repo(url):
    assert normalize_remote(url) == "github.com/kabukisensei/coop-sql"


def test_new_object_keys_default_to_dbo():
    assert key_from_name("view", "vNew") == ObjectKey("view", "dbo", "vNew")
    assert key_from_name("procedure", "[sales].[usp_X]") == ObjectKey("procedure", "sales", "usp_X")
    assert key_from_name("schema", "[audit]") == ObjectKey("schema", "", "audit")


def test_lakehouse_cannot_hold_locks():
    lake = build_target("lake", {"kind": "fabric_lakehouse", "server": "x.fabric.microsoft.com", "database": "L"})
    with pytest.raises(LockError, match="locks: target:"):
        check_lock_target(lake, configured=False)
    check_lock_target(build_target("wh", {"kind": "fabric_warehouse", "server": "x", "database": "W"}), configured=True)


def test_locks_section_is_read(tmp_path):
    (tmp_path / "sqlsc.yml").write_text("target: dev\nlocks:\n  target: shared\n  repo: sales\n")
    cfg = load(tmp_path)
    assert (cfg.lock_target, cfg.lock_repo) == ("shared", "sales")


def test_lakehouse_checkout_explains_where_locks_go(tmp_path):
    (tmp_path / "sqlsc.yml").write_text(
        "target: lake\ntargets:\n  lake:\n    kind: fabric_lakehouse\n    server: x.fabric.microsoft.com\n"
        "    database: L\n"
    )
    (tmp_path / "database" / "Views").mkdir(parents=True)
    (tmp_path / "database" / "Views" / "dbo.v.sql").write_text("CREATE VIEW dbo.v AS SELECT 1 AS x\n")
    result = CliRunner().invoke(main, ["--json", "--config", str(tmp_path / "sqlsc.yml"), "checkout", "dbo.v"])
    assert result.exit_code == 2
    error = json.loads(result.output)["error"]
    assert error["kind"] == "locks" and "cannot hold the lock table" in error["message"]


# --- against a live SQL Server ----------------------------------------------------------------


def as_user(config, who: str, *args: str) -> dict:
    return sqlsc(config, *args, "--as", who)


@pytest.mark.integration
def test_checkout_conflicts_status_commit_and_checkin(tmp_path, server, make_db):
    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db)
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "dev@example.com")
    git(tmp_path, "config", "user.name", "Dev")
    sqlsc(config, "commit", "-m", "Initial script")

    first = as_user(config, "alice@example.com", "checkout", "sales.vCustomerTotals", "-m", "adding order count")
    assert first["ok"] and first["created_lock_table"]
    assert [o["object"] for o in first["checked_out"]] == ["[sales].[vCustomerTotals]"]

    clash = as_user(config, "bob@example.com", "checkout", "vCustomerTotals", "sales.usp_GetCustomer")
    assert not clash["ok"]
    assert [c["locked_by"] for c in clash["conflicts"]] == ["alice@example.com"]
    assert [o["object"] for o in clash["checked_out"]] == ["[sales].[usp_GetCustomer]"]
    again = as_user(config, "ALICE@example.com", "checkout", "sales.vCustomerTotals")  # case-insensitive owner
    assert again["ok"]

    listed = as_user(config, "bob@example.com", "locks")
    assert {(lock["object"], lock["locked_by"]) for lock in listed["locks"]} == {
        ("[sales].[vCustomerTotals]", "alice@example.com"),
        ("[sales].[usp_GetCustomer]", "bob@example.com"),
    }
    assert [lock["object"] for lock in as_user(config, "bob@example.com", "locks", "--mine")["locks"]] == [
        "[sales].[usp_GetCustomer]"
    ]

    # Both edit the shared database; the lock table itself never shows up as a change.
    conn = connect(server, db)
    cur = conn.cursor()
    cur.execute(
        "ALTER VIEW sales.vCustomerTotals AS SELECT c.CustomerID, SUM(o.Amount) AS Total, COUNT(*) AS Orders "
        "FROM sales.Customer AS c JOIN sales.[Order] AS o ON o.CustomerID = c.CustomerID GROUP BY c.CustomerID"
    )
    cur.execute("CREATE VIEW dbo.vLoose AS SELECT 1 AS One")
    status = as_user(config, "bob@example.com", "status")
    owners = {c["object"]: c["locked_by"] for c in status["changes"]}
    assert owners == {"[sales].[vCustomerTotals]": "alice@example.com", "[dbo].[vLoose]": None}

    # Bob's commit leaves Alice's view alone.
    bobs = as_user(config, "bob@example.com", "commit", "-m", "Bob's work")
    assert [f["object"] for f in bobs["files"]] == ["[dbo].[vLoose]"]
    assert [s["object"] for s in bobs["skipped"]] == ["[sales].[vCustomerTotals]"]

    # Alice checks in: her view is committed and released.
    done = as_user(config, "alice@example.com", "checkin", "-m", "Order count")
    assert done["ok"] and done["commit"]
    assert [f["object"] for f in done["files"]] == ["[sales].[vCustomerTotals]"]
    assert [r["object"] for r in done["released"]] == ["[sales].[vCustomerTotals]"]
    assert git(tmp_path, "log", "-1", "--format=%s") == "Order count"
    assert git(tmp_path, "show", "--name-only", "--format=", "HEAD") == "database/Views/sales.vCustomerTotals.sql"
    assert [lock["object"] for lock in sqlsc(config, "locks")["locks"]] == ["[sales].[usp_GetCustomer]"]

    # Releasing someone else's checkout takes --force.
    refused = as_user(config, "alice@example.com", "unlock", "sales.usp_GetCustomer")
    assert not refused["ok"] and refused["refused"][0]["locked_by"] == "bob@example.com"
    forced = as_user(config, "alice@example.com", "unlock", "sales.usp_GetCustomer", "--force")
    assert forced["ok"] and sqlsc(config, "locks")["locks"] == []
    assert sqlsc(config, "status")["changes"] == []


@pytest.mark.integration
def test_checkout_of_a_new_object_needs_its_type(tmp_path, server, make_db):
    config = link(tmp_path, server, make_db(SAMPLE.read_text()))
    result = CliRunner().invoke(main, ["--json", "--config", str(config), "checkout", "sales.vNotYet"])
    assert result.exit_code == 2 and json.loads(result.output)["error"]["kind"] == "not_found"
    ok = as_user(config, "alice@example.com", "checkout", "sales.vNotYet", "--type", "view")
    assert ok["checked_out"] == [{"object": "[sales].[vNotYet]", "type": "view", "schema": "sales", "name": "vNotYet"}]
    undo = as_user(config, "alice@example.com", "checkin", "--no-commit")
    assert undo["commit"] is None and [r["object"] for r in undo["released"]] == ["[sales].[vNotYet]"]


@pytest.mark.integration
def test_locks_can_live_in_another_database(tmp_path, server, make_db):
    work, shared = make_db(SAMPLE.read_text()), make_db()
    config = link(tmp_path, server, work)
    text = config.read_text().replace("targets:\n", "locks:\n  target: shared\n  repo: sales-db\ntargets:\n")
    text += (
        "  shared:\n    kind: sql_server\n"
        f"    server: '{server}'\n    database: {shared}\n    auth: sql\n    user: sa\n"
        "    trust_server_certificate: true\n"
    )
    config.write_text(text)
    result = as_user(config, "alice@example.com", "checkout", "sales.Customer")
    assert result["lock_target"]["name"] == "shared" and result["repo"] == "sales-db"
    row = connect(server, shared).cursor().execute("SELECT Repo, ObjectName FROM sqlsc.ObjectLock").fetchall()
    assert [tuple(r) for r in row] == [("sales-db", "Customer")]
    guard = CliRunner().invoke(main, ["--json", "--config", str(config), "guard", "install"])
    assert guard.exit_code == 2 and "only works when locks live" in json.loads(guard.output)["error"]["message"]


@pytest.mark.integration
def test_guard_stops_other_logins_changing_a_checked_out_object(tmp_path, server, make_db):
    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db)
    login, password = f"sqlsc_bob_{uuid.uuid4().hex[:6]}", "Bob_Pw_" + uuid.uuid4().hex[:10] + "!"
    admin = connect(server, db)
    admin.cursor().execute(f"CREATE LOGIN [{login}] WITH PASSWORD = '{password}', CHECK_POLICY = OFF")
    try:
        admin.cursor().execute(f"CREATE USER [{login}] FOR LOGIN [{login}]; ALTER ROLE db_owner ADD MEMBER [{login}]")
        assert sqlsc(config, "guard", "install")["installed"] is True
        assert sqlsc(config, "status")["changes"][0]["action"] == "new"  # the guard is not scripted
        as_user(config, "alice@example.com", "checkout", "sales.vCustomerTotals")

        import pyodbc

        from coop_sqlsc.connect import pick_driver

        bob = pyodbc.connect(
            f"Driver={{{pick_driver(pyodbc.drivers())}}};Server={server};Database={db};UID={login};PWD={password};"
            "Encrypt=yes;TrustServerCertificate=yes;",
            autocommit=True,
        )
        with pytest.raises(pyodbc.Error, match="checked out by alice@example.com"):
            bob.cursor().execute("ALTER VIEW sales.vCustomerTotals AS SELECT 1 AS CustomerID, 0 AS Total")
        bob.cursor().execute("ALTER VIEW sales.vAllTotals AS SELECT SUM(Total) AS Total FROM sales.vCustomerTotals")
        bob.close()
        # The login that checked it out may change it.
        admin.cursor().execute("ALTER VIEW sales.vCustomerTotals AS SELECT 1 AS CustomerID, 0 AS Total")
        assert sqlsc(config, "guard", "remove")["installed"] is False
    finally:
        admin.cursor().execute(f"DROP USER IF EXISTS [{login}]")
        admin.close()
        master = connect(server, "master")
        for (spid,) in (
            master.cursor()
            .execute("SELECT session_id FROM sys.dm_exec_sessions WHERE login_name = ?", login)
            .fetchall()
        ):
            master.cursor().execute(f"KILL {spid}")  # the driver pools bob's connection
        master.cursor().execute(f"DROP LOGIN [{login}]")
        master.close()
