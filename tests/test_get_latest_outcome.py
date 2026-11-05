"""What a failed get-latest left applied, as the JSON answer says it (#79).

The SSMS add-in reports a failed update from `done` and `transaction`: a target
without transactions (a Fabric Warehouse) keeps the steps that ran before the
failure, and "before" migration scripts commit on their own ahead of the
structural changes even where the rest is one transaction.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from coop_sqlsc import catalog
from coop_sqlsc import cli as cli_mod

EMPTY = {
    catalog.COLLATION_SQL: [("Latin1_General_CI_AS",)],
    catalog.SCHEMAS_SQL: [],
    catalog.OBJECTS_SQL: [],
    catalog.MODULES_SQL: [],
    catalog.COLUMNS_SQL: [],
    catalog.IDENTITY_SQL: [],
    catalog.COMPUTED_SQL: [],
    catalog.DEFAULTS_SQL: [],
    catalog.KEYS_SQL: [],
    catalog.INDEXES_SQL: [],
    catalog.CHECKS_SQL: [],
    catalog.FKS_SQL: [],
}


def fake_catalog(rows: dict):
    class Cursor:
        def execute(self, statement):
            self.statement = statement
            return self

        def fetchall(self):
            return rows.get(self.statement, [])

    class Connection:
        def cursor(self):
            return Cursor()

    return Connection()


def run(root, *args: str):
    return CliRunner().invoke(cli_mod.main, ["--config", str(root / "sqlsc.yml"), *args])


def setup(tmp_path, monkeypatch, kind: str) -> list[str]:
    executed: list[str] = []

    class Conn:
        autocommit = True

        def cursor(self):
            return self

        def execute(self, sql, *params):
            if "BadType" in sql:
                raise RuntimeError("Type BadType is not a defined system type.")
            executed.append(sql)
            return self

        def fetchone(self):
            return (0,)

        def fetchall(self):
            return []

        def commit(self):
            executed.append("COMMIT")

        def rollback(self):
            executed.append("ROLLBACK")

        def close(self):
            pass

    monkeypatch.setattr(cli_mod, "open_connection", lambda target: Conn())
    monkeypatch.setattr(
        cli_mod, "read_database", lambda conn, target: catalog.read_database(fake_catalog(EMPTY), target)
    )
    result = run(tmp_path, "init", "--kind", kind, "--server", "s", "--database", "Fixture")
    assert result.exit_code == 0, result.output
    views = tmp_path / "database" / "Views"
    views.mkdir(parents=True)
    (views / "dbo.vA.sql").write_text("CREATE VIEW dbo.vA AS SELECT 1 AS One\nGO\n", encoding="utf-8")
    (views / "dbo.vB.sql").write_text("CREATE VIEW dbo.vB AS SELECT CAST(1 AS BadType) AS One\nGO\n", encoding="utf-8")
    return executed


def test_a_target_without_transactions_reports_what_stays_applied(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, "fabric_warehouse")
    result = run(tmp_path, "--json", "get-latest")
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["applied"] is False
    assert payload["transaction"] is False
    assert payload["done"] == ["[dbo].[vA]"]
    assert [f["object"] for f in payload["failed"]] == ["[dbo].[vB]"]
    assert "BadType" in payload["failed"][0]["error"]


def test_a_transactional_failure_rolls_everything_back(tmp_path, monkeypatch):
    executed = setup(tmp_path, monkeypatch, "sql_server")
    result = run(tmp_path, "--json", "get-latest")
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["transaction"] is True
    assert payload["done"] == []
    assert [f["object"] for f in payload["failed"]] == ["[dbo].[vB]"]
    assert "ROLLBACK" in executed and "COMMIT" not in executed


@pytest.mark.parametrize("kind", ["sql_server", "fabric_warehouse"])
def test_a_before_migration_committed_ahead_of_a_failure_is_reported_done(tmp_path, monkeypatch, kind):
    executed = setup(tmp_path, monkeypatch, kind)
    migrations = tmp_path / "database" / "Migrations"
    migrations.mkdir()
    (migrations / "202601010000_fill.sql").write_text("-- sqlsc: before\nSELECT 1 AS Filled;\n", encoding="utf-8")
    result = run(tmp_path, "--json", "get-latest")
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["transaction"] is (kind == "sql_server")
    # The script ran and committed first; on a transactional target the structural
    # changes after it were rolled back, on a Warehouse vA stays.
    expected = ["Migrations/202601010000_fill.sql"] + ([] if kind == "sql_server" else ["[dbo].[vA]"])
    assert payload["done"] == expected
    assert [f["object"] for f in payload["failed"]] == ["[dbo].[vB]"]
    if kind == "sql_server":
        assert executed.count("COMMIT") == 1 and executed.count("ROLLBACK") == 1
