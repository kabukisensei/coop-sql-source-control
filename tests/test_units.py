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


def test_cli_errors_are_json_for_agents(tmp_path):
    result = CliRunner().invoke(cli_mod.main, ["--json", "--config", str(tmp_path / "missing.yml"), "status"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["error"]["kind"] == "config"
