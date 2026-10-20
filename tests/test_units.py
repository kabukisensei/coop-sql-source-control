"""Offline tests: no database needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from coop_sqlsc import cli as cli_mod
from coop_sqlsc.catalog import CatalogResult
from coop_sqlsc.compare import CHANGED, DELETED, NEW, compare, matches
from coop_sqlsc.config import ConfigError, build_target, load
from coop_sqlsc.connect import connection_string, pack_token, pick_driver
from coop_sqlsc.layout import key_for_path, read_folder, relative_path, write_object
from coop_sqlsc.objects import ObjectKey, normalize
from coop_sqlsc.scripter import Column, ForeignKey, KeyConstraint, Table, quote, script_module, script_table, type_text

# --- layout -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        ObjectKey("table", "dbo", "Customer"),
        ObjectKey("view", "sales.eu", "v:Totals?"),
        ObjectKey("procedure", "dbo", "usp 100%"),
        ObjectKey("schema", "", "Finance"),
        ObjectKey("function", "dbo", "ends with dot."),
    ],
)
def test_every_name_maps_to_one_file_and_back(key):
    path = relative_path(key)
    assert not any(ch in path.split("/")[-1] for ch in '<>:"\\|?*')
    assert key_for_path(path) == key


def test_layout_matches_redgate_folders():
    assert relative_path(ObjectKey("procedure", "dbo", "usp_Load")) == "Stored Procedures/dbo.usp_Load.sql"
    assert relative_path(ObjectKey("schema", "", "sales")) == "Security/Schemas/sales.sql"


def test_read_folder_ignores_other_files_and_flags_misnamed_ones(tmp_path):
    write_object(tmp_path, ObjectKey("view", "dbo", "v1"), "CREATE VIEW dbo.v1 AS SELECT 1;\r\nGO  \r\n")
    (tmp_path / "README.md").write_text("hi")
    (tmp_path / "Views" / "no_schema.sql").write_text("x")
    objects, unknown = read_folder(tmp_path)
    assert objects == {ObjectKey("view", "dbo", "v1"): "CREATE VIEW dbo.v1 AS SELECT 1;\nGO\n"}
    assert unknown == ["Views/no_schema.sql"]


# --- scripter -------------------------------------------------------------------------


def test_quote_only_when_needed():
    assert quote("Customer") == "Customer"
    assert quote("Order") == "[Order]"
    assert quote("Customer Name") == "[Customer Name]"
    assert quote("a]b") == "[a]]b]"


@pytest.mark.parametrize(
    ("col", "expected"),
    [
        (Column("a", "nvarchar", max_length=200), "nvarchar(100)"),
        (Column("a", "nvarchar", max_length=-1), "nvarchar(max)"),
        (Column("a", "varbinary", max_length=16), "varbinary(16)"),
        (Column("a", "decimal", precision=19, scale=4), "decimal(19, 4)"),
        (Column("a", "datetime2", scale=7), "datetime2(7)"),
        (Column("a", "int"), "int"),
        (Column("a", "Phone", type_schema="dbo", user_type=True), "dbo.Phone"),
    ],
)
def test_type_text(col, expected):
    assert type_text(col) == expected


def test_fabric_keys_are_not_enforced():
    table = Table(
        "dbo",
        "Fact",
        columns=[Column("Id", "bigint", nullable=False), Column("DimId", "int")],
        keys=[KeyConstraint("PK_Fact", True, "CLUSTERED", [("Id", False)])],
        foreign_keys=[ForeignKey("FK_Fact_Dim", ["DimId"], "dbo", "Dim", ["DimId"])],
    )
    text = script_table(table, database_collation=None, enforced_keys=False)
    assert "ADD CONSTRAINT PK_Fact PRIMARY KEY NONCLUSTERED (Id) NOT ENFORCED;" in text
    assert "REFERENCES dbo.Dim (DimId) NOT ENFORCED;" in text


def test_system_named_constraints_leave_their_name_out():
    table = Table(
        "dbo",
        "T",
        columns=[Column("Id", "int", nullable=False)],
        keys=[KeyConstraint(None, True, "CLUSTERED", [("Id", False)])],
    )
    assert "ALTER TABLE dbo.T ADD PRIMARY KEY CLUSTERED (Id);" in script_table(table, database_collation=None)


def test_module_set_options_only_when_off():
    assert script_module("CREATE VIEW v AS SELECT 1") == "CREATE VIEW v AS SELECT 1\nGO\n"
    assert script_module("CREATE VIEW v AS SELECT 1", quoted_identifier=False).startswith("SET QUOTED_IDENTIFIER OFF")


def test_normalize_ignores_line_endings_and_trailing_space():
    assert normalize("\r\n\r\nA  \r\nB\t\r\n\r\n") == "A\nB\n"


# --- compare ------------------------------------------------------------------------


def test_compare_names_directions_from_the_database_side():
    a, b, c = ObjectKey("view", "dbo", "a"), ObjectKey("view", "dbo", "b"), ObjectKey("table", "dbo", "c")
    changes = compare({a: "1\n", b: "2\n"}, {b: "3\n", c: "4\n"})
    assert [(ch.key, ch.action) for ch in changes] == [(c, DELETED), (a, NEW), (b, CHANGED)]
    assert "-3\n+2" in changes[2].unified_diff()


def test_object_name_matching():
    key = ObjectKey("table", "sales", "Order")
    assert all(matches(key, p) for p in ("sales.Order", "[sales].[Order]", "order", "SALES.ORDER"))
    assert not matches(key, "dbo.Order")


# --- config -------------------------------------------------------------------------


def test_coop_targets_merge_with_sqlsc_overrides(tmp_path):
    (tmp_path / ".coop").mkdir()
    (tmp_path / ".coop" / "project.yml").write_text(
        "sql_targets:\n  default_environment: dev\n"
        "  dev:\n    kind: fabric_warehouse\n    database: WH_Gold\n    workspace_id: abc\n"
        "  test:\n    kind: azure_sql\n    server: t.database.windows.net\n    database: Sales\n"
    )
    repo = tmp_path / "sql"
    repo.mkdir()
    (repo / "sqlsc.yml").write_text("folder: db\ntargets:\n  dev:\n    server: x.datawarehouse.fabric.microsoft.com\n")
    cfg = load(repo)
    assert cfg.default_target == "dev"
    dev = cfg.target()
    assert (dev.kind, dev.server, dev.database, dev.auth) == (
        "fabric_warehouse",
        "x.datawarehouse.fabric.microsoft.com",
        "WH_Gold",
        "entra",
    )
    assert dev.source == "coop project.yml + sqlsc.yml"
    assert cfg.target("test").source == "coop project.yml"
    assert cfg.scripts_dir == (repo / "db").resolve()


def test_fabric_target_without_server_explains_where_to_find_it():
    with pytest.raises(ConfigError, match="SQL connection string"):
        build_target("dev", {"kind": "fabric_lakehouse", "database": "LH"})


def test_unknown_kind_lists_the_valid_ones():
    with pytest.raises(ConfigError, match="azure_sql"):
        build_target("dev", {"kind": "oracle", "server": "s", "database": "d"})


# --- connect ------------------------------------------------------------------------


def test_driver_choice_needs_18_or_newer():
    assert (
        pick_driver(["ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"])
        == "ODBC Driver 18 for SQL Server"
    )
    assert pick_driver(["ODBC Driver 17 for SQL Server"]) is None


def test_connection_string_never_holds_a_secret():
    target = build_target("dev", {"kind": "azure_sql", "server": "s.database.windows.net", "database": "Sales"})
    text = connection_string(target, "ODBC Driver 18 for SQL Server")
    assert "Server=tcp:s.database.windows.net,1433" in text
    assert "PWD" not in text and "Encrypt=yes" in text
    assert pack_token("ab")[:4] == (4).to_bytes(4, "little")


# --- CLI, with the database faked ----------------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch):
    objects: dict[ObjectKey, str] = {}

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(cli_mod, "open_connection", lambda target: Conn())
    monkeypatch.setattr(cli_mod, "read_database", lambda conn, target: CatalogResult(dict(objects), []))
    return objects


def run(tmp_path: Path, *args: str):
    return CliRunner().invoke(cli_mod.main, ["--config", str(tmp_path / "sqlsc.yml"), *args])


def test_cli_init_status_script_round_trip(tmp_path, fake_db):
    result = run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".gitattributes").read_text() == "*.sql text diff\n"

    fake_db[ObjectKey("view", "dbo", "vSales")] = "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n"
    status = run(tmp_path, "--json", "status", "--exit-code")
    assert status.exit_code == 1
    assert json.loads(status.output)["summary"] == {"new": 1, "changed": 0, "deleted": 0}

    assert run(tmp_path, "script").exit_code == 0
    assert (tmp_path / "database" / "Views" / "dbo.vSales.sql").is_file()
    assert run(tmp_path, "status", "--exit-code").exit_code == 0


def _git(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_commit_takes_files_that_match_the_database_but_not_head(tmp_path, fake_db):
    # `script`, `data add`, or an edit applied with get-latest leave files that match the
    # database; commit must still commit them, not report "nothing to commit".
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")
    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    view = ObjectKey("view", "dbo", "vSales")
    fake_db[view] = "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n"
    assert run(tmp_path, "script").exit_code == 0

    first = run(tmp_path, "--json", "commit", "-m", "Script")
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["summary"] == {"new": 1, "changed": 0, "deleted": 0}
    assert _git(tmp_path, "show", "--name-only", "--format=", "HEAD") == "database/Views/dbo.vSales.sql"

    # Edited by hand and applied: the database matches the file, HEAD does not.
    fake_db[view] = "CREATE VIEW dbo.vSales AS SELECT 2;\nGO\n"
    (tmp_path / "database" / "Views" / "dbo.vSales.sql").write_text(fake_db[view], encoding="utf-8")
    second = json.loads(run(tmp_path, "--json", "commit").output)
    assert second["summary"] == {"new": 0, "changed": 1, "deleted": 0}
    assert "SELECT 2" in _git(tmp_path, "show", "HEAD:database/Views/dbo.vSales.sql")
    assert json.loads(run(tmp_path, "--json", "commit").output)["commit"] is None
    assert _git(tmp_path, "status", "--porcelain", "--", "database") == ""


def test_a_failed_push_keeps_the_commit_and_says_so(tmp_path, fake_db):
    # No remote: the push fails, but the commit happened and must be reported as done.
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")
    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    fake_db[ObjectKey("view", "dbo", "vSales")] = "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n"
    result = run(tmp_path, "--json", "commit", "--push", "-m", "Script")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["commit"] and payload["pushed"] is None
    assert payload["push_error"].startswith(f"Committed as {payload['commit']}, but the push failed")
    assert _git(tmp_path, "log", "--format=%s") == "Script"


def test_git_errors_name_the_reason_not_the_hint():
    from coop_sqlsc.git_ops import _reason

    push = (
        "fatal: 'C:/x/no-such-remote.git' does not appear to be a git repository\n"
        "fatal: Could not read from remote repository.\n\n"
        "Please make sure you have the correct access rights\nand the repository exists.\n"
    )
    assert _reason(push) == "'C:/x/no-such-remote.git' does not appear to be a git repository"
    assert _reason("error: pathspec 'x' did not match\n") == "pathspec 'x' did not match"
    assert _reason("something odd.\n") == "something odd"


def test_git_uncommitted_lists_added_modified_and_deleted(tmp_path):
    from coop_sqlsc import git_ops

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")
    folder = tmp_path / "db"
    (folder / "Views").mkdir(parents=True)
    (folder / "Views" / "dbo.a.sql").write_text("a")
    (folder / "Views" / "dbo.b.sql").write_text("b")
    (tmp_path / "outside.sql").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    (folder / "Views" / "dbo.a.sql").write_text("a2")
    (folder / "Views" / "dbo.b.sql").unlink()
    (folder / "Views" / "dbo.c d.sql").write_text("c")
    (tmp_path / "outside.sql").write_text("y")
    assert git_ops.uncommitted(tmp_path, folder) == {
        "Views/dbo.a.sql": "modified",
        "Views/dbo.b.sql": "deleted",
        "Views/dbo.c d.sql": "added",
    }


def test_fabric_connection_host_from_the_item():
    from coop_sqlsc.fabric_api import connection_host, item_path

    assert connection_host(
        "fabric_warehouse", {"properties": {"connectionString": "abc.datawarehouse.fabric.microsoft.com"}}
    ) == ("abc.datawarehouse.fabric.microsoft.com")
    lakehouse = {
        "properties": {"sqlEndpointProperties": {"connectionString": "tcp:xyz.datawarehouse.fabric.microsoft.com,1433"}}
    }
    assert connection_host("fabric_lakehouse", lakehouse) == "xyz.datawarehouse.fabric.microsoft.com"
    assert item_path("fabric_lakehouse", "w", "i").endswith("/workspaces/w/lakehouses/i")


def test_coop_fabric_targets_get_their_server_from_the_fabric_api(tmp_path, monkeypatch):
    from coop_sqlsc import fabric_api

    (tmp_path / ".coop").mkdir()
    (tmp_path / ".coop" / "project.yml").write_text(
        "fabric:\n  tenant_id: t-1\nsql_targets:\n  default_environment: dev\n"
        "  dev:\n    kind: fabric_warehouse\n    workspace_id: w-1\n    item_id: i-1\n    database: WH\n"
    )
    (tmp_path / "sqlsc.yml").write_text("folder: database\n")
    asked = []

    def lookup(kind, workspace_id, item_id, tenant_id=""):
        asked.append((kind, workspace_id, item_id, tenant_id))
        return "abc.datawarehouse.fabric.microsoft.com"

    monkeypatch.setattr(fabric_api, "lookup_server", lookup)
    target = load(tmp_path).target()
    assert target.server == "abc.datawarehouse.fabric.microsoft.com" and target.tenant_id == "t-1"
    assert asked == [("fabric_warehouse", "w-1", "i-1", "t-1")]


def test_static_data_compares_rows_not_their_order_or_n_prefix():
    def script(*rows: str) -> str:
        body = "\n".join(("              " if i == 0 else "             ,") + r for i, r in enumerate(rows))
        head = "MERGE INTO dbo.S AS target\nUSING\n(\n    SELECT\n    FROM\n    (\n        VALUES\n"
        return f"{head}{body}\n    ) AS v (id, label)\n) AS source\nGO\n"

    key = ObjectKey("data", "dbo", "S")
    db = script("(1, 'Open')", "(2, 'Gelöscht')")
    assert compare({key: db}, {key: script("(2, N'Gelöscht')", "(1, 'Open')")}) == []
    assert compare({key: db}, {key: script("(1, 'Open')", "(2, 'Geloscht')")})[0].action == CHANGED
    # An N' inside a quoted value is text, not a prefix.
    assert compare({key: script("(1, 'A N''x')")}, {key: script("(1, 'A ''x')")})[0].action == CHANGED


def test_token_failures_keep_the_reason_not_the_whole_credential_chain():
    from coop_sqlsc.connect import token_reason

    chain = (
        "DefaultAzureCredential failed to retrieve a token from the included credentials.\n"
        "Attempted credentials:\n\tEnvironmentCredential: unavailable.\n"
        "\tAzureCliCredential: ERROR: The command failed with an unexpected error. Here is the traceback:\n"
        "ERROR: AADSTS700082: The refresh token has expired due to inactivity.\n"
        'Traceback (most recent call last):\n  File "x.py", line 1\n' * 3
    )
    reason = token_reason(Exception(chain))
    assert reason.startswith("DefaultAzureCredential failed")
    assert "AADSTS700082: The refresh token has expired" in reason and "Traceback" not in reason


def test_a_connect_that_never_answers_gives_up_at_the_deadline(monkeypatch):
    import threading
    import time

    from coop_sqlsc.connect import ConnectTimeout, connect_deadline, connect_within

    never = threading.Event()
    start = time.monotonic()
    with pytest.raises(ConnectTimeout):
        connect_within(lambda: never.wait(), 1)
    assert time.monotonic() - start < 5
    assert connect_within(lambda: "conn", 5) == "conn"
    with pytest.raises(ValueError):
        connect_within(lambda: (_ for _ in ()).throw(ValueError("login failed")), 5)

    monkeypatch.setenv("SQLSC_CONNECT_TIMEOUT", "30")
    assert connect_deadline() == 30
    monkeypatch.setenv("SQLSC_CONNECT_TIMEOUT", "soon")
    assert connect_deadline() == 90


def test_a_stalled_login_is_retried_once(monkeypatch):
    import sys
    import threading
    import types

    from coop_sqlsc import connect as connect_mod

    calls = []
    never = threading.Event()

    def fake_connect(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            never.wait()  # the first login never answers
        return "conn"

    fake = types.SimpleNamespace(pooling=True, Error=Exception, drivers=lambda: ["ODBC Driver 18 for SQL Server"])
    fake.connect = fake_connect
    monkeypatch.setitem(sys.modules, "pyodbc", fake)
    monkeypatch.setenv("SQLSC_CONNECT_TIMEOUT", "5")
    target = build_target("dev", {"kind": "sql_server", "server": "localhost", "database": "D", "auth": "windows"})
    assert connect_mod.open_connection(target) == "conn" and len(calls) == 2


def test_pooling_is_off_before_pyodbc_creates_its_environment(monkeypatch):
    # pyodbc reads `pooling` once, at its first call (drivers() here); set later, it does
    # nothing, and the driver manager's COM+ context check can hang every connect.
    import sys
    import types

    from coop_sqlsc import connect as connect_mod

    seen = []

    class FakePyodbc(types.ModuleType):
        Error = Exception

        def __setattr__(self, name, value):
            if name == "pooling":
                seen.append(("pooling", value))
            super().__setattr__(name, value)

        def drivers(self):
            seen.append(("drivers", None))
            return ["ODBC Driver 18 for SQL Server"]

        def connect(self, *args, **kwargs):
            seen.append(("connect", None))
            return object()

    monkeypatch.setitem(sys.modules, "pyodbc", FakePyodbc("pyodbc"))
    target = build_target("dev", {"kind": "sql_server", "server": "localhost", "database": "D", "auth": "windows"})
    connect_mod.open_connection(target)
    assert seen[0] == ("pooling", False) and seen.index(("drivers", None)) > 0


def test_cli_errors_are_json_for_agents(tmp_path):
    result = CliRunner().invoke(cli_mod.main, ["--json", "--config", str(tmp_path / "missing.yml"), "status"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["error"]["kind"] == "config"


# --- get-latest planning ------------------------------------------------------------

from coop_sqlsc.compare import Change  # noqa: E402
from coop_sqlsc.deploy import ManualChange, build_plan, table_changes, to_alter  # noqa: E402

TABLE = """CREATE TABLE dbo.T
(
      Id int IDENTITY(1, 1) NOT NULL
     ,Name nvarchar(50) NOT NULL CONSTRAINT DF_T_Name DEFAULT ('')
     ,Old int NULL
);
GO
ALTER TABLE dbo.T ADD CONSTRAINT PK_T PRIMARY KEY CLUSTERED (Id);
GO
"""


def test_to_alter_skips_comments_and_strings():
    text = "-- CREATE VIEW in a comment\n/* CREATE PROC x */\nCREATE   VIEW dbo.v AS SELECT 'CREATE VIEW' AS a"
    assert to_alter(text).endswith("ALTER   VIEW dbo.v AS SELECT 'CREATE VIEW' AS a")
    assert to_alter("create or alter procedure p as select 1").startswith("ALTER procedure")


def test_table_changes_alter_in_place():
    repo = TABLE.replace("nvarchar(50) NOT NULL CONSTRAINT DF_T_Name DEFAULT ('')", "nvarchar(80) NOT NULL") + (
        "CREATE NONCLUSTERED INDEX IX_T_Name ON dbo.T (Name);\nGO\n"
    )
    repo = repo.replace("     ,Old int NULL\n", "     ,New date NULL\n")
    key = ObjectKey("table", "dbo", "T")
    held, notes = table_changes(key, TABLE, repo, allow_drops=False)
    assert "ALTER TABLE dbo.T ADD New date NULL" in held
    assert "ALTER TABLE dbo.T ALTER COLUMN Name nvarchar(80) NOT NULL" in held
    assert held[-1] == "CREATE NONCLUSTERED INDEX IX_T_Name ON dbo.T (Name);"
    assert any("DROP CONSTRAINT" in s and "sys.default_constraints" in s for s in held)
    assert notes == ["column Old is not in the repo; pass --allow-drops to drop it"]
    dropped, _ = table_changes(key, TABLE, repo, allow_drops=True)
    assert "ALTER TABLE dbo.T DROP COLUMN Old" in dropped


def test_identity_change_needs_a_migration():
    with pytest.raises(ManualChange, match="IDENTITY"):
        table_changes(ObjectKey("table", "dbo", "T"), TABLE, TABLE.replace("IDENTITY(1, 1) ", ""), allow_drops=True)


def test_plan_orders_creates_and_holds_back_drops():
    t = ObjectKey("table", "dbo", "T")
    v = ObjectKey("view", "dbo", "v")
    gone = ObjectKey("procedure", "dbo", "p")
    plan = build_plan(
        [
            Change(v, "deleted", None, "CREATE VIEW dbo.v AS SELECT 1 AS a\nGO\n"),
            Change(t, "deleted", None, TABLE),
            Change(gone, "new", "CREATE PROCEDURE dbo.p AS SELECT 1\nGO\n", None),
        ],
        allow_drops=False,
    )
    assert [s.key for s in sorted(plan.runnable, key=lambda s: s.phase)] == [t, v]
    assert [s.key for s in plan.held_back] == [gone]


# --- sync record: which side moved -------------------------------------------------

from coop_sqlsc.sync import BOTH, DATABASE, REPO, UNKNOWN, SyncRecord  # noqa: E402


def test_sync_record_tells_dropped_from_not_yet_applied(tmp_path):
    view, proc = ObjectKey("view", "dbo", "v1"), ObjectKey("procedure", "dbo", "p1")
    text = "CREATE VIEW dbo.v1 AS SELECT 1;\nGO\n"
    sync = SyncRecord.load(tmp_path, "dev")
    assert not sync.path.exists()

    # First look: both sides hold v1; nothing is known about p1.
    changes = compare({view: text}, {view: text, proc: "CREATE PROC dbo.p1 AS SELECT 1;\nGO\n"}, sync)
    assert [(c.key, c.action, c.origin) for c in changes] == [(proc, DELETED, REPO)]  # a teammate's new proc
    sync.save()
    assert json.loads(sync.path.read_text())["objects"] == {"Views/dbo.v1.sql": sync.hashes[view]}

    # Later: v1 is gone from the database. It was synced before, so it was dropped.
    sync = SyncRecord.load(tmp_path, "dev")
    (change,) = compare({}, {view: text}, sync)
    assert (change.action, change.origin) == (DELETED, DATABASE)

    # Changed on one side only: the side that still matches the record did not move.
    sync = SyncRecord.load(tmp_path, "dev")
    other = text.replace("1", "2")
    assert compare({view: other}, {view: text}, sync)[0].origin == DATABASE
    assert compare({view: text}, {view: other}, sync)[0].origin == REPO
    assert compare({view: other}, {view: other.replace("2", "3")}, sync)[0].origin == BOTH
    # New in the database, never synced: the database made it.
    assert compare({view: text, proc: "x"}, {view: text}, sync)[0].origin == DATABASE
    # Changed, never synced: nobody knows.
    assert compare({proc: "x"}, {proc: "y"}, SyncRecord(None))[0].origin == UNKNOWN


def test_commit_leaves_a_teammates_unapplied_object_alone(tmp_path, fake_db):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")
    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    assert ".sqlsc/" in (tmp_path / ".gitignore").read_text()
    view, proc = ObjectKey("view", "dbo", "vSales"), ObjectKey("procedure", "dbo", "usp_New")
    fake_db[view] = "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n"
    assert run(tmp_path, "--json", "commit", "-m", "First").exit_code == 0

    # A teammate's commit lands: a new proc the database does not have yet.
    write_object(tmp_path / "database", proc, "CREATE PROC dbo.usp_New AS SELECT 1;\nGO\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "Teammate adds usp_New")
    status = json.loads(run(tmp_path, "--json", "status").output)
    assert status["sides"] == {"to_commit": 0, "to_get": 1, "conflicts": 0, "unknown_side": 0}
    assert status["changes"][0]["origin"] == REPO

    # Commit must not delete their file; get-latest is what applies it.
    result = json.loads(run(tmp_path, "--json", "commit").output)
    assert result["commit"] is None and [h["object"] for h in result["held_back"]] == ["[dbo].[usp_New]"]
    assert (tmp_path / "database" / "Stored Procedures" / "dbo.usp_New.sql").is_file()
    # --force commits it as the database sees it (gone).
    assert json.loads(run(tmp_path, "--json", "commit", "--force").output)["summary"]["deleted"] == 1
    assert not (tmp_path / "database" / "Stored Procedures" / "dbo.usp_New.sql").exists()

    # Dropped from the database after a sync: that is a real deletion, committed as such.
    del fake_db[view]
    status = json.loads(run(tmp_path, "--json", "status").output)
    assert status["changes"][0]["origin"] == DATABASE
    assert json.loads(run(tmp_path, "--json", "commit").output)["summary"]["deleted"] == 1
    assert not (tmp_path / "database" / "Views" / "dbo.vSales.sql").exists()


def test_get_latest_keeps_uncommitted_database_work(tmp_path, fake_db, monkeypatch):
    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    view = ObjectKey("view", "dbo", "vSales")
    fake_db[view] = "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n"
    assert run(tmp_path, "script").exit_code == 0  # synced
    fake_db[view] = "CREATE VIEW dbo.vSales AS SELECT 2;\nGO\n"  # edited in the database, not committed

    applied: list[str] = []
    monkeypatch.setattr(cli_mod, "apply", lambda conn, plan, target: applied.append(plan) or _no_failures())
    result = json.loads(run(tmp_path, "--json", "get-latest").output)
    assert result["steps"] == [] and [k["object"] for k in result["kept"]] == ["[dbo].[vSales]"]
    assert "changed in the database" in result["kept"][0]["reason"]
    forced = json.loads(run(tmp_path, "--json", "get-latest", "--force").output)
    assert [s["object"] for s in forced["steps"]] == ["[dbo].[vSales]"] and forced["kept"] == []


def _no_failures():
    class Result:
        failed: list = []
        done: list = []
        transactional = True

    return Result()


def test_status_says_who_you_are_and_which_checkouts_are_yours(tmp_path, fake_db, monkeypatch):
    # The SSMS window shows "you" on your own checkouts: status names the user it ran
    # as and flags each change and lock with `mine`, compared case-insensitively.
    from datetime import datetime

    from coop_sqlsc.locks import Lock

    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    view = ObjectKey("view", "dbo", "vSales")
    fake_db[view] = "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n"
    lock = Lock("repo", view, "Ada@Example.com", "ada", datetime(2026, 10, 19, 12, 0), None)

    class FakeStore:
        def __init__(self, conn, transactional):
            pass

        def list(self, repo):
            return [lock]

    monkeypatch.setattr(cli_mod, "LockStore", FakeStore)
    mine = json.loads(run(tmp_path, "--json", "status", "--as", "ada@example.com").output)
    assert mine["user"] == "ada@example.com"
    assert mine["changes"][0]["locked_by"] == "Ada@Example.com" and mine["changes"][0]["mine"] is True
    assert mine["locks"][0]["mine"] is True

    theirs = json.loads(run(tmp_path, "--json", "status", "--as", "bob@example.com").output)
    assert theirs["changes"][0]["mine"] is False and theirs["locks"][0]["mine"] is False
