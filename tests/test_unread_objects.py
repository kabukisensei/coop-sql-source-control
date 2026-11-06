"""An object the database has but could not give sqlsc is unknown, not dropped.

#77: a module the catalog lists whose definition comes back NULL (WITH ENCRYPTION, or a
login without VIEW DEFINITION) or not at all. #78: a tracked static-data table that is
still there but whose rows cannot be read as static data (no key now, a column type
static data cannot hold). Either way the file, HEAD and the sync record stay as they
are until a healthy read, and a genuine drop is still a deletion.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from coop_sqlsc import catalog, data
from coop_sqlsc import cli as cli_mod
from coop_sqlsc.compare import DELETED, comparable, compare
from coop_sqlsc.config import Target
from coop_sqlsc.objects import ObjectKey
from coop_sqlsc.sync import SyncRecord

from .test_unread_catalog import HEALTHY, fake_connection

TARGET = Target(name="dev", kind="sql_server", server="", database="Fixture", auth="entra")
VIEW = ObjectKey("view", "dbo", "vT")
DATA = ObjectKey("data", "dbo", "T")
VIEW_ROW = (2, "CREATE VIEW dbo.vT AS SELECT Id FROM dbo.T", True, True)


def without_view_definition(rows: dict, how: str) -> dict:
    """The catalog still lists dbo.vT; its definition is NULL or has no row."""
    modules = [r for r in rows[catalog.MODULES_SQL] if r[0] != 2]
    if how == "null":
        modules.append((2, None, True, True))
    return {**rows, catalog.MODULES_SQL: modules}


# --- the catalog read --------------------------------------------------------------------


@pytest.mark.parametrize("how", ["null", "missing"])
def test_a_listed_module_without_a_definition_is_unread(how):
    result = catalog.read_database(fake_connection(without_view_definition(HEALTHY, how)), TARGET)
    assert VIEW not in result.objects
    assert list(result.unread_objects) == [VIEW]
    assert "[dbo].[vT]" in result.unread_objects[VIEW]
    # Nothing claims encryption as a fact: NULL can also be a missing permission.
    assert "VIEW DEFINITION" in result.unread_objects[VIEW] or "no definition row" in result.unread_objects[VIEW]
    assert result.unread == {}
    assert ObjectKey("table", "dbo", "T") in result.objects


def test_a_healthy_read_has_no_unread_objects():
    result = catalog.read_database(fake_connection(HEALTHY), TARGET)
    assert VIEW in result.objects and result.unread_objects == {}


def test_compare_keeps_the_record_of_a_held_object():
    sync = SyncRecord(None)
    sync.record(VIEW, comparable(VIEW, "CREATE VIEW dbo.vT AS SELECT 1 AS Id;\nGO\n"))
    assert compare({}, {}, sync, held={VIEW}) == []
    assert VIEW in sync
    compare({}, {}, sync)
    assert VIEW not in sync, "an object gone from both sides, and not held, is forgotten"


# --- static data: what is absent, what is unread -------------------------------------------


class DataConn:
    """Answers the static-data queries for one table."""

    def __init__(self, columns, keys):
        self.columns, self.keys = columns, keys

    def cursor(self):
        return self

    def execute(self, sql, *params):
        self.sql = sql
        return self

    def fetchall(self):
        if self.sql == data.COLUMNS_SQL:
            return self.columns
        if self.sql == data.KEY_SQL:
            return self.keys
        return [("1",)]


INT_ID = ("Id", "int", 4, 10, 0, False, False)
PK = ("PK", "PK_T", "Id")


@pytest.mark.parametrize(
    "columns, keys, why",
    [
        ([INT_ID], [], "no primary key"),
        ([INT_ID, ("Shape", "geometry", -1, 0, 0, False, False)], [PK], "geometry"),
    ],
)
def test_a_present_table_that_cannot_be_read_as_data_is_unread(columns, keys, why):
    into: dict = {}
    unread: dict = {}
    warnings = cli_mod._read_static_data(DataConn(columns, keys), TARGET, [DATA], into, unread=unread)
    assert into == {} and warnings == []
    assert list(unread) == [DATA] and why in unread[DATA]


def test_a_missing_table_is_absent_not_unread():
    into: dict = {}
    unread: dict = {}
    cli_mod._read_static_data(DataConn([], []), TARGET, [DATA], into, unread=unread)
    assert into == {} and unread == {}


# --- through the CLI ------------------------------------------------------------------------


def run(root, *args: str):
    return CliRunner().invoke(cli_mod.main, ["--config", str(root / "sqlsc.yml"), *args])


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.rows = dict(HEALTHY)
        self.data_columns = [INT_ID]
        self.data_keys = [PK]
        self.executed: list[str] = []

    def __truediv__(self, other: str) -> Path:
        return self.path / other

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.path, check=True, capture_output=True, text=True).stdout.strip()

    def sync_has(self, key: ObjectKey) -> bool:
        return key in SyncRecord.load(self.path, "dev")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    fixture = Repo(tmp_path)

    class Conn:
        autocommit = True

        def cursor(self):
            return self

        def execute(self, sql, *params):
            self.sql = sql
            fixture.executed.append(sql)
            if sql.startswith("CREATE VIEW dbo.vNew"):
                fixture.rows = {
                    **fixture.rows,
                    catalog.OBJECTS_SQL: [*fixture.rows[catalog.OBJECTS_SQL], (3, "dbo", "vNew", "V")],
                    catalog.MODULES_SQL: [*fixture.rows[catalog.MODULES_SQL], (3, sql, True, True)],
                }
            return self

        def fetchone(self):
            return (0,)

        def fetchall(self):
            if self.sql == data.COLUMNS_SQL:
                return fixture.data_columns
            if self.sql == data.KEY_SQL:
                return fixture.data_keys
            if self.sql.startswith("SELECT CAST([Id]"):
                return [("1",), ("2",)]
            return []

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli_mod, "open_connection", lambda target: Conn())
    monkeypatch.setattr(
        cli_mod, "read_database", lambda conn, target: catalog.read_database(fake_connection(fixture.rows), target)
    )
    run(tmp_path, "init", "--layout", "type", "--kind", "sql_server", "--server", "s", "--database", "Fixture")
    # Track dbo.T's rows: the Data file is the switch.
    columns, key_columns = data.track_columns(Conn(), DATA)
    text = data.render(DATA, columns, key_columns, [("1",), ("2",)])
    (tmp_path / "database" / "Data").mkdir(parents=True)
    (tmp_path / "database" / "Data" / "dbo.T.sql").write_text(text, encoding="utf-8")
    fixture.git("init", "-q")
    fixture.git("config", "user.email", "dev@example.com")
    fixture.git("config", "user.name", "Dev")
    first = run(tmp_path, "--json", "commit", "-m", "Baseline")
    assert first.exit_code == 0, first.output
    assert fixture.sync_has(VIEW) and fixture.sync_has(DATA)
    return fixture


def view_file(repo: Repo) -> Path:
    return repo / "database" / "Views" / "dbo.vT.sql"


def data_file(repo: Repo) -> Path:
    return repo / "database" / "Data" / "dbo.T.sql"


def assert_held(repo: Repo, key: ObjectKey, path: Path, before: str, head: str) -> None:
    assert path.read_text(encoding="utf-8") == before
    assert repo.git("rev-parse", "HEAD") == head
    assert repo.git("status", "--porcelain") == ""
    assert repo.sync_has(key), "the sync record must survive the unread run"


@pytest.mark.parametrize("how", ["null", "missing"])
def test_an_unread_view_is_held_by_every_command_then_recovers(repo, how):
    path = view_file(repo)
    before, head = path.read_text(encoding="utf-8"), repo.git("rev-parse", "HEAD")
    repo.rows = without_view_definition(HEALTHY, how)

    status = json.loads(run(repo, "--json", "status").output)
    assert status["unread"] == ["Views/dbo.vT.sql"]
    assert [c for c in status["changes"] if c["action"] == DELETED] == []
    assert any("[dbo].[vT] was not compared" in w for w in status["warnings"])
    assert_held(repo, VIEW, path, before, head)

    result = run(repo, "--json", "script")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["files"] == []
    assert_held(repo, VIEW, path, before, head)

    result = run(repo, "--json", "commit", "-m", "While unread")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["commit"] is None
    assert_held(repo, VIEW, path, before, head)

    result = run(repo, "--json", "get-latest")
    assert result.exit_code == 0, result.output
    assert not [s for s in repo.executed if "vT" in s], "nothing may be applied for the held view"
    assert_held(repo, VIEW, path, before, head)

    # A healthy read again: in sync, nothing to do, the record still there.
    repo.rows = dict(HEALTHY)
    status = json.loads(run(repo, "--json", "status").output)
    assert status["unread"] == [] and status["changes"] == []
    assert_held(repo, VIEW, path, before, head)


def test_a_repo_edit_to_an_unread_view_waits_for_a_healthy_read(repo):
    path = view_file(repo)
    path.write_text("CREATE VIEW dbo.vT AS SELECT Id, 1 AS One FROM dbo.T\nGO\n", encoding="utf-8")
    repo.rows = without_view_definition(HEALTHY, "null")
    result = run(repo, "--json", "get-latest", "--force")
    assert result.exit_code == 0, result.output
    assert not [s for s in repo.executed if "vT" in s]
    repo.rows = dict(HEALTHY)
    status = json.loads(run(repo, "--json", "status").output)
    assert [(c["object"], c["origin"]) for c in status["changes"]] == [("[dbo].[vT]", "repo")]


def test_a_view_stays_held_through_an_update_that_applies_other_objects(repo):
    path = view_file(repo)
    (repo / "database" / "Views" / "dbo.vNew.sql").write_text(
        "CREATE VIEW dbo.vNew AS SELECT 1 AS One\nGO\n", encoding="utf-8"
    )
    repo.rows = without_view_definition(HEALTHY, "null")
    result = run(repo, "--json", "get-latest")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["done"] == ["[dbo].[vNew]"]
    assert payload["still_different"] == []
    assert path.is_file() and repo.sync_has(VIEW)


def test_a_dropped_view_is_still_a_deletion(repo):
    path = view_file(repo)
    repo.rows = {
        **HEALTHY,
        catalog.OBJECTS_SQL: [r for r in HEALTHY[catalog.OBJECTS_SQL] if r[2] != "vT"],
        catalog.MODULES_SQL: [r for r in HEALTHY[catalog.MODULES_SQL] if r[0] != 2],
    }
    status = json.loads(run(repo, "--json", "status").output)
    assert status["unread"] == []
    assert [(c["object"], c["action"], c["origin"]) for c in status["changes"]] == [("[dbo].[vT]", DELETED, "database")]
    result = run(repo, "--json", "commit", "-m", "Drop vT")
    assert result.exit_code == 0, result.output
    assert not path.exists()
    assert "Views/dbo.vT.sql" not in repo.git("ls-files")
    assert not repo.sync_has(VIEW)


@pytest.mark.parametrize("change", ["key", "type"])
def test_unreadable_static_data_is_held_then_recovers(repo, change):
    path = data_file(repo)
    before, head = path.read_text(encoding="utf-8"), repo.git("rev-parse", "HEAD")
    if change == "key":
        repo.data_keys = []
    else:
        repo.data_columns = [INT_ID, ("Shape", "geometry", -1, 0, 0, False, False)]

    status = json.loads(run(repo, "--json", "status").output)
    assert status["unread"] == ["Data/dbo.T.sql"]
    assert status["changes"] == []
    assert any("static data could not be read" in w for w in status["warnings"])
    assert_held(repo, DATA, path, before, head)

    assert json.loads(run(repo, "--json", "script").output)["files"] == []
    assert_held(repo, DATA, path, before, head)
    assert json.loads(run(repo, "--json", "commit", "-m", "While unread").output)["commit"] is None
    assert_held(repo, DATA, path, before, head)
    result = run(repo, "--json", "get-latest")
    assert result.exit_code == 0, result.output
    assert not [s for s in repo.executed if s.startswith("MERGE") or "Static data" in s]
    assert_held(repo, DATA, path, before, head)

    repo.data_keys, repo.data_columns = [PK], [INT_ID]
    status = json.loads(run(repo, "--json", "status").output)
    assert status["unread"] == [] and status["changes"] == []
    assert_held(repo, DATA, path, before, head)


def test_data_remove_still_stops_tracking_an_unreadable_table(repo):
    repo.data_keys = []
    result = run(repo, "--json", "data", "remove", "dbo.T")
    assert result.exit_code == 0, result.output
    assert not data_file(repo).exists()
    result = run(repo, "--json", "commit", "-m", "Stop tracking dbo.T")
    assert result.exit_code == 0, result.output
    assert "Data/dbo.T.sql" not in repo.git("ls-files")


def test_a_dropped_table_still_removes_its_static_data(repo):
    repo.data_columns = []  # sys.columns has nothing for it: the table is gone
    status = json.loads(run(repo, "--json", "status").output)
    assert status["unread"] == []
    assert ("[dbo].[T]", "data", DELETED) in [(c["object"], c["type"], c["action"]) for c in status["changes"]]
    assert run(repo, "script").exit_code == 0
    assert not data_file(repo).exists()
