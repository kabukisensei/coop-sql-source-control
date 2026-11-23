"""Regression tests for the 2026-11-23 review: a nullable unique key matched with
plain equality (#95) and `migrations add` writing over a same-second script (#96).

Offline: a fake connection answers the static-data queries; the file system is real.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from coop_sqlsc import cli as cli_mod
from coop_sqlsc import data
from coop_sqlsc import migrations as mig
from coop_sqlsc.config import Target
from coop_sqlsc.objects import ObjectKey

from .test_unread_objects import DataConn

TARGET = Target("dev", "sql_server", "", "Fixture", "entra")
CODE_NULLABLE = ("Code", "int", 4, 10, 0, False, False, True)
CODE_NOT_NULL = ("Code", "int", 4, 10, 0, False, False, False)
REGION = ("Region", "nvarchar", 20, 0, 0, False, False, False)
LABEL = ("Label", "nvarchar", 40, 0, 0, False, False, False)
KEY = ObjectKey("data", "dbo", "Labels")

NULL_SAFE = "(target.Code = source.Code OR (target.Code IS NULL AND source.Code IS NULL))"


def script(columns, keys, rows):
    conn = DataConn(columns, keys)
    conn.fetchall = lambda c=conn: c.columns if c.sql == data.COLUMNS_SQL else c.keys if c.sql == data.KEY_SQL else rows
    text, count = data.script_data(conn, KEY, TARGET)
    assert count == len(rows)
    return text


# --- #95 a nullable unique key -----------------------------------------------------------------


def test_a_nullable_unique_key_column_is_matched_null_safely():
    text = script([CODE_NULLABLE, LABEL], [("UQ", "UQ_Labels", "Code")], [(None, "Unassigned"), ("1", "One")])
    assert "(NULL, N'Unassigned')" in text
    assert f"ON {NULL_SAFE}\n" in text
    assert "ON target.Code = source.Code\n" not in text


def test_a_composite_unique_key_is_null_safe_only_on_its_nullable_part():
    text = script(
        [REGION, CODE_NULLABLE, LABEL],
        [("UQ", "UQ_Labels", "Region"), ("UQ", "UQ_Labels", "Code")],
        [("North", None, "Unassigned")],
    )
    assert f"ON target.Region = source.Region AND {NULL_SAFE}\n" in text


def test_a_primary_key_still_matches_with_plain_equality():
    text = script([CODE_NOT_NULL, LABEL], [("PK", "PK_Labels", "Code")], [("1", "One")])
    assert "ON target.Code = source.Code\n" in text
    assert "IS NULL" not in text


def test_render_matches_equal_and_both_null_keys_but_not_one_null():
    # The predicate itself, evaluated the way SQL does with three-valued logic.
    def sql_match(target, source):
        return (target is not None and source is not None and target == source) or (target is None and source is None)

    column = data.DataColumn("Code", "int", "int", False, "num", True)
    assert data._key_match(column) == NULL_SAFE
    pairs = ((1, 1), (None, None), (1, None), (None, 1), (1, 2))
    assert [sql_match(t, s) for t, s in pairs] == [True, True, False, False, False]


# --- #96 a same-second migration of the same name ------------------------------------------


def test_a_second_migration_in_the_same_second_never_overwrites_the_first(tmp_path):
    when = datetime(2026, 11, 23, 15, 36, 27, tzinfo=timezone.utc)
    first = mig.new_file(tmp_path, "Attach labels", now=when)
    first.write_text(first.read_text(encoding="utf-8") + "SELECT 42;\n", encoding="utf-8")
    with pytest.raises(mig.MigrationExists) as caught:
        mig.new_file(tmp_path, "Attach labels", now=when)
    assert "already exists" in str(caught.value)
    # A name that normalizes to the same file name is the same collision.
    with pytest.raises(mig.MigrationExists):
        mig.new_file(tmp_path, "Attach  labels!", now=when)
    assert "SELECT 42;" in first.read_text(encoding="utf-8")
    assert sorted(p.name for p in (tmp_path / mig.FOLDER).iterdir()) == [first.name]
    # Another name, or another second, is a new file as before.
    other = mig.new_file(tmp_path, "Detach labels", now=when)
    later = mig.new_file(tmp_path, "Attach labels", now=when.replace(second=28))
    assert other != first and later != first and len(list((tmp_path / mig.FOLDER).iterdir())) == 3


def test_migrations_add_reports_the_collision_as_an_error(tmp_path, monkeypatch):
    (tmp_path / "sqlsc.yml").write_text("target: dev\n", encoding="utf-8")
    monkeypatch.setattr(mig, "datetime", _Fixed)
    args = ["--json", "--config", str(tmp_path / "sqlsc.yml"), "migrations", "add", "Attach labels"]
    first = CliRunner().invoke(cli_mod.main, args)
    assert first.exit_code == 0, first.output
    second = CliRunner().invoke(cli_mod.main, args)
    assert second.exit_code == 2
    assert '"ok": false' in second.output and "already exists" in second.output


class _Fixed(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 23, 15, 36, 27, tzinfo=tz)
