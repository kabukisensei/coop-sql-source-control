"""A catalog facet the database did not answer for is unknown, not empty (#50).

A failed index, key, check, foreign-key, default, identity, computed-column,
collation or schema read must never script a partial object as the whole: a
commit would record the missing index as a deletion, and a later deploy would
DROP it from a healthy database.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from coop_sqlsc import catalog
from coop_sqlsc import cli as cli_mod
from coop_sqlsc.compare import DELETED, compare
from coop_sqlsc.config import Target
from coop_sqlsc.deploy import table_changes
from coop_sqlsc.layout import objects_from_files, relative_path
from coop_sqlsc.objects import ObjectKey

TARGET = Target(name="dev", kind="sql_server", server="", database="Fixture", auth="entra")
TABLE = ObjectKey("table", "dbo", "T")
VIEW = ObjectKey("view", "dbo", "vT")
SCHEMA = ObjectKey("schema", "", "sales")

HEALTHY = {
    catalog.COLLATION_SQL: [("Latin1_General_CI_AS",)],
    catalog.SCHEMAS_SQL: [("sales", "dbo")],
    catalog.OBJECTS_SQL: [(1, "dbo", "T", "U"), (2, "dbo", "vT", "V")],
    catalog.MODULES_SQL: [(2, "CREATE VIEW dbo.vT AS SELECT Id FROM dbo.T", True, True)],
    catalog.COLUMNS_SQL: [(1, 1, "Id", "int", "sys", False, 4, 10, 0, False, False, False, None)],
    catalog.IDENTITY_SQL: [],
    catalog.COMPUTED_SQL: [],
    catalog.DEFAULTS_SQL: [],
    catalog.KEYS_SQL: [],
    catalog.INDEXES_SQL: [(1, "IX_T", False, "NONCLUSTERED", False, None, "Id", False, False, 1, 1)],
    catalog.CHECKS_SQL: [],
    catalog.FKS_SQL: [],
}

FACETS = {
    "collation": catalog.COLLATION_SQL,
    "schemas": catalog.SCHEMAS_SQL,
    "identity": catalog.IDENTITY_SQL,
    "computed": catalog.COMPUTED_SQL,
    "defaults": catalog.DEFAULTS_SQL,
    "keys": catalog.KEYS_SQL,
    "indexes": catalog.INDEXES_SQL,
    "checks": catalog.CHECKS_SQL,
    "foreign keys": catalog.FKS_SQL,
}


def fake_connection(rows: dict, failing: set[str] = frozenset()):
    class Cursor:
        def execute(self, statement):
            if statement in failing:
                raise RuntimeError("fixture metadata read failure")
            self.statement = statement
            return self

        def fetchall(self):
            return rows[self.statement]

    class Connection:
        def cursor(self):
            return Cursor()

    return Connection()


def test_a_healthy_read_scripts_the_index():
    result = catalog.read_database(fake_connection(HEALTHY), TARGET)
    assert "CREATE NONCLUSTERED INDEX IX_T ON dbo.T (Id);" in result.objects[TABLE]
    assert result.unread == {} and result.warnings == []
    assert SCHEMA in result.objects


@pytest.mark.parametrize("facet", sorted(FACETS))
def test_a_failed_facet_read_holds_the_type_back(facet):
    result = catalog.read_database(fake_connection(HEALTHY, {FACETS[facet]}), TARGET)
    held = "schema" if facet == "schemas" else "table"
    assert list(result.unread) == [held]
    assert "fixture metadata read failure" in result.unread[held]
    assert not [k for k in result.objects if k.type == held], "a partial object must not be scripted"
    # Everything else is still read.
    assert VIEW in result.objects
    assert (TABLE in result.objects) == (held != "table")
    assert any("Skipped" in w for w in result.warnings)


def test_a_genuinely_empty_index_read_still_records_the_removal():
    rows = {**HEALTHY, catalog.INDEXES_SQL: []}
    result = catalog.read_database(fake_connection(rows), TARGET)
    assert result.unread == {}
    with_index = catalog.read_database(fake_connection(HEALTHY), TARGET).objects[TABLE]
    folder, _ = objects_from_files({relative_path(TABLE): with_index})
    changes = [c for c in compare(result.objects, folder) if c.key == TABLE]
    assert [(c.key, c.action) for c in changes] == [(TABLE, "changed")]
    statements, _ = table_changes(TABLE, changes[0].db_text, changes[0].file_text, allow_drops=False)
    assert any("CREATE NONCLUSTERED INDEX IX_T" in s for s in statements)


# --- through the CLI: nothing written, nothing deleted, nothing planned --------------


def run(root, *args: str):
    return CliRunner().invoke(cli_mod.main, ["--config", str(root / "sqlsc.yml"), *args])


class Repo:
    """A fixture repo folder and the catalog rows (and failing queries) its target answers with."""

    def __init__(self, path: Path):
        self.path = path
        self.rows = dict(HEALTHY)
        self.failing: set[str] = set()

    def __truediv__(self, other: str) -> Path:
        return self.path / other


@pytest.fixture
def repo(tmp_path, monkeypatch):
    fixture = Repo(tmp_path)

    class Conn:
        def cursor(self):
            return self

        def execute(self, sql, *params):
            return self

        def fetchone(self):
            return (0,)

        def close(self):
            pass

    monkeypatch.setattr(cli_mod, "open_connection", lambda target: Conn())
    monkeypatch.setattr(
        cli_mod,
        "read_database",
        lambda conn, target: catalog.read_database(fake_connection(fixture.rows, fixture.failing), target),
    )
    run(tmp_path, "init", "--kind", "sql_server", "--server", "s", "--database", "Fixture")
    assert run(tmp_path, "script").exit_code == 0
    table_file = tmp_path / "database" / "Tables" / "dbo.T.sql"
    assert "IX_T" in table_file.read_text()
    return fixture


def test_script_and_status_leave_a_held_table_alone(repo):
    table_file = repo / "database" / "Tables" / "dbo.T.sql"
    before = table_file.read_text()
    repo.failing.add(catalog.INDEXES_SQL)

    status = json.loads(run(repo, "--json", "status").output)
    assert status["unread"] == ["Tables/dbo.T.sql"]
    assert status["summary"] == {"new": 0, "changed": 0, "deleted": 0}
    assert any("No table was compared" in w and "indexes" in w for w in status["warnings"])

    result = run(repo, "--json", "script")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["files"] == []
    assert table_file.read_text() == before, "the index must not be scripted away"
    # The view, read in full, is still compared.
    assert (repo / "database" / "Views" / "dbo.vT.sql").is_file()


def test_a_held_schema_file_is_not_deleted(repo):
    schema_file = repo / "database" / "Security" / "Schemas" / "sales.sql"
    assert schema_file.is_file()
    repo.failing.add(catalog.SCHEMAS_SQL)
    status = json.loads(run(repo, "--json", "status").output)
    assert status["unread"] == ["Security/Schemas/sales.sql"]
    assert [c for c in status["changes"] if c["action"] == DELETED] == []
    assert run(repo, "script").exit_code == 0
    assert schema_file.is_file()


def test_get_latest_plans_nothing_for_a_held_table(repo):
    repo.failing.add(catalog.KEYS_SQL)
    result = run(repo, "--json", "get-latest", "--dry-run")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert all(step["schema"] + "." + step["name"] != "dbo.T" for step in payload.get("steps", []))
    assert not any("DROP" in s for step in payload.get("steps", []) for s in step.get("statements", []))


def test_a_commit_does_not_record_the_lost_index(repo):
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo.path, check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "dev@example.com")
    git("config", "user.name", "Dev")
    first = run(repo, "--json", "commit", "-m", "Baseline")
    assert first.exit_code == 0, first.output
    assert "IX_T" in git("show", "HEAD:database/Tables/dbo.T.sql")

    repo.failing.add(catalog.INDEXES_SQL)
    second = run(repo, "--json", "commit", "-m", "After a failed read")
    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    assert payload["commit"] is None and payload["summary"] == {"new": 0, "changed": 0, "deleted": 0}
    assert "IX_T" in git("show", "HEAD:database/Tables/dbo.T.sql")
    assert "IX_T" in (repo / "database" / "Tables" / "dbo.T.sql").read_text()
