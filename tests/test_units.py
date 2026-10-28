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
from coop_sqlsc.layout import key_for_path, read_folder, relative_path, use_layout, write_object
from coop_sqlsc.objects import ObjectKey, normalize
from coop_sqlsc.scripter import (
    Column,
    ForeignKey,
    KeyConstraint,
    Table,
    quote,
    script_module,
    script_table,
    script_table_sqlsc,
    type_text,
)

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
    assert "    PRIMARY KEY CLUSTERED ([Id] ASC)\n" in script_table(table, database_collation=None)
    assert "ALTER TABLE dbo.T ADD PRIMARY KEY CLUSTERED (Id);" in script_table_sqlsc(table, database_collation=None)


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
        """A database with no sqlsc.ObjectLock table yet (the first-run state): the lock
        table reads as positively absent, so there are no checkouts."""

        def cursor(self):
            return self

        def execute(self, sql, *params):
            return self

        def fetchone(self):
            return (0,)

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
    # The first commit also links the repo: init's files go along while they are new.
    assert _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split() == [
        ".gitattributes",
        ".gitignore",
        "database/Views/dbo.vSales.sql",
        "sqlsc.yml",
    ]
    assert json.loads(first.output)["setup_files"] == ["sqlsc.yml", ".gitattributes", ".gitignore"]

    # Edited by hand and applied: the database matches the file, HEAD does not.
    fake_db[view] = "CREATE VIEW dbo.vSales AS SELECT 2;\nGO\n"
    (tmp_path / "database" / "Views" / "dbo.vSales.sql").write_text(fake_db[view], encoding="utf-8")
    second = json.loads(run(tmp_path, "--json", "commit").output)
    assert second["summary"] == {"new": 0, "changed": 1, "deleted": 0}
    assert "SELECT 2" in _git(tmp_path, "show", "HEAD:database/Views/dbo.vSales.sql")
    assert json.loads(run(tmp_path, "--json", "commit").output)["commit"] is None
    assert _git(tmp_path, "status", "--porcelain", "--", "database") == ""

    # An edited filter is committed on its own once nothing else differs.
    with (tmp_path / "sqlsc.yml").open("a") as handle:
        handle.write("exclude_schemas: [staging]\n")
    third = json.loads(run(tmp_path, "--json", "commit").output)
    assert third["setup_files"] == ["sqlsc.yml"] and third["commit"]
    assert _git(tmp_path, "log", "-1", "--format=%s") == "Link the repo to dev: sqlsc.yml"


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


def test_fabric_sql_database_items_give_host_and_real_database_name():
    from coop_sqlsc.fabric_api import connection_host, database_name, item_path

    item = {
        "displayName": "Coop Dev SQL",
        "properties": {
            "serverFqdn": "abc.database.fabric.microsoft.com,1433",
            "databaseName": "Coop Dev SQL-791b20aa",
        },
    }
    assert connection_host("fabric_sql_database", item) == "abc.database.fabric.microsoft.com"
    assert database_name("fabric_sql_database", item) == "Coop Dev SQL-791b20aa"
    assert database_name("fabric_warehouse", item) == ""
    assert item_path("fabric_sql_database", "w", "i").endswith("/workspaces/w/sqlDatabases/i")


def test_coop_fabric_sql_database_targets_use_the_items_database_name(tmp_path, monkeypatch):
    from coop_sqlsc import fabric_api

    (tmp_path / ".coop").mkdir()
    (tmp_path / ".coop" / "project.yml").write_text(
        "sql_targets:\n  default_environment: dev\n"
        "  dev:\n    kind: fabric_sql_database\n    workspace_id: w-1\n    item_id: i-1\n    database: Coop Dev SQL\n"
    )
    (tmp_path / "sqlsc.yml").write_text("folder: database\n")
    monkeypatch.setattr(fabric_api, "lookup_server", lambda *a: "abc.database.fabric.microsoft.com")
    monkeypatch.setattr(fabric_api, "lookup_database", lambda *a: "Coop Dev SQL-i-1")
    target = load(tmp_path).target()
    assert target.server == "abc.database.fabric.microsoft.com" and target.database == "Coop Dev SQL-i-1"


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


