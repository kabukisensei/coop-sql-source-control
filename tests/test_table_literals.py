"""A multiline literal in a table's column list is one column when the table's
columns are put in order for compare (#71): the bytes inside the literal count, the
order of whole columns does not.

Offline: in-memory catalog text only. All names are synthetic.
"""

from __future__ import annotations

import pytest

from coop_sqlsc.compare import CHANGED, _legacy_comparable, comparable, compare
from coop_sqlsc.deploy import build_plan
from coop_sqlsc.objects import ObjectKey
from coop_sqlsc.scripter import Column, Table, script_table, script_table_sqlsc
from coop_sqlsc.sync import REPO, UNKNOWN, SyncRecord, digest

KEY = ObjectKey("table", "dbo", "LiteralFixture")
RENDERERS = [script_table, script_table_sqlsc]


def table(default: str, *, swapped: bool = False) -> Table:
    columns = [
        Column("Id", "int", nullable=False),
        Column("Message", "nvarchar", max_length=400, default_definition="(" + default + ")"),
    ]
    return Table("dbo", "LiteralFixture", columns=columns[::-1] if swapped else columns)


PAIRS = [
    ("N'alpha  \nbeta'", "N'alpha\nbeta'"),  # trailing spaces
    ("N'first\nalpha\nbeta\nlast'", "N'first\nbeta\nalpha\nlast'"),  # swapped lines
    ("N'alpha\n  beta'", "N'alpha\nbeta'"),  # leading spaces
    ("N'alpha\n, beta'", "N'alpha\n beta'"),  # comma-prefixed line
    ("N'alpha\n\nbeta'", "N'alpha\nbeta'"),  # blank line
]


@pytest.mark.parametrize("render", RENDERERS)
@pytest.mark.parametrize("db_value,repo_value", PAIRS)
def test_a_change_inside_a_multiline_table_literal_is_a_difference(render, db_value, repo_value):
    db = render(table(db_value), database_collation=None)
    repo = render(table(repo_value), database_collation=None)
    changes = compare({KEY: db}, {KEY: repo})
    assert [(c.key, c.action) for c in changes] == [(KEY, CHANGED)]
    for allow_drops in (False, True):
        plan = build_plan(changes, allow_drops=allow_drops)
        # the deploy plan replaces the default with the whole literal (#74): never a
        # column-order skip, and never a column added or dropped that is not there
        assert [(s.key, s.action) for s in plan.steps] == [(KEY, "alter")]
        statements = plan.steps[0].statements
        assert len(statements) == 2
        assert "DROP CONSTRAINT" in statements[0] and "N'Message'" in statements[0]
        assert statements[1] == f"ALTER TABLE dbo.LiteralFixture ADD DEFAULT ({repo_value}) FOR Message"
        assert not any(" DROP COLUMN " in s or " ADD Message " in s for s in statements)


@pytest.mark.parametrize("render", RENDERERS)
def test_a_new_column_with_a_multiline_default_is_one_column(render):
    value = "N'first\nalpha\nbeta'"
    old = Table("dbo", "LiteralFixture", columns=[Column("Id", "int", nullable=False)])
    db = render(old, database_collation=None)
    repo = render(table(value), database_collation=None)
    plan = build_plan(compare({KEY: db}, {KEY: repo}), allow_drops=True)
    assert [(s.key, s.action) for s in plan.steps] == [(KEY, "alter")]
    assert len(plan.steps[0].statements) == 1
    assert plan.steps[0].statements[0].startswith("ALTER TABLE dbo.LiteralFixture ADD Message nvarchar(200) NULL")
    assert plan.steps[0].statements[0].endswith(f"DEFAULT ({value})")


@pytest.mark.parametrize("render", RENDERERS)
def test_whole_columns_in_another_order_still_compare_equal(render):
    value = "N'first\nalpha\nbeta\nlast'"
    db = render(table(value), database_collation=None)
    repo = render(table(value, swapped=True), database_collation=None)
    assert compare({KEY: db}, {KEY: repo}) == []
    assert compare({KEY: db}, {KEY: db}) == []


def test_the_two_shapes_still_compare_alike():
    value = "N'alpha\nbeta'"
    dacfx = script_table(table(value), database_collation=None)
    old = script_table_sqlsc(table(value), database_collation=None)
    assert compare({KEY: dacfx}, {KEY: old}) == []


def test_a_one_line_table_compares_on_the_text_it_always_did():
    text = script_table(table("N'plain'"), database_collation=None)
    assert comparable(KEY, text) == _legacy_comparable(KEY, text)


def test_a_repo_only_literal_edit_after_a_sync_is_the_repo():
    db = script_table(table("N'alpha  \nbeta'"), database_collation=None)
    repo = script_table(table("N'alpha\nbeta'"), database_collation=None)
    sync = SyncRecord(None)
    assert compare({KEY: db}, {KEY: db}, sync) == []  # matched: recorded
    changes = compare({KEY: db}, {KEY: repo}, sync)
    assert [(c.action, c.origin) for c in changes] == [(CHANGED, REPO)]


def test_a_record_from_before_the_fix_is_not_trusted_for_a_multiline_literal():
    db = script_table(table("N'alpha  \nbeta'"), database_collation=None)
    repo = script_table(table("N'alpha\nbeta'"), database_collation=None)
    # Written by 1.0.12, where both sides read alike: it cannot say which one moved.
    sync = SyncRecord(None, {KEY: digest(_legacy_comparable(KEY, db))})
    changes = compare({KEY: db}, {KEY: repo}, sync)
    assert [(c.action, c.origin) for c in changes] == [(CHANGED, UNKNOWN)]
    # Once the sides match again the record is rewritten in the new form.
    compare({KEY: repo}, {KEY: repo}, sync)
    assert sync.hashes[KEY] == digest(comparable(KEY, repo))
