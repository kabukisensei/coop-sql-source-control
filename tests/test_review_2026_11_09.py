"""Regression tests for the 2026-11-09 review: static-data keys the script does not
hold (#84), checkin keeping unread objects checked out (#77), and file names that
git must read as names, not patterns.

Offline: fake catalogs and recording connections stand in for the database; git is real.
"""

from __future__ import annotations

import json

import pytest

from coop_sqlsc import cli as cli_mod
from coop_sqlsc import data, git_ops
from coop_sqlsc.config import Target
from coop_sqlsc.layout import write_object
from coop_sqlsc.objects import ObjectKey

from .test_review_fixes import V1, V2, V3, Store, _lock, git, run
from .test_review_fixes import repo as review_repo  # noqa: F401  (fixture)
from .test_unread_catalog import HEALTHY
from .test_unread_objects import DATA, INT_ID, PK, VIEW, DataConn, without_view_definition
from .test_unread_objects import repo as unread_repo  # noqa: F401  (fixture)

TARGET = Target("dev", "sql_server", "", "Fixture", "entra")
BASE = ("Base", "int", 4, 10, 0, False, False)
COMPUTED_KEY = ("ComputedKey", "int", 4, 10, 0, False, True)
LABEL = ("Label", "nvarchar", 40, 0, 0, False, False)
STAMP = ("Stamp", "timestamp", 8, 0, 0, False, False)


# --- #84 a key column the data script does not hold ---------------------------------------


@pytest.mark.parametrize(
    "columns, keys, named",
    [
        (
            [BASE, COMPUTED_KEY, LABEL],
            [("PK", "PK_Lookup", "ComputedKey")],
            "primary key uses ComputedKey (a computed column)",
        ),
        (
            [BASE, COMPUTED_KEY, LABEL],
            [("UQ", "UQ_Lookup", "ComputedKey")],
            "unique constraint UQ_Lookup uses ComputedKey",
        ),
        (
            [BASE, COMPUTED_KEY, LABEL],
            [("PK", "PK_Lookup", "Base"), ("PK", "PK_Lookup", "ComputedKey")],
            "primary key uses ComputedKey",
        ),
        ([BASE, STAMP], [("UQ", "UQ_Stamp", "Stamp")], "Stamp (a timestamp column)"),
    ],
)
def test_a_key_the_script_cannot_hold_is_refused(columns, keys, named):
    for rows in ([("1", "One")], []):  # nonempty and empty captures alike
        conn = DataConn(columns, keys)
        conn.fetchall = lambda c=conn, r=rows: (
            c.columns if c.sql == data.COLUMNS_SQL else c.keys if c.sql == data.KEY_SQL else r
        )
        with pytest.raises(data.DataError) as caught:
            data.script_data(conn, ObjectKey("data", "dbo", "Lookup"), TARGET)
        assert named in str(caught.value)


def test_an_ordinary_key_is_still_held_in_the_script():
    for kind in ("PK", "UQ"):
        conn = DataConn([BASE, COMPUTED_KEY, LABEL], [(kind, "K", "Base")])
        conn.fetchall = lambda c=conn: (
            c.columns if c.sql == data.COLUMNS_SQL else c.keys if c.sql == data.KEY_SQL else [("1", "One")]
        )
        script, count = data.script_data(conn, ObjectKey("data", "dbo", "Lookup"), TARGET)
        assert count == 1
        assert ") AS v (Base, Label)" in script
        assert "ON target.Base = source.Base" in script
        assert "ComputedKey" not in script


def test_data_add_refuses_a_computed_key_without_writing_a_file(review_repo, monkeypatch):  # noqa: F811
    root, _, conn = review_repo
    from coop_sqlsc import cli_release, locks

    monkeypatch.setattr(
        locks, "lookup", lambda c, name: type("Found", (), {"type": "table", "schema": "dbo", "name": "Lookup"})()
    )
    monkeypatch.setattr(cli_release, "open_connection", lambda target: conn, raising=False)

    def fetchall():
        last = conn.ran[-1]
        if last == data.COLUMNS_SQL:
            return [BASE, COMPUTED_KEY, LABEL]
        if last == data.KEY_SQL:
            return [("PK", "PK_Lookup", "ComputedKey")]
        return [("1", "One")]

    monkeypatch.setattr(conn, "fetchall", fetchall)
    result = run(root, "--json", "data", "add", "dbo.Lookup")
    assert result.exit_code != 0
    assert "ComputedKey" in result.output
    assert not (root / "database" / "Data").exists() or not list((root / "database" / "Data").iterdir())


def test_tracked_data_whose_key_becomes_computed_is_held_then_recovers(unread_repo):  # noqa: F811
    repo = unread_repo
    path = repo / "database" / "Data" / "dbo.T.sql"
    before, head = path.read_text(encoding="utf-8"), repo.git("rev-parse", "HEAD")
    repo.data_columns = [("Id", "int", 4, 10, 0, False, True)]
    status = json.loads(run(repo, "--json", "status").output)
    assert status["unread"] == ["Data/dbo.T.sql"] and status["changes"] == []
    assert any("computed column" in w for w in status["warnings"])
    assert json.loads(run(repo, "--json", "commit", "-m", "While unread").output)["commit"] is None
    assert path.read_text(encoding="utf-8") == before and repo.git("rev-parse", "HEAD") == head
    repo.data_columns, repo.data_keys = [INT_ID], [PK]
    status = json.loads(run(repo, "--json", "status").output)
    assert status["unread"] == [] and status["changes"] == []