def test_a_not_null_column_without_a_default_is_flagged():
    # Found on LocalDB: the ADD runs, then fails on a table with rows (and the update rolls back).
    key = ObjectKey("table", "dbo", "T")
    repo = TABLE.replace("     ,Old int NULL\n", "     ,Old int NULL\n     ,Region varchar(10) NOT NULL\n")
    steps, notes = table_changes(key, TABLE, repo, allow_drops=False)
    assert "ALTER TABLE dbo.T ADD Region varchar(10) NOT NULL" in steps
    assert [n for n in notes if "Region" in n] == [
        "column Region is NOT NULL with no DEFAULT: the ADD fails if the table has rows; "
        "give it a DEFAULT, or add it nullable and fill it in a before migration script"
    ]
    with_default = repo.replace("varchar(10) NOT NULL", "varchar(10) NOT NULL CONSTRAINT DF_T_Region DEFAULT ('NA')")
    _, quiet = table_changes(key, TABLE, with_default, allow_drops=False)
    assert not [n for n in quiet if "Region" in n]


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


def test_allow_drops_refuses_what_other_objects_still_use():
    # Found on LocalDB: dropping a column a procedure still read left the update failing half way.
    t = ObjectKey("table", "dbo", "T")
    gone = ObjectKey("procedure", "dbo", "p")
    other = ObjectKey("view", "dbo", "v")
    repo = TABLE.replace("     ,Old int NULL\n", "")
    plan = build_plan(
        [
            Change(t, "changed", TABLE, repo),
            Change(gone, "new", "CREATE PROCEDURE dbo.p AS SELECT 1\nGO\n", None),
            Change(other, "new", "CREATE VIEW dbo.v AS SELECT 1 AS a\nGO\n", None),
        ],
        allow_drops=True,
        guard_drops=True,
    )
    by_key = {s.key: s for s in plan.runnable}
    guard = by_key[gone].statements[0]
    assert "sys.sql_expression_dependencies" in guard and "OBJECT_ID(N'dbo.p')" in guard
    assert "d.referenced_minor_id = 0" in guard
    assert "NOT IN (OBJECT_ID(N'dbo.v'))" in guard  # dropped in the same plan, so it does not count
    assert "dbo.p is still used by %s" in guard
    assert by_key[gone].statements[-1] == "DROP PROCEDURE dbo.p"
    assert by_key[gone].notes == ["refused if a view, function, procedure or trigger still uses it"]
    column = [s for s in by_key[t].statements if "dbo.T.Old is still used by %s" in s]
    assert len(column) == 1 and "COLUMNPROPERTY(OBJECT_ID(N'dbo.T'), N'Old', 'ColumnId')" in column[0]
    # A plain (not schema-bound) view is recorded against the table only: ask it about its columns.
    assert "sys.dm_sql_referenced_entities(@name, N'OBJECT')" in column[0]
    assert "r.referenced_minor_name = N'Old'" in column[0]
    assert by_key[t].statements.index(column[0]) < by_key[t].statements.index("ALTER TABLE dbo.T DROP COLUMN Old")
    assert "column Old is dropped unless a view, function, procedure or trigger still uses it" in by_key[t].notes
    # Fabric Warehouse has no sys.sql_expression_dependencies: no guard there.
    plain = build_plan([Change(gone, "new", "CREATE PROCEDURE dbo.p AS SELECT 1\nGO\n", None)], allow_drops=True)
    assert plain.runnable[0].statements == ["DROP PROCEDURE dbo.p"] and plain.runnable[0].notes == []


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
    assert json.loads(sync.path.read_text())["objects"] == {"view:dbo.v1": sync.hashes[view]}
    # A record written by 1.0.9 or before named objects by file path; it still reads.
    sync.path.write_text(json.dumps({"objects": {"Views/dbo.v1.sql": sync.hashes[view]}}))
    assert SyncRecord.load(tmp_path, "dev").hashes == {view: sync.hashes[view]}

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
    monkeypatch.setattr(cli_mod, "apply", lambda conn, plan, target, *_: applied.append(plan) or _no_failures())
    result = json.loads(run(tmp_path, "--json", "get-latest").output)
    assert result["steps"] == [] and [k["object"] for k in result["kept"]] == ["[dbo].[vSales]"]
    assert "changed in the database" in result["kept"][0]["reason"]
    forced = json.loads(run(tmp_path, "--json", "get-latest", "--force").output)
    assert [s["object"] for s in forced["steps"]] == ["[dbo].[vSales]"] and forced["kept"] == []


