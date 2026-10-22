"""Regression tests for literal-safe normalizing and renamed-module identity (1.0.4).

- A space, tab or line break inside a string literal or quoted name is data: capture
  keeps it, and a change inside a literal is a difference.
- sp_rename (and ALTER SCHEMA ... TRANSFER) leave a module's stored CREATE naming the
  old object: capture scripts it under its current name and says so; a deploy never runs
  a file whose CREATE names another object.

Offline: an in-memory catalog stands in for the database. All names are synthetic.
"""

from __future__ import annotations

import random

import pytest

from coop_sqlsc import catalog, sqltext
from coop_sqlsc.compare import CHANGED, DELETED, Change, comparable, compare
from coop_sqlsc.config import Target
from coop_sqlsc.deploy import build_plan
from coop_sqlsc.layout import objects_from_files, relative_path
from coop_sqlsc.migrations import parse
from coop_sqlsc.objects import ObjectKey, batches, normalize
from coop_sqlsc.scripter import script_module


def legacy_normalize(text: str) -> str:
    """normalize as it was before 1.0.4: every line end trimmed, literals included."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


PROC = ObjectKey("procedure", "dbo", "LiteralFixture")


def proc_with(literal: str) -> str:
    return f"CREATE PROCEDURE dbo.LiteralFixture\nAS\nSELECT {literal} AS LiteralValue;\n"


# --- literals: normalize -------------------------------------------------------------------


@pytest.mark.parametrize(
    "literal",
    [
        "'alpha  \nbeta'",  # trailing spaces before a line break
        "'alpha\t\nbeta'",  # a trailing tab
        "'alpha\r\nbeta  \r\n'",  # CR LF inside, trailing spaces before it
        "'alpha\rbeta'",  # a lone CR
        "N'ünïcode  \nwert  '",  # a Unicode literal
        "'it''s  \nhere'''",  # escaped quotes, one right before the close
        "'  \n\n  '",  # a literal of nothing but whitespace and blank lines
    ],
)
def test_normalize_keeps_literal_bytes(literal):
    text = proc_with(literal)
    assert literal in normalize(text)
    assert literal in script_module(text)


@pytest.mark.parametrize("ident", ["[Col  \nName]", '"Col  \nName"', "[odd]]  \nname]"])
def test_normalize_keeps_quoted_names(ident):
    text = f"CREATE VIEW dbo.QuotedFixture\nAS\nSELECT 1 AS {ident};\n"
    assert ident in normalize(text)


def test_normalize_still_trims_outside_literals():
    text = "\n\n  \r\nCREATE VIEW dbo.v   \r\nAS   \r\n  SELECT 'a  \nb' AS x   -- note   \r\n  FROM t\t\r\n\n\n"
    assert normalize(text) == "CREATE VIEW dbo.v\nAS\n  SELECT 'a  \nb' AS x   -- note\n  FROM t\n"


def test_a_quote_inside_a_comment_opens_no_literal():
    text = "-- don't   \nSELECT 1   \n/* it's /* nested */ still   \n a comment */   \nSELECT 'x  \ny'   \n"
    assert normalize(text) == "-- don't\nSELECT 1\n/* it's /* nested */ still\n a comment */\nSELECT 'x  \ny'\n"


def test_normalize_matches_the_old_one_wherever_there_is_no_literal():
    rng = random.Random(1004)
    alphabet = ["a", "b", " ", "\t", "\n", "\r\n", "\r", "-", "*", "/", ";", "GO", " "]
    for _ in range(2000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        if "--" in text or "/*" in text:
            continue  # comments are trimmed the same way, but keep the property simple
        assert normalize(text) == legacy_normalize(text), repr(text)


def test_normalize_is_idempotent():
    for text in [proc_with("'alpha  \r\nbeta'"), "SELECT 'unterminated  \n", "  \n", ""]:
        assert normalize(normalize(text)) == normalize(text)


def test_go_lines_still_split_batches():
    script = script_module(proc_with("'a  \nb'"), quoted_identifier=False)
    assert batches(script) == ["SET QUOTED_IDENTIFIER OFF", proc_with("'a  \nb'").strip()]


# --- literals: capture and compare ---------------------------------------------------------


def fake_connection(rows: dict):
    class Cursor:
        def execute(self, statement):
            if statement not in rows:
                raise AssertionError("unexpected catalog request")
            self.statement = statement
            return self

        def fetchall(self):
            return rows[self.statement]

    class Connection:
        def cursor(self):
            return Cursor()

    return Connection()


TARGET = Target(name="dev", kind="fabric_warehouse", server="", database="Fixture", auth="entra")
SQL_SERVER = Target(name="dev", kind="sql_server", server="", database="Fixture", auth="entra")


def read(objects: list[tuple], modules: list[tuple], target: Target = TARGET):
    return catalog.read_database(
        fake_connection(
            {
                catalog.COLLATION_SQL: [("Latin1_General_100_BIN2_UTF8",)],
                catalog.SCHEMAS_SQL: [],
                catalog.OBJECTS_SQL: objects,
                catalog.MODULES_SQL: modules,
            }
        ),
        target,
    )


def test_capture_then_compare_keeps_the_literal():
    definition = proc_with("'alpha  \nbeta'")
    captured = read([(1, "dbo", "LiteralFixture", "P")], [(1, definition, True, True)])
    assert "'alpha  \nbeta'" in captured.objects[PROC]
    # Written to the repo and read back, it matches the database: nothing to do.
    folder, _ = objects_from_files({relative_path(PROC): captured.objects[PROC]})
    assert compare(captured.objects, folder) == []


def test_a_change_only_inside_a_literal_is_a_difference():
    db = {PROC: script_module(proc_with("'alpha  \nbeta'"))}
    folder, _ = objects_from_files({relative_path(PROC): script_module(proc_with("'alpha\nbeta'"))})
    changes = compare(db, folder)
    assert [(c.key, c.action) for c in changes] == [(PROC, CHANGED)]
    assert "alpha  " in changes[0].unified_diff()
    # And the deploy plan carries the literal as the repo has it.
    plan = build_plan(changes, allow_drops=False)
    assert any("'alpha\nbeta'" in s for step in plan.steps for s in step.statements)


def test_formatting_outside_literals_is_still_no_difference():
    db = {PROC: script_module(proc_with("'alpha  \nbeta'"))}
    edited = proc_with("'alpha  \nbeta'").replace("\nAS\n", "   \r\nAS\t\r\n") + "GO  \r\n\r\n"
    folder, _ = objects_from_files({relative_path(PROC): edited})
    assert compare(db, folder) == []


def test_line_break_style_inside_a_literal_is_no_difference():
    # Git's text attribute stores literal line breaks as LF and checks them out as CR LF
    # on Windows; a style that cannot survive a clone is not a change.
    db = {PROC: script_module(proc_with("'alpha  \r\nbeta'"))}
    folder, _ = objects_from_files({relative_path(PROC): proc_with("'alpha  \nbeta'") + "GO\n"})
    assert compare(db, folder) == []
    assert comparable(PROC, db[PROC]) == comparable(PROC, folder[PROC])


def test_migration_checksums_do_not_move():
    text = "UPDATE dbo.t SET note = 'x  \ny';   \n"
    assert parse("20260101_note", text).checksum == parse("20260101_note", legacy_normalize(text)).checksum


# --- renamed modules ------------------------------------------------------------------------

VIEW = ObjectKey("view", "dbo", "RenamedView")


def test_a_renamed_view_is_scripted_under_its_current_name():
    result = read(
        [(42, "dbo", "RenamedView", "V")],
        [(42, "CREATE VIEW dbo.OriginalName AS SELECT 1 AS LiteralValue;", True, True)],
    )
    assert result.objects[VIEW] == "CREATE VIEW dbo.[RenamedView] AS SELECT 1 AS LiteralValue;\nGO\n"
    assert any("[dbo].[RenamedView]" in w and "[dbo].[OriginalName]" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("otype", "key", "definition", "expected"),
    [
        (
            "P",
            ObjectKey("procedure", "etl", "LoadThing_old"),
            # a self-reference in the body, the old name in a string, a comment and dynamic SQL
            "/* header: etl.LoadThing */\nCREATE   OR ALTER PROC [etl].[LoadThing]\n  @n int\nAS\n"
            "-- calls etl.LoadThing again\nIF @n > 0 EXEC etl.LoadThing @n - 1;\n"
            "EXEC (N'SELECT ''etl.LoadThing''');\nSELECT 'LoadThing' AS name;\n",
            "/* header: etl.LoadThing */\nCREATE   OR ALTER PROC [etl].[LoadThing_old]\n  @n int\nAS\n"
            "-- calls etl.LoadThing again\nIF @n > 0 EXEC etl.LoadThing @n - 1;\n"
            "EXEC (N'SELECT ''etl.LoadThing''');\nSELECT 'LoadThing' AS name;\n",
        ),
        (
            "IF",
            ObjectKey("function", "dbo", "Odd ] Name"),
            "CREATE FUNCTION dbo.fnOld() RETURNS TABLE AS RETURN SELECT 1 AS x;",
            "CREATE FUNCTION dbo.[Odd ]] Name]() RETURNS TABLE AS RETURN SELECT 1 AS x;",
        ),
        (
            "TR",
            ObjectKey("trigger", "sales", "trAudit_v2"),
            "CREATE TRIGGER [sales].[trAudit] ON sales.Orders AFTER INSERT AS SELECT 1;",
            "CREATE TRIGGER [sales].[trAudit_v2] ON sales.Orders AFTER INSERT AS SELECT 1;",
        ),
        (
            "V",
            ObjectKey("view", "archive", "Moved"),  # ALTER SCHEMA archive TRANSFER dbo.Moved
            "CREATE VIEW dbo.Moved AS SELECT 1 AS x;",
            "CREATE VIEW [archive].Moved AS SELECT 1 AS x;",
        ),
        (
            "V",
            ObjectKey("view", "dbo", "NewName"),  # no schema in the CREATE: only the name moves
            "CREATE VIEW OldName AS SELECT 1 AS x;",
            "CREATE VIEW [NewName] AS SELECT 1 AS x;",
        ),
    ],
)
def test_each_module_kind_takes_its_current_name(otype, key, definition, expected):
    trigger_ok = otype != "TR"
    result = read([(7, key.schema, key.name, otype)], [(7, definition, True, trigger_ok)], SQL_SERVER)
    scripted = result.objects[key]
    assert expected in scripted
    assert len(result.warnings) == 1
    found = sqltext.declaration(scripted)
    assert found is not None and (found.schema or "dbo", found.name)[1] == key.name
    if not trigger_ok:  # QUOTED_IDENTIFIER OFF: the SET batch stays in front
        assert scripted.startswith("SET QUOTED_IDENTIFIER OFF\nGO\n")


def test_a_matching_declaration_is_left_exactly_as_written():
    for definition in [
        "CREATE VIEW [dbo].[Same] AS SELECT 1 AS x;",
        "create view DBO.Same as select 1 as x;",  # the schema's case is how the CREATE spelled it
        "-- banner\nCREATE OR ALTER VIEW dbo . Same AS SELECT 1 AS x;",
    ]:
        result = read([(1, "dbo", "Same", "V")], [(1, definition, True, True)])
        assert result.objects[ObjectKey("view", "dbo", "Same")] == normalize(definition + "\nGO")
        assert result.warnings == []


def test_a_case_only_rename_is_a_rename():
    result = read([(1, "dbo", "MixedCase", "V")], [(1, "CREATE VIEW dbo.mixedcase AS SELECT 1 AS x;", True, True)])
    assert "CREATE VIEW dbo.[MixedCase]" in result.objects[ObjectKey("view", "dbo", "MixedCase")]


def test_a_corrected_capture_matches_the_stale_catalog():
    stale = "CREATE VIEW dbo.OriginalName AS SELECT 1 AS LiteralValue;"
    db = read([(42, "dbo", "RenamedView", "V")], [(42, stale, True, True)]).objects
    folder, _ = objects_from_files({relative_path(VIEW): db[VIEW]})
    assert compare(db, folder) == []  # no add, drop or alter from the declaration alone


def test_a_file_declaring_another_name_is_never_run():
    stale_file = "CREATE VIEW dbo.OriginalName AS SELECT 1 AS LiteralValue;\nGO\n"
    good = "CREATE VIEW dbo.[RenamedView] AS SELECT 2 AS LiteralValue;\nGO\n"
    for change in [
        Change(VIEW, DELETED, None, stale_file),  # only in the repo: would be created
        Change(VIEW, CHANGED, good, stale_file),  # in both: would be altered
    ]:
        plan = build_plan([change], allow_drops=True)
        assert [s.action for s in plan.steps] == ["manual"]
        assert plan.runnable == []
        assert not any(line.startswith(("CREATE", "ALTER")) for line in plan.script(TARGET).splitlines())
        assert "[dbo].[OriginalName]" in plan.steps[0].notes[0]
    # The corrected file deploys against the current name.
    plan = build_plan([Change(VIEW, CHANGED, stale_file, good)], allow_drops=False)
    statements = [s for step in plan.steps for s in step.statements]
    assert statements == ["ALTER VIEW dbo.[RenamedView] AS SELECT 2 AS LiteralValue;"]


def test_declaration_reader_ignores_names_it_cannot_place():
    assert sqltext.declaration("SELECT 1; CREATE VIEW dbo.v AS SELECT 1;") is None  # not leading
    assert sqltext.declaration("CREATE TABLE dbo.t (a int);") is None
    assert sqltext.declared_as("CREATE VIEW db.dbo.v AS SELECT 1;", "dbo", "w") == (
        "CREATE VIEW db.dbo.v AS SELECT 1;",
        None,
    )
