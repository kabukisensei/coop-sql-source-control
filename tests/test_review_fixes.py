"""Regression tests for the 2026-10-21 review of sqlsc 1.0.2 (issues #30-#36).

Offline: a fake catalog and recording connections stand in for the database; git is real.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from coop_sqlsc import cli as cli_mod
from coop_sqlsc import git_ops
from coop_sqlsc.catalog import CatalogResult
from coop_sqlsc.compare import CHANGED, DELETED, NEW, Change, compare
from coop_sqlsc.config import build_target
from coop_sqlsc.deploy import apply, build_plan, migration_steps
from coop_sqlsc.layout import write_object
from coop_sqlsc.locks import Lock
from coop_sqlsc.migrations import parse
from coop_sqlsc.objects import ObjectKey
from coop_sqlsc.scripter import script_module
from coop_sqlsc.sync import BOTH, DATABASE, REPO, SyncRecord

VIEW = ObjectKey("view", "dbo", "v")
V1 = "CREATE VIEW dbo.v AS SELECT 1 AS n;\nGO\n"
V2 = "CREATE VIEW dbo.v AS SELECT 2 AS n;\nGO\n"
V3 = "CREATE VIEW dbo.v AS SELECT 3 AS n;\nGO\n"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def run(root: Path, *args: str):
    return CliRunner().invoke(cli_mod.main, ["--config", str(root / "sqlsc.yml"), *args])


def out(result) -> dict:
    return json.loads(result.output)


class Recorder:
    """A connection that records what ran and what a commit kept."""

    def __init__(self, refuse: str | None = None):
        self.autocommit = True
        self.refuse = refuse
        self.pending: list[str] = []
        self.committed: list[str] = []

    def cursor(self):
        return self

    def execute(self, sql, *params):
        if self.refuse and self.refuse in sql:
            raise RuntimeError(f"[SQL Server]The INSERT permission was denied on {self.refuse}.")
        self.pending.append(sql)
        if self.autocommit:
            self.committed.append(sql)
        return self

    def fetchone(self):
        return (1,)

    def fetchall(self):
        return []

    def commit(self):
        self.committed += self.pending
        self.pending = []

    def rollback(self):
        self.pending = []

    def close(self):
        pass

    @property
    def ran(self) -> list[str]:
        return self.committed + self.pending


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo linked to a fake SQL Server: (root, objects in the database, connection)."""
    objects: dict[ObjectKey, str] = {}
    conn = Recorder()
    monkeypatch.setattr(cli_mod, "open_connection", lambda target: conn)
    monkeypatch.setattr(cli_mod, "read_database", lambda c, t: CatalogResult(dict(objects), []))
    monkeypatch.setattr(cli_mod, "_peek_locks", lambda s, u: ({}, "dev@example.com"))
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "dev@example.com")
    git(tmp_path, "config", "user.name", "Dev")
    result = run(tmp_path, "init", "--kind", "sql_server", "--server", "fixture", "--database", "fixture")
    assert result.exit_code == 0, result.output
    return tmp_path, objects, conn


# --- #30 a drop meeting an edit is a conflict ------------------------------------------


@pytest.mark.parametrize("deleted_side", ["database", "repo"])
def test_delete_modify_is_a_conflict(deleted_side):
    sync = SyncRecord(None)
    compare({VIEW: V1}, {VIEW: V1}, sync)
    db, folder = ({}, {VIEW: V2}) if deleted_side == "database" else ({VIEW: V2}, {})
    (change,) = compare(db, folder, sync)
    assert change.origin == BOTH


def test_an_untouched_survivor_is_still_a_one_sided_delete():
    sync = SyncRecord(None)
    compare({VIEW: V1}, {VIEW: V1}, sync)
    assert compare({}, {VIEW: V1}, sync)[0].origin == DATABASE  # dropped from the database
    assert compare({VIEW: V1}, {}, sync)[0].origin == REPO  # deleted in the repo
    # Never synced: as before (a teammate's object; new in the database).
    fresh = SyncRecord(None)
    assert compare({}, {VIEW: V2}, fresh)[0].origin == REPO
    assert compare({VIEW: V2}, {}, fresh)[0].origin == DATABASE


def test_commit_keeps_a_file_edited_after_the_database_dropped_it(repo):
    root, db, _ = repo
    db[VIEW] = V1
    assert run(root, "--json", "commit", "-m", "v1").exit_code == 0
    del db[VIEW]
    path = write_object(root / "database", VIEW, V2)

    status = out(run(root, "--json", "status"))
    assert [(c["action"], c["origin"]) for c in status["changes"]] == [(DELETED, BOTH)]
    result = out(run(root, "--json", "commit"))
    assert result["files"] == [] and [h["object"] for h in result["held_back"]] == ["[dbo].[v]"]
    assert "dropped from the database and changed in the repo" in result["held_back"][0]["reason"]
    assert path.read_text() == V2
    # --force still commits the database's side (the drop), as documented.
    assert out(run(root, "--json", "commit", "--force"))["summary"]["deleted"] == 1
    assert not path.exists()