def test_a_failed_update_reads_cleanly(tmp_path, fake_db, monkeypatch):
    # SQL Server's messages end with a period; the CLI added another ("conditions.. Every").
    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    (tmp_path / "database" / "Views").mkdir(parents=True)
    (tmp_path / "database" / "Views" / "dbo.vNew.sql").write_text("CREATE VIEW dbo.vNew AS SELECT 1;\nGO\n")

    class Failed:
        failed = [(ObjectKey("view", "dbo", "vNew"), "it does not satisfy these conditions.")]
        done: list = []
        transactional = True

    monkeypatch.setattr(cli_mod, "apply", lambda conn, plan, target, *_: Failed())
    result = run(tmp_path, "get-latest")
    assert result.exit_code == 1
    text = " ".join(result.output.split())
    assert "these conditions. Every change was rolled back." in text and ".." not in text


def _no_failures(plan=None):
    class Result:
        failed: list = []
        done: list = [s.key for s in plan.steps] if plan else []
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


# --- migration scripts -----------------------------------------------------------------------


def test_migration_files_know_when_they_run_and_when_they_were_edited(tmp_path):
    from datetime import datetime

    from coop_sqlsc import migrations as mig

    files = {
        "Migrations/002_after.sql": "UPDATE t SET x = 1;\nGO\n",
        "Migrations/001_before.sql": "-- sqlsc: before\nALTER TABLE t ADD y int NULL;\nGO\nUPDATE t SET y = 0;\n",
        "Migrations/notes.txt": "ignored",
        "Tables/dbo.t.sql": "CREATE TABLE dbo.t (x int);",
    }
    found = mig.from_files(files)
    assert [(m.id, m.when) for m in found] == [("001_before", "before"), ("002_after", "after")]
    assert mig.statements(found[0]) == ["ALTER TABLE t ADD y int NULL;", "UPDATE t SET y = 0;"]
    applied = {"001_before": mig.Applied("001_before", datetime(2026, 1, 1), "ada", found[0].checksum)}
    assert [m.id for m in mig.pending(found, applied)] == ["002_after"]
    assert mig.edited(found, applied) == []
    changed = mig.parse("001_before", "-- sqlsc: before\nALTER TABLE t ADD y int NOT NULL;\n")
    assert mig.edited([changed], applied) == [changed]
    recorded = mig.record_sql(found[1], "o'neil", None)
    assert "VALUES ('002_after', SYSUTCDATETIME(), 'o''neil', SUSER_SNAME()," in recorded

    path = mig.new_file(tmp_path, "Split customer name!", now=datetime(2026, 10, 20, 9, 5))
    assert path.name == "20261020090500_Split-customer-name.sql" and path.read_text().startswith("-- sqlsc: after")
    assert mig.read_folder(tmp_path)[0].when == "after"


def _fake_migration_store(monkeypatch, applied: dict):
    from coop_sqlsc import migrations as mig

    monkeypatch.setattr(mig, "read_applied", lambda conn: dict(applied))


def test_status_text_lists_changes_and_pending_migrations_together(tmp_path, fake_db, monkeypatch):
    # Found live: with a migration pending, text status hid the object changes, and with
    # nothing to report it printed "matches" and then an empty "0 new, 0 changed" table.
    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    _fake_migration_store(monkeypatch, {})
    quiet = run(tmp_path, "status").output
    assert "The repo matches" in quiet and "0 new" not in quiet

    folder = tmp_path / "database"
    (folder / "Views").mkdir(parents=True)
    (folder / "Views" / "dbo.vSales.sql").write_text("CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n")
    (folder / "Migrations").mkdir()
    (folder / "Migrations" / "001_backfill.sql").write_text("UPDATE dbo.t SET x = 1;\n")
    both = run(tmp_path, "status").output
    assert "dbo" in both and "vSales" in both and "1 migration script(s) have not run" in both

    # A repo linked before the sync record existed still gets .sqlsc/ ignored.
    (tmp_path / ".gitignore").write_text("*.bak\n")
    run(tmp_path, "status")
    assert (tmp_path / ".gitignore").read_text() == "*.bak\n.sqlsc/\n"


