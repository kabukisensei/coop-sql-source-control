"""Two people and coop on one dev database: nobody steps on anyone's toes, and
every change lands in both the repository and the database.

Offline, like test_review_fixes: a shared dict stands in for the dev database, an
in-memory table for sqlsc.ObjectLock, and real git with a bare `origin` that two
clones (Ada's and Bob's) push to and pull from. coop acts for Ada as
`--as coop-agent:ada@example.com`, the way coop-agent's sql_source tool does.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from coop_sqlsc import cli as cli_mod
from coop_sqlsc.catalog import CatalogResult
from coop_sqlsc.layout import write_object
from coop_sqlsc.locks import Lock, owns
from coop_sqlsc.objects import ObjectKey
from coop_sqlsc.sync import BOTH, DATABASE, REPO

V = ObjectKey("view", "dbo", "v")
W = ObjectKey("view", "dbo", "w")
V1 = "CREATE VIEW dbo.v AS SELECT 1 AS n;\nGO\n"
V2 = "CREATE VIEW dbo.v AS SELECT 2 AS n;\nGO\n"
V3 = "CREATE VIEW dbo.v AS SELECT 3 AS n;\nGO\n"
W1 = "CREATE VIEW dbo.w AS SELECT 1 AS n;\nGO\n"
W2 = "CREATE VIEW dbo.w AS SELECT 2 AS n;\nGO\n"
ADA, BOB, COOP = "ada@example.com", "bob@example.com", "coop-agent:ada@example.com"


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, check=check, capture_output=True, text=True)


def run(root: Path, *args: str):
    return CliRunner().invoke(cli_mod.main, ["--json", "--config", str(root / "sqlsc.yml"), *args])


def out(result) -> dict:
    return json.loads(result.output)


class Conn:
    autocommit = True

    def cursor(self):
        return self

    def execute(self, sql, *params):
        return self

    def fetchone(self):
        return (1,)

    def fetchall(self):
        return []

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class LockTable:
    """sqlsc.ObjectLock in memory, shared by every clone (one dev database)."""

    def __init__(self):
        self.rows: dict[tuple[str, ObjectKey], Lock] = {}

    def __call__(self, conn, *, transactional):
        return self

    def exists(self):
        return True

    def ensure(self):
        return False

    def login(self):
        return "dev-login"

    def list(self, repo=None):
        return [lock for (r, _), lock in self.rows.items() if repo is None or r == repo]

    def acquire(self, keys, *, repo, who, note):
        granted, conflicts = [], []
        for key in keys:
            current = self.rows.get((repo, key))
            if current is not None and not owns(current.locked_by, who):
                conflicts.append(current)
                continue
            if current is None:
                self.rows[(repo, key)] = Lock(repo, key, who, "dev-login", datetime.now(timezone.utc), note)
            granted.append(key)
        return granted, conflicts

    def release(self, keys, *, repo, who, force=False):
        released, refused = [], []
        for (r, key), lock in list(self.rows.items()):
            if r != repo or (keys is not None and key not in keys):
                continue
            mine = owns(lock.locked_by, who)
            if keys is None and not mine:
                continue
            if not mine and not force:
                refused.append(lock)
                continue
            del self.rows[(r, key)]
            released.append(key)
        return released, refused


class Team:
    def __init__(self, tmp_path: Path, layout: str = "type"):
        self.db: dict[ObjectKey, str] = {}
        self.locks = LockTable()
        self.layout = layout
        self.origin = tmp_path / "origin.git"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.ada = self.clone(tmp_path / "ada", ADA, "Ada")
        result = run(
            self.ada, "init", "--kind", "sql_server", "--server", "fixture", "--database", "fixture", "--layout", layout
        )
        assert result.exit_code == 0, result.output
        git(self.ada, "add", "-A")
        git(self.ada, "commit", "-q", "-m", "sqlsc setup")
        git(self.ada, "push", "-q", "-u", "origin", "main")
        self.bob = self.clone(tmp_path / "bob", BOB, "Bob")

    def clone(self, where: Path, email: str, name: str) -> Path:
        git(where.parent, "clone", "-q", str(self.origin), str(where))
        git(where, "config", "user.email", email)
        git(where, "config", "user.name", name)
        return where

    def view_path(self, key: ObjectKey) -> str:
        """A view's file, relative to the scripts folder, in this team's layout."""
        if self.layout == "schema":
            return f"{key.schema}/Views/{key.name}.sql"
        return f"Views/{key.schema}.{key.name}.sql"

    def pull(self, clone: Path) -> None:
        git(clone, "pull", "-q", "--ff-only")

    def head(self, clone: Path) -> str:
        return git(clone, "rev-parse", "HEAD").stdout.strip()

    def file(self, clone: Path, key: ObjectKey) -> str | None:
        path = clone / "database" / self.view_path(key)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def locked_by(self, key: ObjectKey) -> str | None:
        found = [lock.locked_by for (_, k), lock in self.locks.rows.items() if k == key]
        return found[0] if found else None


# Every scenario runs in both file layouts: sqlsc's own (`type`) and the one SSMS's
# Create Project from Database and SqlPackage write (`schema`, 2026-10-27).
@pytest.fixture(params=["type", "schema"])
def team(tmp_path, monkeypatch, request):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    team = Team(tmp_path, request.param)
    monkeypatch.setattr(cli_mod, "open_connection", lambda target: Conn())
    monkeypatch.setattr(cli_mod, "read_database", lambda c, t: CatalogResult(dict(team.db), []))
    monkeypatch.setattr(cli_mod, "LockStore", team.locks)
    return team


# --- the happy path: a change made in the database reaches the repo, then every clone ----


def test_a_change_in_dev_reaches_the_repo_and_every_clone(team):
    team.db[V] = V1
    first = out(run(team.ada, "commit", "-m", "add dbo.v", "--push"))
    assert first["commit"] and first["pushed"] == "main" and first["git"]["branch"] == "main"
    # Bob's clone is behind; his status says so without touching the network, and his
    # next commit pulls Ada's commit in first, so dbo.v is not committed twice.
    git(team.bob, "fetch", "-q")
    status = out(run(team.bob, "status"))
    assert status["git"]["behind"] == 1
    assert any("1 commit(s) behind origin/main" in w for w in status["warnings"])
    team.db[W] = W1
    second = out(run(team.bob, "commit", "-m", "add dbo.w", "--push"))
    assert second["git"]["pulled"] == 1 and second["commit"]
    assert [f["object"] for f in second["files"]] == ["[dbo].[w]"]
    assert team.file(team.bob, V) == V1
    team.pull(team.ada)
    assert team.file(team.ada, W) == W1
    assert out(run(team.ada, "status"))["changes"] == []


# --- two people on one object -------------------------------------------------------------


def test_a_checkout_keeps_a_teammate_and_their_commit_off_the_object(team):
    team.db[V] = V1
    run(team.ada, "commit", "-m", "baseline", "--push")
    team.pull(team.bob)
    assert out(run(team.ada, "checkout", "dbo.v", "-m", "widening n"))["checked_out"]
    # Bob cannot check it out, and his commit leaves it for Ada.
    bob = run(team.bob, "checkout", "dbo.v")
    assert bob.exit_code == 1
    assert out(bob)["conflicts"][0]["locked_by"] == ADA and out(bob)["conflicts"][0]["note"] == "widening n"
    team.db[V] = V2  # Ada's edit in SSMS, live in dev
    team.db[W] = W1  # Bob's new view
    commit = out(run(team.bob, "commit", "-m", "bob's work", "--push"))
    assert [f["object"] for f in commit["files"]] == ["[dbo].[w]"]
    assert [s["object"] for s in commit["skipped"]] == ["[dbo].[v]"]
    assert team.file(team.bob, V) == V1  # untouched
    # Ada checks in: her commit lands on top of Bob's (pull first) and releases the lock.
    checkin = out(run(team.ada, "checkin", "-m", "widen n", "--push"))
    assert checkin["git"]["pulled"] == 1 and checkin["commit"] and checkin["pushed"] == "main"
    assert [r["object"] for r in checkin["released"]] == ["[dbo].[v]"]
    assert team.locked_by(V) is None
    assert team.file(team.ada, W) == W1 and team.file(team.ada, V) == V2
    team.pull(team.bob)
    assert team.file(team.bob, V) == V2


def test_coop_works_for_ada_and_bob_is_kept_off_its_object(team):
    team.db[V] = V1
    run(team.ada, "commit", "-m", "baseline", "--push")
    team.pull(team.bob)
    # coop checks out on Ada's behalf before it edits (sql_source checkout).
    assert out(run(team.ada, "checkout", "dbo.v", "--as", COOP, "-m", "task 12"))["checked_out"]
    assert team.locked_by(V) == COOP
    assert run(team.bob, "checkout", "dbo.v").exit_code == 1
    # Ada may take her agent's checkout over; coop may not take Ada's.
    assert out(run(team.ada, "checkout", "dbo.v"))["checked_out"]
    team.db[V] = V2  # coop's approved dev write
    # sql_source commit: sqlsc commit --as coop-agent:ada, then unlock for what it committed.
    commit = out(run(team.ada, "commit", "-m", "coop: widen n", "--as", COOP))
    assert commit["commit"] and [f["object"] for f in commit["files"]] == ["[dbo].[v]"]
    assert out(run(team.ada, "unlock", "dbo.v", "--as", COOP))["released"]
    assert team.locked_by(V) is None
    # The commit is local until a person pushes (coop never pushes).
    assert out(run(team.ada, "status"))["git"]["ahead"] == 1


def test_an_object_changed_on_both_sides_is_never_overwritten_silently(team):
    team.db[V] = V1
    run(team.ada, "commit", "-m", "baseline", "--push")
    team.pull(team.bob)
    assert out(run(team.bob, "status"))["changes"] == []  # Bob's sync record now knows dbo.v
    # Ada edits the file in her clone and pushes it; meanwhile Bob changes dev in SSMS.
    write_object(team.ada / "database", V, V2)
    git(team.ada, "commit", "-q", "-am", "edit v in the repo")
    git(team.ada, "push", "-q")
    team.db[V] = V3
    # Bob's clone has synced dbo.v before, so status knows the database moved...
    assert out(run(team.bob, "status"))["changes"][0]["origin"] == DATABASE
    commit = out(run(team.bob, "commit", "-m", "bob"))
    # ...and the pull brought Ada's edit in first, so now both sides moved: held back.
    assert commit["git"]["pulled"] == 1 and commit["commit"] is None
    assert commit["held_back"][0]["origin"] == BOTH
    assert team.file(team.bob, V) == V2 and team.db[V] == V3
    latest = out(run(team.bob, "get-latest", "--dry-run"))
    assert [k["object"] for k in latest["kept"]] == ["[dbo].[v]"]
    # Only a deliberate --force picks a side.
    forced = out(run(team.bob, "commit", "-m", "take the database's version", "--force"))
    assert forced["commit"] and team.file(team.bob, V) == V3


def test_a_fresh_clone_does_not_commit_the_database_over_a_teammates_pushed_edit(team):
    team.db[V] = V1
    run(team.ada, "commit", "-m", "baseline", "--push")
    team.pull(team.bob)  # Bob has never run sqlsc here: no sync record
    write_object(team.ada / "database", V, V2)
    git(team.ada, "commit", "-q", "-am", "edit v in the repo")
    git(team.ada, "push", "-q")
    team.db[V] = V3  # Bob's edit in SSMS
    assert out(run(team.bob, "status"))["changes"][0]["origin"] == "unknown"
    commit = out(run(team.bob, "commit", "-m", "bob"))
    # Without a sync record "unknown" would be committed; the pull just changed this
    # file, which proves the repo moved, so it is held back instead.
    assert commit["git"]["pulled"] == 1 and commit["git"]["pulled_files"] == [team.view_path(V)]
    assert commit["commit"] is None and "pull just brought this file in" in commit["held_back"][0]["reason"]
    assert team.file(team.bob, V) == V2
    # A pull that touched an unrelated file does not hold a genuine database change back.
    team.db[W] = W1
    (team.ada / "notes.md").write_text("x\n")
    git(team.ada, "add", "notes.md")
    git(team.ada, "commit", "-q", "-m", "notes")
    git(team.ada, "push", "-q")
    later = out(run(team.bob, "commit", "-m", "bob", "--force"))
    assert later["git"]["pulled"] == 1 and [f["object"] for f in later["files"]] == ["[dbo].[v]", "[dbo].[w]"]


def test_a_teammates_pushed_object_is_applied_not_deleted(team):
    team.db[V] = V1
    run(team.ada, "commit", "-m", "baseline", "--push")
    team.pull(team.bob)
    team.db[W] = W1
    run(team.ada, "commit", "-m", "add w", "--push")
    # Bob has never synced dbo.w. Without the pull his commit would not see it; after the
    # pull the file is new in the repo, and commit leaves it for get-latest instead of
    # deleting it as "gone from the database".
    del team.db[W]  # dev does not have it yet (say Ada's dev write was rolled back)
    commit = out(run(team.bob, "commit", "-m", "bob"))
    assert commit["commit"] is None
    assert commit["held_back"][0]["object"] == "[dbo].[w]" and commit["held_back"][0]["origin"] == REPO
    assert team.file(team.bob, W) == W1


# --- git states that used to lose or mangle work ---------------------------------------


def test_a_diverged_branch_stops_the_commit_before_anything_is_written(team):
    team.db[V] = V1
    run(team.ada, "commit", "-m", "baseline", "--push")
    team.pull(team.bob)
    (team.bob / "notes.md").write_text("bob's notes\n")
    git(team.bob, "add", "notes.md")
    git(team.bob, "commit", "-q", "-m", "notes")  # Bob's own, unpushed
    team.db[W] = W1
    run(team.ada, "commit", "-m", "add w", "--push")
    team.db[V] = V2
    refused = run(team.bob, "commit", "-m", "bob")
    assert refused.exit_code != 0
    error = out(refused)["error"]
    assert error["kind"] == "git" and "cannot fast-forward" in error["message"] and "git pull" in error["message"]
    assert team.file(team.bob, V) == V1  # nothing written
    assert "commit" not in out(refused) or out(refused).get("commit") is None
    # --no-pull commits anyway; the push is then rejected with the way out, the commit stands.
    local = out(run(team.bob, "commit", "-m", "bob", "--no-pull", "--push"))
    assert local["commit"] and local["pushed"] is None
    assert "git pull" in local["push_error"] and "rejected" in local["push_error"]


def test_a_detached_head_a_merge_in_progress_or_no_identity_stops_the_commit(team, tmp_path):
    team.db[V] = V1
    run(team.ada, "commit", "-m", "baseline", "--push")
    team.pull(team.bob)
    team.db[V] = V2
    # Detached HEAD: a commit would dangle.
    git(team.bob, "checkout", "-q", "--detach")
    detached = run(team.bob, "commit", "-m", "bob")
    assert detached.exit_code != 0 and "detached" in out(detached)["error"]["message"]
    assert out(run(team.bob, "status"))["git"]["detached"] is True
    git(team.bob, "checkout", "-q", "main")
    # A merge in progress with a conflicted file.
    (team.ada / "notes.md").write_text("ada\n")
    git(team.ada, "add", "notes.md")
    git(team.ada, "commit", "-q", "-m", "ada notes")
    git(team.ada, "push", "-q")
    (team.bob / "notes.md").write_text("bob\n")
    git(team.bob, "add", "notes.md")
    git(team.bob, "commit", "-q", "-m", "bob notes")
    git(team.bob, "pull", "-q", "--no-rebase", check=False)
    status = out(run(team.bob, "status"))
    assert status["git"]["in_progress"] == "merge" and status["git"]["unmerged"] == ["notes.md"]
    assert any("not ready for a commit" in w for w in status["warnings"])
    merging = run(team.bob, "commit", "-m", "bob", "--no-pull")
    assert merging.exit_code != 0 and "merge is in progress" in out(merging)["error"]["message"]
    git(team.bob, "merge", "--abort")
    # No identity: git could not record who committed.
    carol = team.clone(tmp_path / "carol", "", "")
    git(carol, "config", "--unset", "user.email")
    git(carol, "config", "--unset", "user.name")
    nobody = run(carol, "commit", "-m", "carol")
    assert nobody.exit_code != 0 and "user.email" in out(nobody)["error"]["message"]
    assert team.file(carol, V) == V1  # nothing written before the refusal


def test_a_half_merged_sql_file_is_left_out_until_it_is_resolved(team):
    team.db[V] = V1
    team.db[W] = W1
    run(team.ada, "commit", "-m", "baseline", "--push")
    team.pull(team.bob)
    marked = "CREATE VIEW dbo.v AS\n<<<<<<< HEAD\nSELECT 2 AS n;\n=======\nSELECT 3 AS n;\n>>>>>>> origin/main\nGO\n"
    (team.bob / "database" / team.view_path(V)).write_text(marked)
    team.db[W] = W2
    status = out(run(team.bob, "status"))
    assert status["conflicted"] == [team.view_path(V)]
    assert [c["object"] for c in status["changes"]] == ["[dbo].[w]"]
    assert any("conflict markers" in w for w in status["warnings"])
    # get-latest would otherwise run the markers as SQL; script would overwrite the merge.
    plan = out(run(team.bob, "get-latest", "--dry-run"))
    assert "<<<<<<<" not in (plan["script"] or "") and "dbo.v" not in (plan["script"] or "")
    out(run(team.bob, "script"))
    assert (team.bob / "database" / team.view_path(V)).read_text() == marked
    commit = out(run(team.bob, "commit", "-m", "w only"))
    assert [f["object"] for f in commit["files"]] == ["[dbo].[w]"]


def test_other_uncommitted_work_is_left_exactly_as_it_was(team):
    team.db[V] = V1
    run(team.ada, "commit", "-m", "baseline", "--push")
    team.pull(team.bob)
    (team.bob / "README.md").write_text("work in progress\n")
    git(team.bob, "add", "README.md")  # staged, but not sqlsc's business
    (team.bob / "database" / "scratch.sql").write_text("-- not an object file\n")
    team.db[V] = V2
    commit = out(run(team.bob, "commit", "-m", "v2"))
    assert commit["commit"]
    porcelain = git(team.bob, "status", "--porcelain").stdout
    assert "A  README.md" in porcelain and "?? database/scratch.sql" in porcelain
    assert git(team.bob, "show", "--stat", "--format=", "HEAD").stdout.count(".sql") == 1


def test_a_pull_that_would_overwrite_a_hand_edit_is_refused_with_the_way_out(team):
    team.db[V] = V1
    run(team.ada, "commit", "-m", "baseline", "--push")
    team.pull(team.bob)
    write_object(team.ada / "database", V, V2)
    git(team.ada, "commit", "-q", "-am", "v2")
    git(team.ada, "push", "-q")
    write_object(team.bob / "database", V, V3)  # Bob's uncommitted hand edit of the same file
    team.db[W] = W1
    refused = run(team.bob, "commit", "-m", "bob")
    assert refused.exit_code != 0
    message = out(refused)["error"]["message"]
    assert "did not fast-forward" in message and "stash" in message
    assert team.file(team.bob, V) == V3  # the hand edit survived


def test_two_sqlsc_runs_at_once_wait_for_gits_index_lock(team):
    team.db[V] = V1
    lock = team.ada / ".git" / "index.lock"
    lock.write_text("")

    def let_go():
        time.sleep(0.5)
        lock.unlink()

    threading.Thread(target=let_go).start()
    assert out(run(team.ada, "commit", "-m", "v1"))["commit"]


def test_line_endings_do_not_count_as_a_change(team):
    team.db[V] = V1
    run(team.ada, "commit", "-m", "baseline", "--push")
    path = team.ada / "database" / team.view_path(V)
    path.write_bytes(V1.replace("\n", "\r\n").encode("utf-8"))
    assert out(run(team.ada, "status"))["changes"] == []
    team.db[V] = V1.replace("\n", "\r\n")
    path.write_text(V1)
    assert out(run(team.ada, "status"))["changes"] == []


# --- #43 a half-merged file stays checked out through a check-in --------------------------


def test_checkin_keeps_a_marker_conflicted_object_checked_out(team):
    team.db[V] = V1
    team.db[W] = W1
    assert run(team.ada, "commit", "-m", "baseline", "--push").exit_code == 0
    assert run(team.ada, "checkout", "dbo.v", "dbo.w").exit_code == 0
    marked = "CREATE VIEW dbo.v AS\n<<<<<<< HEAD\nSELECT 2 AS n;\n=======\nSELECT 3 AS n;\n>>>>>>> origin/main\nGO\n"
    (team.ada / "database" / team.view_path(V)).write_text(marked, encoding="utf-8")
    team.db[W] = W2  # a clean change next to it
    result = run(team.ada, "checkin", "--no-pull", "-m", "w")
    payload = out(result)
    # The clean object is committed and released; the half-merged one is neither.
    assert result.exit_code == 1 and payload["ok"] is False
    assert [f["object"] for f in payload["files"]] == ["[dbo].[w]"] and payload["commit"]
    assert [r["object"] for r in payload["released"]] == ["[dbo].[w]"]
    assert [h["object"] for h in payload["held_back"]] == ["[dbo].[v]"]
    assert payload["held_back"][0]["action"] == "conflict" and "conflict markers" in payload["held_back"][0]["reason"]
    assert team.locked_by(V) == ADA and team.locked_by(W) is None
    assert team.file(team.ada, V) == marked, "the file is left for the person to resolve"
    # Only conflicts: nothing is committed or released.
    again = out(run(team.ada, "checkin", "--no-pull"))
    assert again["ok"] is False and again["commit"] is None and again["released"] == []
    assert team.locked_by(V) == ADA
    # --no-commit is the deliberate undo: it releases on purpose.
    assert run(team.ada, "checkin", "--no-commit").exit_code == 0
    assert team.locked_by(V) is None


# --- #44 unknown checkout ownership is not "nobody has anything checked out" -------------


def test_a_commit_stops_when_the_checkouts_cannot_be_read(team, monkeypatch):
    team.db[V] = V1
    assert run(team.ada, "commit", "-m", "baseline", "--push").exit_code == 0
    team.pull(team.bob)
    assert run(team.ada, "checkout", "dbo.v").exit_code == 0
    team.db[V] = V2  # Ada's unfinished edit in dev
    team.db[W] = W1  # Bob's own new object

    def unavailable(repo=None):
        raise RuntimeError("SELECT permission was denied on the object 'ObjectLock'")

    monkeypatch.setattr(team.locks, "list", unavailable)
    head = team.head(team.bob)
    result = run(team.bob, "commit", "-m", "bob")
    payload = out(result)
    assert result.exit_code == 2 and payload["ok"] is False and payload["error"]["kind"] == "locks"
    assert "Checkouts could not be read" in payload["error"]["message"] and "--force" in payload["error"]["message"]
    assert team.head(team.bob) == head and team.file(team.bob, V) == V1 and team.file(team.bob, W) is None
    assert team.locked_by(V) == ADA
    # Status still works and says why the checkouts are unknown, without claiming there are none.
    status = out(run(team.bob, "status"))
    assert status["locks"] == [] and any("Checkouts could not be read" in w for w in status["warnings"])
    # Nothing to commit: no refusal needed, the repo matches.
    team.db[V] = V1
    del team.db[W]
    assert out(run(team.bob, "commit", "-m", "nothing"))["commit"] is None
    # --force is the deliberate way past it.
    team.db[W] = W1
    forced = out(run(team.bob, "commit", "-m", "bob", "--force"))
    assert forced["commit"] and [f["object"] for f in forced["files"]] == ["[dbo].[w]"]


def test_a_bad_lock_target_stops_the_commit_too(team):
    team.db[V] = V1
    assert run(team.ada, "commit", "-m", "baseline", "--push").exit_code == 0
    with (team.ada / "sqlsc.yml").open("a", encoding="utf-8") as f:
        f.write("locks:\n  target: nowhere\n")
    team.db[V] = V2
    result = run(team.ada, "commit", "-m", "edit")
    payload = out(result)
    assert result.exit_code == 2 and payload["error"]["kind"] == "locks" and "nowhere" in payload["error"]["message"]
    assert team.file(team.ada, V) == V1
    status = out(run(team.ada, "status"))
    assert any("nowhere" in w for w in status["warnings"])


# --- who prepared a change: the commit says when an agent did -----------------------------


def message(clone: Path) -> str:
    return git(clone, "log", "-1", "--format=%B").stdout


def trailers(clone: Path) -> list[str]:
    done = subprocess.run(
        ["git", "interpret-trailers", "--parse"], cwd=clone, input=message(clone), capture_output=True, text=True
    )
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def test_a_commit_coop_prepares_carries_a_prepared_by_trailer_and_the_person_stays_the_author(team):
    team.db[V] = V1
    done = out(run(team.ada, "commit", "-m", "add dbo.v", "--as", COOP))
    assert done["commit"] and done["prepared_by"] == ["coop-agent"]
    assert trailers(team.ada) == ["Prepared-by: coop-agent"]
    assert git(team.ada, "log", "-1", "--format=%ae").stdout.strip() == ADA
    # --prepared-by carries the agent's version (coop-agent passes its own).
    team.db[V] = V2
    done = out(run(team.ada, "commit", "-m", "change dbo.v", "--as", COOP, "--prepared-by", "coop-agent 0.38.6"))
    assert done["prepared_by"] == ["coop-agent 0.38.6"]
    assert trailers(team.ada) == ["Prepared-by: coop-agent 0.38.6"]
    assert message(team.ada).startswith("change dbo.v\n\nPrepared-by: coop-agent 0.38.6")
    # A person's own commit says nothing: no agent, no trailer.
    team.db[V] = V3
    done = out(run(team.ada, "commit", "-m", "change dbo.v again"))
    assert done["commit"] and done["prepared_by"] == []
    assert trailers(team.ada) == []
    # Nothing to commit: no trailer claimed either.
    assert out(run(team.ada, "commit", "-m", "nothing", "--as", COOP))["prepared_by"] == []


def test_checking_in_coops_checkout_marks_the_commit_as_prepared_by_coop(team):
    team.db[V] = V1
    team.db[W] = W1
    run(team.ada, "commit", "-m", "baseline", "--push")
    assert out(run(team.ada, "checkout", "dbo.v", "--as", COOP))["ok"]
    assert out(run(team.ada, "checkout", "dbo.w"))["ok"]
    team.db[V] = V2
    team.db[W] = W2
    # Ada reviews coop's change and checks both objects in herself: the commit names coop
    # for the object it prepared, and Ada remains the author.
    done = out(run(team.ada, "checkin", "-m", "ada checks in"))
    assert done["commit"] and done["prepared_by"] == ["coop-agent"]
    assert trailers(team.ada) == ["Prepared-by: coop-agent"]
    assert team.locked_by(V) is None and team.locked_by(W) is None
    # Her own checkout alone: no trailer.
    assert out(run(team.ada, "checkout", "dbo.w"))["ok"]
    team.db[W] = W1
    done = out(run(team.ada, "checkin", "-m", "ada alone"))
    assert done["commit"] and done["prepared_by"] == []
    assert trailers(team.ada) == []