def test_get_latest_does_not_drop_an_object_edited_after_the_repo_deleted_it(repo):
    root, db, conn = repo
    db[VIEW] = V1
    assert run(root, "--json", "commit", "-m", "v1").exit_code == 0
    (root / "database" / "Views" / "dbo.v.sql").unlink()
    db[VIEW] = V2

    result = out(run(root, "--json", "get-latest", "--allow-drops"))
    assert result["steps"] == [] and [(k["object"], k["origin"]) for k in result["kept"]] == [("[dbo].[v]", BOTH)]
    assert not any("DROP VIEW" in sql for sql in conn.ran)
    forced = out(run(root, "--json", "get-latest", "--allow-drops", "--force"))
    assert [s["action"] for s in forced["steps"]] == ["drop"]


# --- #31 unreadable migration history ---------------------------------------------------


def _charge_script(root: Path) -> None:
    folder = root / "database" / "Migrations"
    folder.mkdir(parents=True)
    (folder / "001_charge.sql").write_text("UPDATE dbo.Balance SET Amount = Amount - 10;\nGO\n")


def test_get_latest_stops_when_migration_history_cannot_be_read(repo, monkeypatch):
    root, db, conn = repo
    _charge_script(root)
    db[VIEW] = V1
    write_object(root / "database", ObjectKey("view", "dbo", "w"), V1.replace("dbo.v", "dbo.w"))

    def unreadable(c):
        raise RuntimeError("SELECT denied on sqlsc.Migration")

    monkeypatch.setattr(cli_mod.migrations_mod, "read_applied", unreadable)
    result = run(root, "--json", "get-latest")
    assert result.exit_code == 2
    assert out(result)["error"]["kind"] == "history"
    assert conn.ran == []  # neither the migration nor the structural change ran
    assert run(root, "--json", "get-latest", "--dry-run").exit_code == 2  # no script that would replay it

    status = out(run(root, "--json", "status"))
    assert status["migrations"]["pending"] == [] and status["migrations"]["history_unknown"] is True
    listed = out(run(root, "--json", "migrations"))
    assert [m["state"] for m in listed["migrations"]] == ["unknown"]
    # Naming objects updates just those, with no migrations, so it can go ahead.
    named = out(run(root, "--json", "get-latest", "dbo.w"))
    assert named["ok"] and [s["object"] for s in named["steps"]] == ["[dbo].[w]"]
    assert not any("Amount - 10" in sql for sql in conn.ran)


def test_a_missing_history_table_still_means_nothing_has_run(repo, monkeypatch):
    root, _, conn = repo
    _charge_script(root)

    class FirstRun(Recorder):
        def fetchone(self):
            return (0,)  # OBJECT_ID('sqlsc.Migration') IS NULL

    first = FirstRun()
    monkeypatch.setattr(cli_mod, "open_connection", lambda target: first)
    result = out(run(root, "--json", "get-latest"))
    assert result["ok"] and [m["id"] for m in result["migrations"]] == ["001_charge"]
    assert any("Amount - 10" in sql for sql in first.committed)


# --- #32 check-in keeps what it could not commit -----------------------------------------


class Store:
    def __init__(self, locks: list[Lock]):
        self.locks = locks
        self.released: list[ObjectKey] = []

    def __call__(self, *args, **kwargs):
        return self

    def exists(self):
        return True

    def ensure(self):
        return False

    def list(self, repo):
        return list(self.locks)

    def release(self, keys, **kwargs):
        self.released += list(keys)
        return list(keys), []


def _lock(key: ObjectKey) -> Lock:
    return Lock("fixture", key, "dev@example.com", "dev", datetime.now(), None)


def test_checkin_keeps_a_conflicted_object_checked_out_and_says_so(repo, monkeypatch):
    root, db, _ = repo
    other = ObjectKey("view", "dbo", "w")
    db[VIEW], db[other] = V1, V1.replace("dbo.v", "dbo.w")
    assert run(root, "--json", "commit", "-m", "v1").exit_code == 0
    db[VIEW] = V2  # changed in the database...
    write_object(root / "database", VIEW, V3)  # ...and in the repo
    db[other] = V2.replace("dbo.v", "dbo.w")  # a clean change
    store = Store([_lock(VIEW), _lock(other)])
    monkeypatch.setattr(cli_mod, "LockStore", store)

    result = run(root, "--json", "checkin")
    assert result.exit_code == 1
    payload = out(result)
    assert payload["ok"] is False
    assert [f["object"] for f in payload["files"]] == ["[dbo].[w]"] and payload["commit"]
    assert [h["object"] for h in payload["held_back"]] == ["[dbo].[v]"]
    assert store.released == [other]
    assert [r["object"] for r in payload["released"]] == ["[dbo].[w]"]

    # --no-commit is a deliberate undo: it releases everything named.
    store.released = []
    assert run(root, "--json", "checkin", "--no-commit").exit_code == 0
    assert set(store.released) == {VIEW, other}