def test_get_latest_runs_pending_migrations_around_the_structural_changes(tmp_path, fake_db, monkeypatch):
    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    folder = tmp_path / "database"
    (folder / "Views").mkdir(parents=True)
    (folder / "Views" / "dbo.vSales.sql").write_text("CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n")
    (folder / "Migrations").mkdir()
    (folder / "Migrations" / "001_prepare.sql").write_text("-- sqlsc: before\nSELECT 1;\n")
    (folder / "Migrations" / "002_backfill.sql").write_text("UPDATE dbo.t SET x = 1;\n")
    _fake_migration_store(monkeypatch, {})

    status = json.loads(run(tmp_path, "--json", "status", "--exit-code").output)
    assert [m["id"] for m in status["migrations"]["pending"]] == ["001_prepare", "002_backfill"]

    # Written out for review: the before script, the create, then the after script.
    dry = json.loads(run(tmp_path, "--json", "get-latest", "--dry-run").output)
    script = dry["script"]
    assert script.index("001_prepare") < script.index("CREATE VIEW") < script.index("002_backfill")
    assert "migration script(s) run first" in script and "sqlsc.Migration" in script
    assert [s["action"] for s in dry["steps"]] == ["create", "migrate", "migrate"]

    # Run: the before script goes on its own, the database is read again, then the rest.
    plans: list = []
    monkeypatch.setattr(cli_mod, "apply", lambda conn, plan, target, *_: plans.append(plan) or _no_failures(plan))
    result = json.loads(run(tmp_path, "--json", "get-latest").output)
    assert result["applied"] is True
    assert [[s.key.name for s in p.steps] for p in plans] == [["001_prepare"], ["vSales", "002_backfill"]]
    assert [m["id"] for m in result["migrations"]] == ["001_prepare", "002_backfill"]
    assert [s["object"] for s in result["steps"]][0] == "Migrations/001_prepare.sql"

    text = run(tmp_path, "get-latest").output
    assert "Updated 1 object(s) and ran 2 migration script(s)." in text

    # Once recorded, nothing runs again, and a single object's get-latest never runs scripts.
    from datetime import datetime

    from coop_sqlsc import migrations as mig

    done = {m: mig.Applied(m, datetime(2026, 1, 1), "ada", "x") for m in ("001_prepare", "002_backfill")}
    _fake_migration_store(monkeypatch, done)
    plans.clear()
    again = json.loads(run(tmp_path, "--json", "get-latest").output)
    assert again["migrations"] == [] and [s["action"] for s in again["steps"]] == ["create"]
    listing = json.loads(run(tmp_path, "--json", "migrations").output)
    assert [m["state"] for m in listing["migrations"]] == ["edited", "edited"]  # checksum "x" is not the file's


def test_commit_takes_new_migration_scripts_along(tmp_path, fake_db, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")
    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    _fake_migration_store(monkeypatch, {})
    added = json.loads(run(tmp_path, "--json", "migrations", "add", "backfill region").output)
    assert added["path"].endswith("_backfill-region.sql")
    Path(added["path"]).write_text("UPDATE dbo.Customer SET Region = 'NA' WHERE Region IS NULL;\n")

    result = json.loads(run(tmp_path, "--json", "commit").output)
    rel = f"Migrations/{Path(added['path']).name}"
    assert result["commit"] and result["migrations"] == [rel]
    committed = _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == [".gitattributes", ".gitignore", "database/" + rel, "sqlsc.yml"]
    assert "Add 1 migration script(s)" in _git(tmp_path, "log", "-1", "--format=%s")


def test_status_lists_files_that_still_need_committing(tmp_path, fake_db, monkeypatch):
    # Found in SSMS after Track rows…: the data file matched the database, so status (and
    # the add-in's "Commit every change (N)") did not count it, yet commit took it along.
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")
    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    fake_db[ObjectKey("view", "dbo", "vSales")] = "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n"
    assert run(tmp_path, "script").exit_code == 0  # written, not committed

    status = run(tmp_path, "--json", "status", "--exit-code")
    assert status.exit_code == 1
    listed = json.loads(status.output)
    assert [(c["object"], c["action"], c["origin"]) for c in listed["changes"]] == [
        ("[dbo].[vSales]", "new", "database")
    ]
    assert listed["summary"] == {"new": 1, "changed": 0, "deleted": 0}

    assert json.loads(run(tmp_path, "--json", "commit").output)["commit"]
    assert run(tmp_path, "status", "--exit-code").exit_code == 0


def test_a_deploy_with_nothing_to_change_still_records_the_release(tmp_path, fake_db, monkeypatch):
    # Found on the Fabric SQL database: history said "No releases recorded" for a database
    # that was at the released commit, because the record only went in with the changes.
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")
    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    fake_db[ObjectKey("view", "dbo", "vSales")] = "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n"
    assert run(tmp_path, "script").exit_code == 0
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "v1")

    executed: list[str] = []

    class Cursor:
        def execute(self, statement, *params):
            executed.append(statement)
            return self

    class Conn:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli_mod, "open_connection", lambda target: Conn())
    result = json.loads(run(tmp_path, "--json", "deploy", "--ref", "HEAD").output)
    assert result["steps"] == [] and result["recorded"] is True
    assert any(s.startswith("INSERT INTO sqlsc.Deployment") and ", 0)" in s for s in executed)
    text = run(tmp_path, "deploy", "--ref", "HEAD").output
    assert "Recorded the release in sqlsc.Deployment" in text


