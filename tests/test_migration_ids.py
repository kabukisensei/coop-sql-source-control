"""A migration id sqlsc.Migration cannot hold never runs (#51).

MigrationId is varchar(200). Recorded truncated, a longer id would read as pending
again on the next update and a backfill would run twice.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from coop_sqlsc import cli as cli_mod
from coop_sqlsc import migrations as mig
from coop_sqlsc.catalog import CatalogResult

STAMP = "20261026130000_"


def _id(length: int) -> str:
    return STAMP + "x" * (length - len(STAMP))


@pytest.mark.parametrize("length", [199, 200])
def test_boundary_ids_are_recorded_as_they_are(length):
    m = mig.parse(_id(length), "SELECT 1;\n")
    assert mig.too_long([m]) == []
    assert f"VALUES ('{m.id}', SYSUTCDATETIME()" in mig.record_sql(m, "ada", None)
    applied = {m.id: mig.Applied(m.id, datetime.now(timezone.utc), "ada", m.checksum)}
    assert mig.pending([m], applied) == []


def test_a_201_character_id_is_refused_not_truncated():
    m = mig.parse(_id(201), "SELECT 1;\n")
    assert mig.too_long([m]) == [m]
    with pytest.raises(ValueError, match="longer than the 200 characters"):
        mig.record_sql(m, "ada", None)


def test_new_file_keeps_the_id_within_the_limit(tmp_path):
    path = mig.new_file(tmp_path, "x" * 300, now=datetime(2026, 10, 26, 13, 0, 0))
    assert len(path.stem) == mig.MAX_ID and path.stem.startswith(STAMP)
    assert mig.too_long(mig.read_folder(tmp_path)) == []
    short = mig.new_file(tmp_path, "Split customer name!", now=datetime(2026, 10, 20, 9, 5))
    assert short.name == "20261020090500_Split-customer-name.sql"


def run(root, *args: str):
    return CliRunner().invoke(cli_mod.main, ["--json", "--config", str(root / "sqlsc.yml"), *args])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    class Conn:
        ran: list[str] = []

        def cursor(self):
            return self

        def execute(self, sql, *params):
            Conn.ran.append(sql)
            return self

        def fetchone(self):
            return (0,)

        def fetchall(self):
            return []

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cli_mod, "open_connection", lambda target: Conn())
    monkeypatch.setattr(cli_mod, "read_database", lambda conn, target: CatalogResult({}, []))
    monkeypatch.setattr(mig, "read_applied", lambda conn: {})
    run(tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales")
    folder = tmp_path / "database" / "Migrations"
    folder.mkdir(parents=True)
    (folder / "001_ok.sql").write_text("SELECT 1;\n")
    return tmp_path, Conn


def test_get_latest_stops_before_running_an_unrecordable_migration(repo):
    root, conn = repo
    (root / "database" / "Migrations" / f"{_id(201)}.sql").write_text("UPDATE dbo.Balance SET Amount = Amount - 10;\n")
    status = json.loads(run(root, "status").output)
    assert any("longer than the 200 characters" in w for w in status["warnings"])

    for args in (("get-latest",), ("get-latest", "--dry-run")):
        result = run(root, *args)
        assert result.exit_code == 2, result.output
        error = json.loads(result.output)["error"]
        assert error["kind"] == "migration" and f"{_id(201)}.sql (201 characters)" in error["message"]
    assert not any("Amount - 10" in sql or "001_ok" in sql for sql in conn.ran), "nothing ran"


def test_a_200_character_migration_runs_once(repo, monkeypatch):
    root, conn = repo
    (root / "database" / "Migrations" / f"{_id(200)}.sql").write_text("SELECT 2;\n")
    plans: list = []

    def apply(conn, plan, target, *_):
        plans.append(plan)

        class Result:
            failed: list = []
            done = [s.key for s in plan.steps]
            transactional = True

        return Result()

    monkeypatch.setattr(cli_mod, "apply", apply)
    result = run(root, "get-latest")
    assert result.exit_code == 0, result.output
    assert [m["id"] for m in json.loads(result.output)["migrations"]] == ["001_ok", _id(200)]
    recorded = [s for p in plans for step in p.steps for s in step.statements if "INSERT INTO sqlsc.Migration" in s]
    assert any(f"'{_id(200)}'" in s for s in recorded)