def test_checkin_with_only_conflicts_releases_nothing(repo, monkeypatch):
    root, db, _ = repo
    db[VIEW] = V1
    assert run(root, "--json", "commit", "-m", "v1").exit_code == 0
    db[VIEW] = V2
    write_object(root / "database", VIEW, V3)
    store = Store([_lock(VIEW)])
    monkeypatch.setattr(cli_mod, "LockStore", store)
    payload = out(run(root, "--json", "checkin"))
    assert payload["commit"] is None and payload["released"] == [] and store.released == []
    assert payload["held_back"][0]["origin"] == BOTH


# --- #33 schema filters cover deleted files ----------------------------------------------


def test_an_excluded_schemas_deleted_file_is_left_alone(repo):
    root, db, _ = repo
    staged = ObjectKey("view", "staging", "v")
    db[staged] = V1.replace("dbo.v", "staging.v")
    db[VIEW] = V1
    assert run(root, "--json", "commit", "-m", "v1").exit_code == 0
    with (root / "sqlsc.yml").open("a", encoding="utf-8") as f:
        f.write("exclude_schemas: [staging]\n")
    (root / "database" / "Views" / "staging.v.sql").unlink()
    db[VIEW] = V2

    status = out(run(root, "--json", "status"))
    assert [c["object"] for c in status["changes"]] == ["[dbo].[v]"]
    result = out(run(root, "--json", "commit"))
    assert [f["object"] for f in result["files"]] == ["[dbo].[v]"]
    shown = git(root, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert "database/Views/staging.v.sql" not in shown
    assert "D database/Views/staging.v.sql" in git(root, "status", "--porcelain")


# --- #34 the release record shares the release's transaction -----------------------------


def test_a_release_that_cannot_be_recorded_is_rolled_back(repo, monkeypatch):
    root, _, _ = repo
    conn = Recorder(refuse="sqlsc.Deployment")
    from coop_sqlsc.config import load

    cfg = load(root / "sqlsc.yml")
    state = cli_mod.State(cfg, cfg.target(), {}, {VIEW: V1}, [])
    monkeypatch.setattr(cli_mod, "open_connection", lambda target: conn)
    with pytest.raises(SystemExit) as stop:
        cli_mod.run_update(
            cli_mod.Opts(True, root / "sqlsc.yml"),
            state,
            objects=(),
            command="deploy",
            dry_run=False,
            output=None,
            allow_drops=False,
            footer=lambda plan: ["INSERT INTO sqlsc.Deployment VALUES (1)"],
        )
    assert stop.value.code == 1
    assert conn.committed == []


def test_deploy_reports_a_failed_record_cleanly(repo, monkeypatch):
    root, db, _ = repo
    db[VIEW] = V1
    assert run(root, "--json", "commit", "-m", "v1").exit_code == 0
    write_object(root / "database", VIEW, V2)
    git(root, "commit", "-qam", "v2")
    conn = Recorder(refuse="sqlsc.Deployment")
    monkeypatch.setattr(cli_mod, "open_connection", lambda target: conn)

    result = run(root, "--json", "deploy", "--ref", "HEAD")
    assert result.exit_code == 1
    payload = out(result)
    assert payload["ok"] is False and payload["recorded"] is False and payload["transaction"] is True
    assert payload["failed"][0]["object"] == "[sqlsc].[Deployment]"
    assert "not recorded" in payload["failed"][0]["error"]
    assert not any("ALTER VIEW" in sql for sql in conn.committed)

    # Nothing to change: the record alone fails just as cleanly.
    db[VIEW] = V2
    payload = out(run(root, "--json", "deploy", "--ref", "HEAD"))
    assert payload["ok"] is False and payload["recorded"] is False and payload["steps"] == []
    text = run(root, "deploy", "--ref", "HEAD").output
    assert "[sqlsc].[Deployment] failed" in text and "Recorded the release" not in text


# --- #35 git paths are UTF-8, whatever the locale ----------------------------------------


def test_read_tree_keeps_non_ascii_names_on_a_legacy_windows_locale(repo, monkeypatch):
    root, db, _ = repo
    table = ObjectKey("table", "dbo", "Café")
    db[table] = "CREATE TABLE dbo.[Café]\n(\n      Id int NOT NULL\n);\nGO\n"
    assert run(root, "--json", "commit", "-m", "v1").exit_code == 0
    commit = git_ops.resolve(root, "HEAD")
    folder = root / "database"
    expected = git_ops.read_tree(root, commit, folder)
    assert "Tables/dbo.Café.sql" in expected
    # subprocess's text mode decodes with the active code page when Python's UTF-8 mode is off.
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1252", raising=False)
    import locale

    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "cp1252")
    assert git_ops.read_tree(root, commit, folder) == expected