def test_an_agents_checkout_belongs_to_the_person_it_works_for():
    from coop_sqlsc.locks import on_behalf, owns

    assert owns("ada@example.com", "ADA@example.com")
    assert owns("coop-agent:ada@example.com", "ada@example.com")
    assert on_behalf("coop-agent:ada@example.com", "ada@example.com")
    assert not on_behalf("ada@example.com", "ada@example.com")
    # The agent never takes over the person's own checkouts, or another person's agent's.
    assert not owns("ada@example.com", "coop-agent:ada@example.com")
    assert not owns("coop-agent:bob@example.com", "ada@example.com")
    assert not owns("coop-agent:ada@example.com", "coop-agent:bob@example.com")


# --- schema filter ------------------------------------------------------------------


def test_schema_lists_in_sqlsc_yml_are_case_blind_and_ignore_wins(tmp_path):
    (tmp_path / "sqlsc.yml").write_text(
        "target: dev\nschemas: [Sales, finance]\nexclude_schemas: FINANCE\n"
        "targets:\n  dev:\n    kind: azure_sql\n    server: s\n    database: d\n"
    )
    dev = load(tmp_path).target()
    assert dev.keeps_schema("sales") and dev.keeps_schema("SALES")
    assert not dev.keeps_schema("finance")  # ignored, though included
    assert not dev.keeps_schema("dbo")  # not on the include list
    open_target = build_target("dev", {"kind": "azure_sql", "server": "s", "database": "d"})
    assert open_target.keeps_schema("dbo")  # no lists: the whole database


def test_a_schema_list_that_is_not_names_says_how_to_write_it(tmp_path):
    (tmp_path / "sqlsc.yml").write_text("schemas: {sales: true}\n")
    with pytest.raises(ConfigError, match=r"list of schema names"):
        load(tmp_path)


def test_left_out_schemas_are_not_scripted_compared_or_deleted(tmp_path, fake_db):
    assert run(tmp_path, "init", "--kind", "azure_sql", "--server", "s", "--database", "Sales").exit_code == 0
    with (tmp_path / "sqlsc.yml").open("a") as handle:
        handle.write("exclude_schemas: [staging]\n")
    fake_db[ObjectKey("schema", "", "staging")] = "CREATE SCHEMA [staging];\nGO\n"
    fake_db[ObjectKey("view", "staging", "vLoad")] = "CREATE VIEW staging.vLoad AS SELECT 1;\nGO\n"
    fake_db[ObjectKey("view", "dbo", "vSales")] = "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n"
    # A file the team scripted before the schema was left out stays, and is not "deleted".
    write_object(tmp_path / "database", ObjectKey("view", "staging", "vOld"), "CREATE VIEW staging.vOld AS SELECT 2;\n")

    status = json.loads(run(tmp_path, "--json", "status").output)
    assert status["summary"] == {"new": 1, "changed": 0, "deleted": 0}
    assert run(tmp_path, "script").exit_code == 0
    assert (tmp_path / "database" / "Views" / "dbo.vSales.sql").is_file()
    assert not (tmp_path / "database" / "Views" / "staging.vLoad.sql").exists()
    assert (tmp_path / "database" / "Views" / "staging.vOld.sql").is_file()


def test_a_cp1252_console_gets_question_marks_not_a_crash(tmp_path, monkeypatch):
    import io
    import sys

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    cli_mod._printable_output()
    print("✓ Wrote sqlsc.yml", file=stream)
    stream.flush()
    assert raw.getvalue().decode("cp1252").startswith("? Wrote sqlsc.yml")


