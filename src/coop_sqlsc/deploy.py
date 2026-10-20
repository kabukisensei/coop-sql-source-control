"""Get latest: the script that makes a database match the repo, and running it.

The plan is built from the same comparison `status` shows, read from the
repo's side:

- an object only in the repo is created;
- a view, function, procedure or trigger that differs is changed in place
  with ALTER, so its permissions survive (SQL Server then stores the text as
  CREATE again, so the database scripts back exactly like the repo file;
  CREATE OR ALTER would leave padding spaces behind);
- a table that differs is altered column by column and constraint by
  constraint (a table is never dropped and re-created, so no row is lost);
- an object only in the database is dropped only with allow_drops, and a
  dropped column likewise;
- static data runs its MERGE script once every table and code object is in
  place, retried like code objects when one lookup table refers to another.

Changes that cannot be made safely by ALTER (a new IDENTITY, a changed
computed column, an unnamed constraint that was removed) are reported as
needing a hand-written migration and left out; everything else still runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import migrations
from .compare import CHANGED, DELETED, NEW, Change
from .config import Target
from .data import identity_off
from .locks import CREATE_SCHEMA
from .migrations import BEFORE, Migration
from .objects import ObjectKey, batches
from .scripter import qualified, quote

# Order steps run in. Drops of code objects go first so nothing they read blocks
# a table change; static data once its tables (and their triggers) exist; table
# drops last, after nothing reads them any more.
PH_DROP_CODE, PH_SCHEMA, PH_TABLE, PH_TABLE_KEYS, PH_CODE, PH_DATA, PH_DROP_TABLE, PH_DROP_SCHEMA = range(8)
# Hand-written migration scripts (migrations.py) bracket the structural changes.
PH_MIGRATE_BEFORE, PH_MIGRATE_AFTER = -1, 8

_DROP_WORD = {"view": "VIEW", "procedure": "PROCEDURE", "function": "FUNCTION", "trigger": "TRIGGER"}
_TRANSACTIONAL = {"sql_server", "azure_sql", "fabric_sql_database"}


@dataclass
class Step:
    key: ObjectKey
    action: str  # create, alter, drop, data, skip, manual
    phase: int
    statements: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cleanup: list[str] = field(default_factory=list)  # run if the step fails part way

    def to_json(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "type": self.key.type,
            "schema": self.key.schema,
            "name": self.key.name,
            "object": self.key.display,
            "statements": self.statements,
            "notes": self.notes,
        }


@dataclass
class Plan:
    steps: list[Step]

    @property
    def runnable(self) -> list[Step]:
        return [s for s in self.steps if s.statements and s.action not in ("skip", "manual")]

    @property
    def held_back(self) -> list[Step]:
        return [s for s in self.steps if s.action in ("skip", "manual")]

    def script(self, target: Target, header: list[str] | None = None, footer: list[str] | None = None) -> str:
        lines = header or [
            f"-- Get latest for {target.label} '{target.name}' ({target.database}), written by sqlsc.",
            "-- Review before running it anywhere other than a development database.",
        ]
        lines = [*lines, ""]
        for step in sorted(self.runnable, key=lambda s: (s.phase, s.key.sort_key)):
            lines.append(f"-- {step.action}: {step.key.display}")
            for note in step.notes:
                lines.append(f"-- {note}")
            for statement in step.statements:
                lines.append(statement.rstrip())
                lines.append("GO")
            lines.append("")
        for step in self.held_back:
            lines.append(f"-- not included ({step.action}): {step.key.display}: {'; '.join(step.notes)}")
        for statement in footer or []:
            lines += [statement.rstrip(), "GO"]
        return "\n".join(lines).rstrip() + "\n"


def migration_steps(pending: list[Migration], who: str, commit: str | None) -> list[Step]:
    """One step per migration that has not run here: its batches, then the row that
    says it has. The first step of each phase also makes sure sqlsc.Migration exists."""
    steps: list[Step] = []
    seen: set[int] = set()
    for migration in pending:
        phase = PH_MIGRATE_BEFORE if migration.when == BEFORE else PH_MIGRATE_AFTER
        ensure = [] if phase in seen else [CREATE_SCHEMA, migrations.CREATE_TABLE]
        seen.add(phase)
        steps.append(
            Step(
                migration.key,
                "migrate",
                phase,
                [*ensure, *migrations.statements(migration), migrations.record_sql(migration, who, commit)],
                [f"runs once, {migration.when} the structural changes"],
            )
        )
    return steps


# Targets whose sys.sql_expression_dependencies says what still reads a column or
# object, so a drop can refuse while something depends on it (see _still_used_guard).
GUARDED_KINDS = {"sql_server", "azure_sql", "fabric_sql_database"}


def build_plan(
    changes: list[Change], *, allow_drops: bool, enforced_keys: bool = True, guard_drops: bool = False
) -> Plan:
    steps: list[Step] = []
    # Objects this plan drops do not count as still using what else it drops.
    dropping = (
        [c.key for c in changes if c.action == NEW and allow_drops and c.key.type in ("table", *_DROP_WORD)]
        if guard_drops
        else None
    )
    for change in changes:
        key = change.key
        if change.action == DELETED:  # only in the repo: create it
            steps.extend(_create_steps(key, change.file_text or ""))
        elif change.action == NEW:  # only in the database: drop it
            steps.append(_drop_step(key, allow_drops, dropping))
        elif change.action == CHANGED:
            steps.append(_change_step(key, change.db_text or "", change.file_text or "", allow_drops, dropping))
    return Plan(steps)


# --- creates and drops ----------------------------------------------------------------


def _create_steps(key: ObjectKey, text: str) -> list[Step]:
    parts = batches(text)
    if key.type == "data":
        return [_data_step(key, text)]
    if key.type == "schema":
        return [Step(key, "create", PH_SCHEMA, parts)]
    if key.type == "table":
        # Foreign keys wait until every new table exists.
        plain = [b for b in parts if not _is_foreign_key(b)]
        keys = [b for b in parts if _is_foreign_key(b)]
        out = [Step(key, "create", PH_TABLE, plain)]
        if keys:
            out.append(Step(key, "create", PH_TABLE_KEYS, keys, ["foreign keys"]))
        return out
    return [Step(key, "create", PH_CODE, parts)]


def _drop_step(key: ObjectKey, allow_drops: bool, dropping: list[ObjectKey] | None = None) -> Step:
    if key.type == "data":  # never happens: rows are only read for tables the repo tracks
        return Step(key, "skip", PH_DATA, [], ["static data is not tracked in the repo"])
    if key.type == "schema":
        statement, phase = f"DROP SCHEMA {quote(key.name)}", PH_DROP_SCHEMA
    elif key.type == "table":
        statement, phase = f"DROP TABLE {qualified(key.schema, key.name)}", PH_DROP_TABLE
    else:
        statement, phase = f"DROP {_DROP_WORD[key.type]} {qualified(key.schema, key.name)}", PH_DROP_CODE
    if not allow_drops:
        return Step(
            key, "skip", phase, [statement], ["in the database but not the repo; pass --allow-drops to drop it"]
        )
    guard = [] if dropping is None or key.type == "schema" else [_still_used_guard(key, None, dropping)]
    notes = ["refused if a view, function, procedure or trigger still uses it"] if guard else []
    return Step(key, "drop", phase, [*guard, statement], notes)


def _still_used_guard(key: ObjectKey, column: str | None, dropping: list[ObjectKey]) -> str:
    """A statement that fails, naming them, when other objects still read the object or
    column about to be dropped. Objects dropped by the same plan are not counted. Only
    for targets with sys.sql_expression_dependencies (GUARDED_KINDS); the whole update
    then rolls back, so nothing is half done."""

    def lit(text: str) -> str:
        return "N'" + text.replace("'", "''") + "'"

    obj = qualified(key.schema, key.name)
    what = obj + (f".{quote(column)}" if column else "")
    where = f"d.referenced_id = OBJECT_ID({lit(obj)})"
    if column:
        where += f" AND d.referenced_minor_id = COLUMNPROPERTY(d.referenced_id, {lit(column)}, 'ColumnId')"
    else:
        where += " AND d.referenced_minor_id = 0"
    others = [lit(qualified(k.schema, k.name)) for k in dropping if k != key]
    if others:
        where += " AND d.referencing_id NOT IN (" + ", ".join(f"OBJECT_ID({o})" for o in others) + ")"
    message = what + " is still used by %s. Change or drop those first, or do it in a migration script."
    return (
        "DECLARE @users nvarchar(max) = (SELECT STRING_AGG(QUOTENAME(OBJECT_SCHEMA_NAME(u.referencing_id)) "
        "+ '.' + QUOTENAME(OBJECT_NAME(u.referencing_id)), ', ') "
        f"FROM (SELECT DISTINCT d.referencing_id FROM sys.sql_expression_dependencies AS d WHERE {where}) AS u);\n"
        f"IF @users IS NOT NULL RAISERROR ({lit(message)}, 16, 1, @users)"
    )


# --- changes ------------------------------------------------------------------------------


def _data_step(key: ObjectKey, text: str) -> Step:
    off = identity_off(text)
    return Step(key, "data", PH_DATA, batches(text), cleanup=[off] if off else [])


def _change_step(
    key: ObjectKey, db_text: str, repo_text: str, allow_drops: bool, dropping: list[ObjectKey] | None = None
) -> Step:
    if key.type == "data":
        return _data_step(key, repo_text)
    if key.type in _DROP_WORD:
        return Step(key, "alter", PH_CODE, [to_alter(b) for b in batches(repo_text)])
    if key.type == "schema":
        return Step(key, "manual", PH_SCHEMA, [], ["the schema's owner changed; change it with ALTER AUTHORIZATION"])
    try:
        statements, notes = table_changes(key, db_text, repo_text, allow_drops, dropping)
    except ManualChange as exc:
        return Step(key, "manual", PH_TABLE, [], [str(exc)])
    if not statements:
        return Step(key, "skip", PH_TABLE, [], notes or ["only the column order differs; ALTER cannot reorder columns"])
    return Step(key, "alter", PH_TABLE, statements, notes)


class ManualChange(Exception):
    """A table change ALTER cannot make safely; the message says which."""


_CREATE = re.compile(r"\bCREATE(\s+OR\s+ALTER)?(\s+)(VIEW|PROC|PROCEDURE|FUNCTION|TRIGGER)\b", re.IGNORECASE)


def to_alter(batch: str) -> str:
    """The first CREATE [OR ALTER] VIEW/PROCEDURE/FUNCTION/TRIGGER outside comments becomes ALTER."""
    match = _CREATE.search(_mask_comments_and_strings(batch))
    if match is None:
        return batch
    return batch[: match.start()] + "ALTER" + match.group(2) + batch[match.start(3) :]


def _mask_comments_and_strings(text: str) -> str:
    """The same text with comments and string literals blanked, positions unchanged."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        if text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
        elif text.startswith("/*", i):
            j, depth = i + 2, 1
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
        elif text[i] == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and j + 1 < n and text[j + 1] == "'":
                    j += 2
                elif text[j] == "'":
                    j += 1
                    break
                else:
                    j += 1
        else:
            i += 1
            continue
        for k in range(i, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


# --- tables ---------------------------------------------------------------------------------

_COLUMN = re.compile(
    r"^(?P<type>.+?)(?: COLLATE (?P<collation>\S+))?(?: IDENTITY\((?P<identity>[^)]*)\))?"
    r" (?P<null>NOT NULL|NULL)(?: CONSTRAINT (?P<dfname>\[(?:[^\]]|\]\])+\]|\S+))?(?: DEFAULT (?P<default>.+))?$"
)


@dataclass
class ParsedTable:
    name: str  # as written, e.g. sales.[Order]
    columns: dict[str, str]  # column name (unquoted) -> definition after the name
    order: list[str]
    statements: list[str]  # every batch after CREATE TABLE


def parse_table(text: str) -> ParsedTable:
    parts = batches(text)
    if not parts or not parts[0].upper().startswith("CREATE TABLE"):
        raise ManualChange("the table script does not start with CREATE TABLE")
    lines = parts[0].split("\n")
    name = lines[0][len("CREATE TABLE") :].strip()
    if len(lines) < 3 or lines[1].strip() != "(" or lines[-1].strip() != ");":
        raise ManualChange("the CREATE TABLE layout was edited by hand; ALTER statements cannot be worked out")
    columns: dict[str, str] = {}
    order: list[str] = []
    for line in lines[2:-1]:
        body = line.strip().lstrip(",").strip()
        if not body:
            continue
        col, rest = _split_name(body)
        columns[col] = rest
        order.append(col)
    return ParsedTable(name, columns, order, parts[1:])


def _split_name(text: str) -> tuple[str, str]:
    if text.startswith("["):
        i = 1
        while i < len(text):
            if text[i] == "]" and i + 1 < len(text) and text[i + 1] == "]":
                i += 2
            elif text[i] == "]":
                return text[1:i].replace("]]", "]"), text[i + 1 :].strip()
            else:
                i += 1
        raise ManualChange(f"cannot read the column name in: {text}")
    name, _, rest = text.partition(" ")
    return name, rest.strip()


def table_changes(
    key: ObjectKey, db_text: str, repo_text: str, allow_drops: bool, dropping: list[ObjectKey] | None = None
) -> tuple[list[str], list[str]]:
    old, new = parse_table(db_text), parse_table(repo_text)
    table = qualified(key.schema, key.name)
    drops: list[str] = []
    columns: list[str] = []
    adds: list[str] = []
    notes: list[str] = []

    old_set, new_set = set(old.statements), set(new.statements)
    for statement in old.statements:
        if statement not in new_set:
            drops.append(_undo_statement(table, statement))
    for statement in new.statements:
        if statement not in old_set:
            adds.append(statement)

    for col in new.order:
        if col not in old.columns:
            columns.append(f"ALTER TABLE {table} ADD {quote(col)} {new.columns[col]}")
            if _needs_rows_filled(new.columns[col]):
                notes.append(
                    f"column {col} is NOT NULL with no DEFAULT: the ADD fails if the table has rows; "
                    "give it a DEFAULT, or add it nullable and fill it in a before migration script"
                )
    for col in old.order:
        if col in new.columns:
            if old.columns[col] != new.columns[col]:
                columns.extend(_alter_column(table, key, col, old.columns[col], new.columns[col]))
        elif allow_drops:
            if dropping is not None:
                columns.append(_still_used_guard(key, col, dropping))
                notes.append(f"column {col} is dropped unless a view, function, procedure or trigger still uses it")
            columns.append(_drop_default(key, col))
            columns.append(f"ALTER TABLE {table} DROP COLUMN {quote(col)}")
        else:
            notes.append(f"column {col} is not in the repo; pass --allow-drops to drop it")
    kept_old = [c for c in old.order if c in new.columns]
    kept_new = [c for c in new.order if c in old.columns]
    if kept_old != kept_new:
        notes.append("column order differs; ALTER cannot reorder columns, so the order is left as it is")
    return drops + columns + adds, notes


def _needs_rows_filled(definition: str) -> bool:
    """A NOT NULL column without a DEFAULT or IDENTITY cannot be added to a table with rows."""
    upper = definition.upper()
    return " NOT NULL" in upper and " DEFAULT " not in upper and " IDENTITY" not in upper and " AS " not in upper


def _alter_column(table: str, key: ObjectKey, col: str, old: str, new: str) -> list[str]:
    if old.startswith("AS ") or new.startswith("AS "):
        raise ManualChange(f"computed column {col} changed; drop and re-add it in a migration script")
    a, b = _COLUMN.match(old), _COLUMN.match(new)
    if a is None or b is None:
        raise ManualChange(f"cannot read the definition of column {col}")
    if a["identity"] != b["identity"]:
        raise ManualChange(f"IDENTITY on column {col} changed; that needs a table rebuild in a migration script")
    out: list[str] = []
    default_changed = (a["dfname"], a["default"]) != (b["dfname"], b["default"])
    if default_changed and a["default"] is not None:
        out.append(_drop_default(key, col))
    if (a["type"], a["collation"], a["null"]) != (b["type"], b["collation"], b["null"]):
        collation = f" COLLATE {b['collation']}" if b["collation"] else ""
        out.append(f"ALTER TABLE {table} ALTER COLUMN {quote(col)} {b['type']}{collation} {b['null']}")
    if default_changed and b["default"] is not None:
        named = f" CONSTRAINT {b['dfname']}" if b["dfname"] else ""
        out.append(f"ALTER TABLE {table} ADD{named} DEFAULT {b['default']} FOR {quote(col)}")
    return out


def _drop_default(key: ObjectKey, col: str) -> str:
    """Drops whatever default the column has, named or not, looked up when it runs."""
    obj = qualified(key.schema, key.name).replace("'", "''")
    column = col.replace("'", "''")
    return (
        "DECLARE @default sysname = (SELECT d.name FROM sys.default_constraints AS d "
        f"WHERE d.parent_object_id = OBJECT_ID(N'{obj}') "
        f"AND d.parent_column_id = COLUMNPROPERTY(OBJECT_ID(N'{obj}'), N'{column}', 'ColumnId'));\n"
        f"DECLARE @drop nvarchar(max) = N'ALTER TABLE {obj} DROP CONSTRAINT ' + QUOTENAME(@default);\n"
        "IF @default IS NOT NULL EXEC (@drop)"
    )


_NAMED_CONSTRAINT = re.compile(r"^ALTER TABLE .+? ADD CONSTRAINT (\[(?:[^\]]|\]\])+\]|\S+) ", re.IGNORECASE)
_INDEX = re.compile(
    r"^CREATE (?:UNIQUE )?(?:CLUSTERED |NONCLUSTERED )?(?:COLUMNSTORE )?INDEX (\[(?:[^\]]|\]\])+\]|\S+) ON "
)
_NOCHECK = re.compile(r"^ALTER TABLE .+? NOCHECK CONSTRAINT (\[(?:[^\]]|\]\])+\]|\S+);?$", re.IGNORECASE)
_UNNAMED_PK = re.compile(r"^ALTER TABLE .+? ADD PRIMARY KEY ", re.IGNORECASE)


def _undo_statement(table: str, statement: str) -> str:
    """The statement that removes what one table-script statement added."""
    flat = " ".join(statement.split())
    if m := _NAMED_CONSTRAINT.match(flat):
        return f"ALTER TABLE {table} DROP CONSTRAINT {m.group(1)}"
    if m := _INDEX.match(flat):
        return f"DROP INDEX {m.group(1)} ON {table}"
    if m := _NOCHECK.match(flat):
        return f"ALTER TABLE {table} CHECK CONSTRAINT {m.group(1)}"
    if _UNNAMED_PK.match(flat):
        obj = table.replace("'", "''")
        return (
            "DECLARE @pk sysname = (SELECT k.name FROM sys.key_constraints AS k "
            f"WHERE k.parent_object_id = OBJECT_ID(N'{obj}') AND k.type = 'PK');\n"
            f"DECLARE @drop nvarchar(max) = N'ALTER TABLE {obj} DROP CONSTRAINT ' + QUOTENAME(@pk);\n"
            "IF @pk IS NOT NULL EXEC (@drop)"
        )
    raise ManualChange(
        f"a constraint SQL Server named itself was removed ({flat[:80]}…); give it a name, or drop it by hand"
    )


def _is_foreign_key(statement: str) -> bool:
    return " FOREIGN KEY " in " ".join(statement.split()).upper()


# --- running it ---------------------------------------------------------------------------

_RETRYABLE = (
    "Invalid object name",
    "Could not find",
    "Cannot find the object",
    "is not a recognized",
    "Invalid column name",
    "conflicted with the FOREIGN KEY constraint",
    "conflicted with the REFERENCE constraint",
)


@dataclass
class ApplyResult:
    done: list[ObjectKey]
    failed: list[tuple[ObjectKey, str]]
    transactional: bool


def apply(conn: Any, plan: Plan, target: Target) -> ApplyResult:
    """Runs the plan. On SQL Server and Azure SQL it is one transaction: all or nothing.

    Code objects that read each other (a view on a view) are retried in later
    passes until no more succeed, so their order in the repo does not matter.
    """
    transactional = target.kind in _TRANSACTIONAL
    cur = conn.cursor()
    if transactional:
        conn.autocommit = False
    done: list[ObjectKey] = []
    failed: list[tuple[ObjectKey, str]] = []
    steps = sorted(plan.runnable, key=lambda s: (s.phase, s.key.sort_key))
    try:
        for phase in sorted({s.phase for s in steps}):
            pending = [s for s in steps if s.phase == phase]
            while pending:
                deferred: list[tuple[Step, str]] = []
                for step in pending:
                    try:
                        for statement in step.statements:
                            cur.execute(statement)
                        done.append(step.key)
                    except Exception as exc:  # pyodbc.Error
                        message = _first_line(exc)
                        for statement in step.cleanup:
                            try:
                                cur.execute(statement)
                            except Exception:  # noqa: BLE001 - best effort, the real error follows
                                pass
                        if phase in (PH_CODE, PH_DATA) and any(word in message for word in _RETRYABLE):
                            deferred.append((step, message))
                        else:
                            raise StepFailed(step.key, message) from exc
                if len(deferred) == len(pending):
                    step, message = deferred[0]
                    raise StepFailed(step.key, message)
                pending = [s for s, _ in deferred]
        if transactional:
            conn.commit()
    except StepFailed as exc:
        if transactional:
            conn.rollback()
            done = []
        failed.append((exc.key, exc.message))
    finally:
        if transactional:
            conn.autocommit = True
    return ApplyResult(done, failed, transactional)


class StepFailed(Exception):
    def __init__(self, key: ObjectKey, message: str) -> None:
        super().__init__(message)
        self.key, self.message = key, message


def _first_line(exc: Exception) -> str:
    text = str(exc)
    # pyodbc puts the server's message after "[SQL Server]"; keep only that part.
    if "[SQL Server]" in text:
        text = text.split("[SQL Server]", 1)[1]
    text = re.sub(r"\s*\(\d+\)\s*\(SQL\w+\)[\"')]*\s*$", "", text.strip())
    return text.splitlines()[0][:400] if text else type(exc).__name__