def test_read_tree_stops_when_git_cannot_read_a_listed_file(repo, monkeypatch):
    root, db, _ = repo
    db[VIEW] = V1
    assert run(root, "--json", "commit", "-m", "v1").exit_code == 0
    commit = git_ops.resolve(root, "HEAD")
    real = subprocess.run

    def lossy(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "cat-file", "--batch"]:
            kwargs["input"] = kwargs["input"].replace(b"dbo.v.sql", b"dbo.x.sql")
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", lossy)
    with pytest.raises(git_ops.GitError, match="could not read database/Views/dbo.v.sql"):
        git_ops.read_tree(root, commit, root / "database")


# --- #36 a SET ... OFF does not leak into the next object --------------------------------


class Session:
    """Tracks the connection's SET options the way SQL Server does: they last."""

    def __init__(self, fail_first: str | None = None):
        self.autocommit = True
        self.options = {"QUOTED_IDENTIFIER": True, "ANSI_NULLS": True}
        self.seen: dict[str, dict[str, bool]] = {}
        self.fail_first = fail_first

    def cursor(self):
        return self

    def execute(self, sql, *params):
        words = sql.split()
        if words[:1] == ["SET"] and words[1] in self.options:
            self.options[words[1]] = words[2].rstrip(";") == "ON"
            return self
        name = next((w for w in words if w.startswith("dbo.")), None)
        if name:
            if self.fail_first == name:
                self.fail_first = None
                raise RuntimeError("Invalid object name 'dbo.T'.")
            self.seen[name] = dict(self.options)
        return self

    def commit(self):
        pass

    def rollback(self):
        pass


TARGET = build_target("dev", {"kind": "sql_server", "server": "fixture", "database": "fixture"})


@pytest.mark.parametrize("fail_first", [None, "dbo.a"])
def test_a_module_scripted_with_options_off_does_not_change_the_next(fail_first):
    a, b = ObjectKey("procedure", "dbo", "a"), ObjectKey("procedure", "dbo", "b")
    off = script_module("CREATE PROCEDURE dbo.a AS SELECT 1;", quoted_identifier=False, ansi_nulls=False)
    on = script_module('CREATE PROCEDURE dbo.b AS SELECT "Name" FROM dbo.T;')
    changed = script_module("CREATE PROCEDURE dbo.a AS SELECT 2;", quoted_identifier=False)
    for changes in (
        [Change(a, DELETED, None, off), Change(b, DELETED, None, on)],  # created
        [Change(a, CHANGED, changed, off), Change(b, CHANGED, on.replace("Name", "Id"), on)],  # altered
    ):
        session = Session(fail_first)  # dbo.a first fails and is retried after dbo.b
        result = apply(session, build_plan(changes, allow_drops=False), TARGET)
        assert result.failed == []
        assert session.seen["dbo.a"] == {"QUOTED_IDENTIFIER": False, "ANSI_NULLS": False}  # its own script
        assert session.seen["dbo.b"] == {"QUOTED_IDENTIFIER": True, "ANSI_NULLS": True}


def test_the_written_script_turns_options_back_on():
    a, b = ObjectKey("view", "dbo", "a"), ObjectKey("view", "dbo", "b")
    off = script_module("CREATE VIEW dbo.a AS SELECT 1 AS n;", quoted_identifier=False)
    plan = build_plan(
        [Change(a, DELETED, None, off), Change(b, DELETED, None, script_module("CREATE VIEW dbo.b AS SELECT 1 AS n;"))],
        allow_drops=False,
    )
    text = plan.script(TARGET)
    assert text.index("SET QUOTED_IDENTIFIER OFF") < text.index("CREATE VIEW dbo.a")
    assert text.index("CREATE VIEW dbo.a") < text.index("SET QUOTED_IDENTIFIER ON") < text.index("CREATE VIEW dbo.b")
    # A module with the defaults gets nothing extra.
    (step,) = build_plan([Change(b, NEW, None, None)], allow_drops=True).runnable
    assert not any(s.startswith("SET") for s in step.statements)


def test_a_migration_that_turns_an_option_off_turns_it_back_on():
    script = parse("001_fix", "SET QUOTED_IDENTIFIER OFF\nGO\nUPDATE dbo.T SET n = 1\nGO\n")
    (step,) = migration_steps([script], "dev", None)
    assert step.statements[-1] == "SET QUOTED_IDENTIFIER ON"
    assert step.cleanup == ["SET QUOTED_IDENTIFIER ON"]