# --- #77 checkin keeps an unread object checked out --------------------------------------


def _checkin(repo, monkeypatch, keys):
    store = Store([_lock(k) for k in keys])
    monkeypatch.setattr(cli_mod, "LockStore", store)
    monkeypatch.setattr(cli_mod, "_peek_locks", lambda s, u: ({}, "dev@example.com", None), raising=False)
    result = run(repo, "--json", "checkin", "--no-pull")
    return result, json.loads(result.output), store


@pytest.mark.parametrize("how", ["null", "missing"])
def test_checkin_keeps_an_unread_view_checked_out(unread_repo, monkeypatch, how):  # noqa: F811
    repo = unread_repo
    repo.rows = without_view_definition(HEALTHY, how)
    head = repo.git("rev-parse", "HEAD")
    result, answer, store = _checkin(repo, monkeypatch, [VIEW])
    assert result.exit_code == 1
    assert answer["ok"] is False and answer["commit"] is None
    assert store.released == []
    assert [(h["object"], h["action"]) for h in answer["held_back"]] == [("[dbo].[vT]", "unread")]
    assert "could not be read from the database" in answer["held_back"][0]["reason"]
    assert repo.git("rev-parse", "HEAD") == head

    repo.rows = dict(HEALTHY)  # a healthy read: it checks in normally
    result, answer, store = _checkin(repo, monkeypatch, [VIEW])
    assert result.exit_code == 0, result.output
    assert answer["ok"] is True and store.released == [VIEW]


def test_checkin_keeps_unread_static_data_checked_out(unread_repo, monkeypatch):  # noqa: F811
    repo = unread_repo
    repo.data_keys = []
    result, answer, store = _checkin(repo, monkeypatch, [DATA])
    assert result.exit_code == 1 and store.released == []
    assert [h["action"] for h in answer["held_back"]] == ["unread"]


def test_checkin_no_commit_still_releases_an_unread_object(unread_repo, monkeypatch):  # noqa: F811
    repo = unread_repo
    repo.rows = without_view_definition(HEALTHY, "null")
    store = Store([_lock(VIEW)])
    monkeypatch.setattr(cli_mod, "LockStore", store)
    result = run(repo, "--json", "checkin", "--no-commit")
    assert result.exit_code == 0, result.output
    assert store.released == [VIEW]


# --- file names are names, never patterns ------------------------------------------------


def test_commit_paths_takes_a_bracketed_name_literally(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "dev@example.com")
    git(tmp_path, "config", "user.name", "Dev")
    views = tmp_path / "Views"
    views.mkdir()
    chosen, other, star = views / "dbo.v[1].sql", views / "dbo.v1.sql", views / "dbo.a*b.sql"
    for path in (chosen, other):
        path.write_text("one\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    for path in (chosen, other):
        path.write_text("two\n", encoding="utf-8")
    sha = git_ops.commit_paths(tmp_path, [chosen], "chosen only")
    assert sha
    assert git(tmp_path, "show", "--name-only", "--format=", "HEAD").splitlines() == ["Views/dbo.v[1].sql"]
    assert git(tmp_path, "status", "--porcelain") == "M Views/dbo.v1.sql"
    if not star.exists():
        try:
            star.write_text("x\n", encoding="utf-8")
        except OSError:
            return  # Windows: * is not a file-name character
        (views / "dbo.aXb.sql").write_text("y\n", encoding="utf-8")
        git_ops.commit_paths(tmp_path, [star], "star only")
        assert git(tmp_path, "show", "--name-only", "--format=", "HEAD").splitlines() == ["Views/dbo.a*b.sql"]
        assert "?? Views/dbo.aXb.sql" in git(tmp_path, "status", "--porcelain")


def test_checkin_commits_a_bracketed_name_as_a_name(review_repo, monkeypatch):  # noqa: F811
    root, db, _ = review_repo
    chosen, other = ObjectKey("view", "dbo", "v[1]"), ObjectKey("view", "dbo", "v1")
    db[chosen], db[other] = V1.replace("dbo.v", "dbo.[v[1]]]"), V1.replace("dbo.v", "dbo.v1")
    assert run(root, "--json", "commit", "-m", "base").exit_code == 0
    db[chosen] = V2.replace("dbo.v", "dbo.[v[1]]]")
    db[other] = V2.replace("dbo.v", "dbo.v1")  # changed in the database...
    write_object(root / "database", other, V3.replace("dbo.v", "dbo.v1"))  # ...and in the repo: held back
    other_file = root / "database" / "Views" / "dbo.v1.sql"
    held_text = other_file.read_text(encoding="utf-8")
    store = Store([_lock(chosen), _lock(other)])
    monkeypatch.setattr(cli_mod, "LockStore", store)
    result = run(root, "--json", "checkin")  # every checkout of mine
    payload = json.loads(result.output)
    assert [h["object"] for h in payload["held_back"]] == ["[dbo].[v1]"], result.output
    committed = git(root, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert committed == ["database/Views/dbo.v[1].sql"], result.output
    assert "dbo.v1.sql" in git(root, "status", "--porcelain")
    assert other_file.read_text(encoding="utf-8") == held_text
    assert store.released == [chosen]


def test_every_git_call_reads_paths_as_names():
    assert git_ops.GIT == ["git", "--literal-pathspecs"]