def test_init_writes_the_schema_filter_and_says_so(tmp_path):
    result = run(
        tmp_path,
        "--json",
        "init",
        "--kind",
        "azure_sql",
        "--server",
        "s",
        "--database",
        "Sales",
        "--schema",
        "sales",
        "--schema",
        "Finance",
        "--exclude-schema",
        "staging",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schemas"] == ["sales", "Finance"] and payload["exclude_schemas"] == ["staging"]
    dev = load(tmp_path).target()
    assert dev.keeps_schema("finance") and not dev.keeps_schema("dbo") and not dev.keeps_schema("staging")
    plain = json.loads(
        run(tmp_path, "--json", "init", "--force", "--kind", "azure_sql", "--server", "s", "--database", "Sales").output
    )
    assert set(plain) == {"ok", "command", "config", "folder", "layout", "target"}


# --- file names longer than the file system allows (#45) ----------------------------------


def test_long_object_names_are_refused_before_writing(tmp_path):
    from coop_sqlsc.layout import FileNameTooLong, file_name_problem

    long_key = ObjectKey("table", "s" * 128, "t" * 128)
    problem = file_name_problem(long_key)
    assert problem is not None and "261 characters" in str(problem) and "255" in str(problem)
    with pytest.raises(FileNameTooLong):
        write_object(tmp_path, long_key, "-- fixture only\n")
    assert not list(tmp_path.rglob("*.sql")), "nothing is written"
    fits = ObjectKey("table", "s" * 124, "t" * 126)  # 124 + 1 + 126 + 4 = 255
    assert file_name_problem(fits) is None
    assert file_name_problem(ObjectKey("view", "dbo", "Ärger")) is None
    assert file_name_problem(ObjectKey("schema", "", "x" * 251)) is None  # 251 + 4 = 255
    assert file_name_problem(ObjectKey("schema", "", "x" * 252)) is not None


def test_sync_record_keys_survive_a_dot_in_a_schema_or_name(tmp_path):  # 64
    from coop_sqlsc.sync import _record_key

    dotted_schema, dotted_name = ObjectKey("view", "a.b", "c"), ObjectKey("view", "a", "b.c")
    assert _record_key(dotted_schema) != _record_key(dotted_name)
    assert _record_key(dotted_schema) == "view:a%2Eb.c" and _record_key(dotted_name) == "view:a.b%2Ec"
    odd = ObjectKey("procedure", "100%", "p.q%r")
    schema = ObjectKey("schema", "", "a.b")
    texts = {
        dotted_schema: "CREATE VIEW [a.b].[c] AS SELECT 1;",
        dotted_name: "CREATE VIEW [a].[b.c] AS SELECT 2;",
        odd: "x",
        schema: "y",
    }
    sync = SyncRecord.load(tmp_path, "dev")
    for key, text in texts.items():
        sync.record(key, text)
    sync.save()
    raw = json.loads(sync.path.read_text())
    assert raw["format"] == 2 and len(raw["objects"]) == 4
    restored = SyncRecord.load(tmp_path, "dev")
    assert restored.hashes == sync.hashes
    # The repo-side edit of [a.b].[v] is held as the repo's, in both layouts.
    for layout in ("type", "schema"):
        use_layout(layout)
        key = ObjectKey("view", "a.b", "v")
        old, new = "CREATE VIEW [a.b].[v] AS SELECT 1;", "CREATE VIEW [a.b].[v] AS SELECT 2;"
        record = SyncRecord.load(tmp_path, layout)
        record.record(key, old)
        record.save()
        change = compare({key: old}, {key: new}, SyncRecord.load(tmp_path, layout))[0]
        assert change.origin == REPO and not change.from_database
        assert compare({key: new}, {key: old}, SyncRecord.load(tmp_path, layout))[0].origin == DATABASE
    use_layout("type")
    # A 1.0.10 record (no format) reads its one-dot keys; a key with two dots is
    # ambiguous and is dropped rather than read at its first dot. File paths from
    # 1.0.9 still read.
    plain = ObjectKey("view", "dbo", "v1")
    old = {"view:dbo.v1": "h1", "view:a.b.c": "h2", "schema:s": "h3", "Views/dbo.v2.sql": "h4"}
    sync.path.write_text(json.dumps({"objects": old}))
    legacy = SyncRecord.load(tmp_path, "dev")
    assert legacy.hashes == {plain: "h1", ObjectKey("schema", "", "s"): "h3", ObjectKey("view", "dbo", "v2"): "h4"}
    assert compare({dotted_schema: "x"}, {dotted_schema: "y"}, legacy)[0].origin == UNKNOWN
