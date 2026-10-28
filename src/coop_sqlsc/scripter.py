"""Turning catalog facts into stable CREATE scripts.

The same object must always produce the same text, byte for byte, so a diff
only ever shows real changes. A table is scripted the way SSMS 22 and DacFx write
it (Create Project from Database, Schema Compare's Update): constraints inside the
CREATE TABLE, upper-case types, aligned columns, indexes after it. So a table file
SSMS wrote and one sqlsc wrote are the same file, and neither tool reformats the
other's. Where DacFx's shape cannot be run (a Fabric Warehouse takes keys only as
ALTER TABLE ... NOT ENFORCED), and for reading repos scripted before 1.0.11, the
sqlsc shape remains (script_table_sqlsc): leading commas one column left of the
list, constraints as ALTER TABLE statements after it. tablefile.py reads both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .objects import normalize

# T-SQL reserved keywords (Microsoft Learn, "Reserved keywords (Transact-SQL)").
RESERVED = frozenset(
    """
ADD ALL ALTER AND ANY AS ASC AUTHORIZATION BACKUP BEGIN BETWEEN BREAK BROWSE BULK BY CASCADE CASE
CHECK CHECKPOINT CLOSE CLUSTERED COALESCE COLLATE COLUMN COMMIT COMPUTE CONSTRAINT CONTAINS
CONTAINSTABLE CONTINUE CONVERT CREATE CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
CURRENT_USER CURSOR DATABASE DBCC DEALLOCATE DECLARE DEFAULT DELETE DENY DESC DISK DISTINCT
DISTRIBUTED DOUBLE DROP DUMP ELSE END ERRLVL ESCAPE EXCEPT EXEC EXECUTE EXISTS EXIT EXTERNAL FETCH
FILE FILLFACTOR FOR FOREIGN FREETEXT FREETEXTTABLE FROM FULL FUNCTION GOTO GRANT GROUP HAVING
HOLDLOCK IDENTITY IDENTITY_INSERT IDENTITYCOL IF IN INDEX INNER INSERT INTERSECT INTO IS JOIN KEY
KILL LEFT LIKE LINENO LOAD MERGE NATIONAL NOCHECK NONCLUSTERED NOT NULL NULLIF OF OFF OFFSETS ON
OPEN OPENDATASOURCE OPENQUERY OPENROWSET OPENXML OPTION OR ORDER OUTER OVER PERCENT PIVOT PLAN
PRECISION PRIMARY PRINT PROC PROCEDURE PUBLIC RAISERROR READ READTEXT RECONFIGURE REFERENCES
REPLICATION RESTORE RESTRICT RETURN REVERT REVOKE RIGHT ROLLBACK ROWCOUNT ROWGUIDCOL RULE SAVE
SCHEMA SECURITYAUDIT SELECT SEMANTICKEYPHRASETABLE SEMANTICSIMILARITYDETAILSTABLE
SEMANTICSIMILARITYTABLE SESSION_USER SET SETUSER SHUTDOWN SOME STATISTICS SYSTEM_USER TABLE
TABLESAMPLE TEXTSIZE THEN TO TOP TRAN TRANSACTION TRIGGER TRUNCATE TRY_CONVERT TSEQUAL UNION
UNIQUE UNPIVOT UPDATE UPDATETEXT USE USER VALUES VARYING VIEW WAITFOR WHEN WHERE WHILE WITH
WITHIN WRITETEXT
""".split()
)

_REGULAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_@#$]*$")


def quote(name: str) -> str:
    """A name with brackets only when it needs them."""
    if _REGULAR.match(name) and name.upper() not in RESERVED:
        return name
    return "[" + name.replace("]", "]]") + "]"


def qualified(schema: str, name: str) -> str:
    return f"{quote(schema)}.{quote(name)}"


@dataclass
class Column:
    name: str
    type_name: str
    max_length: int = 0
    precision: int = 0
    scale: int = 0
    nullable: bool = True
    type_schema: str = "sys"
    user_type: bool = False
    identity: tuple[str, str] | None = None
    computed: str | None = None
    persisted: bool = False
    collation: str | None = None
    default_name: str | None = None
    default_definition: str | None = None


@dataclass
class KeyConstraint:
    name: str | None  # None when SQL Server named it; such names differ per database
    primary: bool
    index_type: str  # CLUSTERED or NONCLUSTERED
    columns: list[tuple[str, bool]]  # (column, descending)


@dataclass
class Index:
    name: str
    unique: bool
    index_type: str  # CLUSTERED, NONCLUSTERED, CLUSTERED COLUMNSTORE, NONCLUSTERED COLUMNSTORE
    columns: list[tuple[str, bool]]
    included: list[str] = field(default_factory=list)
    filter_definition: str | None = None


@dataclass
class CheckConstraint:
    name: str | None
    definition: str
    not_trusted: bool = False
    disabled: bool = False


@dataclass
class ForeignKey:
    name: str | None
    columns: list[str]
    ref_schema: str
    ref_table: str
    ref_columns: list[str]
    on_delete: str = "NO_ACTION"
    on_update: str = "NO_ACTION"
    not_trusted: bool = False
    disabled: bool = False


@dataclass
class Table:
    schema: str
    name: str
    columns: list[Column] = field(default_factory=list)
    keys: list[KeyConstraint] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    checks: list[CheckConstraint] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)


def type_text(col: Column) -> str:
    if col.user_type:
        return qualified(col.type_schema, col.type_name)
    t = col.type_name.lower()
    if t in ("varchar", "char", "varbinary", "binary"):
        return f"{t}({'max' if col.max_length == -1 else col.max_length})"
    if t in ("nvarchar", "nchar"):
        return f"{t}({'max' if col.max_length == -1 else col.max_length // 2})"
    if t in ("decimal", "numeric"):
        return f"{t}({col.precision}, {col.scale})"
    if t in ("datetime2", "time", "datetimeoffset"):
        return f"{t}({col.scale})"
    return t


def column_text(col: Column, database_collation: str | None) -> str:
    if col.computed is not None:
        text = f"{quote(col.name)} AS {col.computed}"
        if col.persisted:
            text += " PERSISTED"
            if not col.nullable:
                text += " NOT NULL"
        return text
    text = f"{quote(col.name)} {type_text(col)}"
    if col.collation and database_collation and col.collation != database_collation:
        text += f" COLLATE {col.collation}"
    if col.identity is not None:
        text += f" IDENTITY({col.identity[0]}, {col.identity[1]})"
    text += " NULL" if col.nullable else " NOT NULL"
    if col.default_definition is not None:
        if col.default_name:
            text += f" CONSTRAINT {quote(col.default_name)}"
        text += f" DEFAULT {col.default_definition}"
    return text


def _named(name: str | None) -> str:
    return f" CONSTRAINT {quote(name)}" if name else ""


def _cols(cols: list[tuple[str, bool]]) -> str:
    return ", ".join(quote(c) + (" DESC" if desc else "") for c, desc in cols)


def script_table_sqlsc(table: Table, *, database_collation: str | None, enforced_keys: bool = True) -> str:
    """The sqlsc shape (before 1.0.11): the form get-latest works out ALTER statements on."""
    name = qualified(table.schema, table.name)
    lines = [f"CREATE TABLE {name}", "("]
    for i, col in enumerate(table.columns):
        lead = "      " if i == 0 else "     ,"
        lines.append(lead + column_text(col, database_collation))
    lines.append(");")
    lines.append("GO")

    for key in sorted(table.keys, key=lambda k: (not k.primary, (k.name or "").lower(), _cols(k.columns))):
        kind = "PRIMARY KEY" if key.primary else "UNIQUE"
        index_type = key.index_type if enforced_keys else "NONCLUSTERED"
        tail = "" if enforced_keys else " NOT ENFORCED"
        lines.append(f"ALTER TABLE {name} ADD{_named(key.name)} {kind} {index_type} ({_cols(key.columns)}){tail};")
        lines.append("GO")

    for check in sorted(table.checks, key=lambda c: ((c.name or "").lower(), c.definition)):
        nocheck = " WITH NOCHECK" if check.not_trusted else ""
        lines.append(f"ALTER TABLE {name}{nocheck} ADD{_named(check.name)} CHECK {check.definition};")
        lines.append("GO")
        if check.disabled and check.name:
            lines.append(f"ALTER TABLE {name} NOCHECK CONSTRAINT {quote(check.name)};")
            lines.append("GO")

    for fk in sorted(table.foreign_keys, key=lambda f: ((f.name or "").lower(), f.columns)):
        nocheck = " WITH NOCHECK" if fk.not_trusted and enforced_keys else ""
        text = (
            f"ALTER TABLE {name}{nocheck} ADD{_named(fk.name)} FOREIGN KEY "
            f"({', '.join(quote(c) for c in fk.columns)}) REFERENCES "
            f"{qualified(fk.ref_schema, fk.ref_table)} ({', '.join(quote(c) for c in fk.ref_columns)})"
        )
        if enforced_keys:
            if fk.on_delete != "NO_ACTION":
                text += f" ON DELETE {fk.on_delete.replace('_', ' ')}"
            if fk.on_update != "NO_ACTION":
                text += f" ON UPDATE {fk.on_update.replace('_', ' ')}"
        else:
            text += " NOT ENFORCED"
        lines.append(text + ";")
        lines.append("GO")
        if fk.disabled and enforced_keys and fk.name:
            lines.append(f"ALTER TABLE {name} NOCHECK CONSTRAINT {quote(fk.name)};")
            lines.append("GO")

    for idx in sorted(table.indexes, key=lambda i: i.name.lower()):
        if "COLUMNSTORE" in idx.index_type and idx.index_type.startswith("CLUSTERED"):
            lines.append(f"CREATE CLUSTERED COLUMNSTORE INDEX {quote(idx.name)} ON {name};")
        else:
            unique = "UNIQUE " if idx.unique else ""
            text = f"CREATE {unique}{idx.index_type} INDEX {quote(idx.name)} ON {name} ({_cols(idx.columns)})"
            if idx.included:
                text += f" INCLUDE ({', '.join(quote(c) for c in idx.included)})"
            if idx.filter_definition:
                text += f" WHERE {idx.filter_definition}"
            lines.append(text + ";")
        lines.append("GO")
    return normalize("\n".join(lines))


# --- the DacFx shape -----------------------------------------------------------------

# Types DacFx writes with their length, precision or scale, always.
_LENGTH_TYPES = ("varchar", "char", "varbinary", "binary")
_UNICODE_TYPES = ("nvarchar", "nchar")
_SCALE_TYPES = ("datetime2", "time", "datetimeoffset")
_DACFX_TYPE_NAMES = {"timestamp": "ROWVERSION"}


def bracket(name: str) -> str:
    """A name the way DacFx writes every name: in brackets."""
    return "[" + name.replace("]", "]]") + "]"


def dacfx_type(col: Column) -> str:
    if col.user_type:
        return f"{bracket(col.type_schema)}.{bracket(col.type_name)}"
    t = col.type_name.lower()
    if t in _LENGTH_TYPES:
        return f"{t.upper()} ({'MAX' if col.max_length == -1 else col.max_length})"
    if t in _UNICODE_TYPES:
        return f"{t.upper()} ({'MAX' if col.max_length == -1 else col.max_length // 2})"
    if t in ("decimal", "numeric"):
        return f"{t.upper()} ({col.precision}, {col.scale})"
    if t in _SCALE_TYPES:
        return f"{t.upper()} ({col.scale})"
    if t == "float":
        return f"FLOAT ({col.precision or 53})"
    return _DACFX_TYPE_NAMES.get(t, t.upper())


def _dacfx_cols(cols: list[tuple[str, bool]]) -> str:
    return ", ".join(bracket(c) + (" DESC" if desc else " ASC") for c, desc in cols)


def _dacfx_named(name: str | None) -> str:
    return f"CONSTRAINT {bracket(name)} " if name else ""


def _dacfx_key(key: KeyConstraint) -> str:
    kind = "PRIMARY KEY" if key.primary else "UNIQUE"
    return f"{_dacfx_named(key.name)}{kind} {key.index_type} ({_dacfx_cols(key.columns)})"


def _dacfx_check(check: CheckConstraint) -> str:
    return f"{_dacfx_named(check.name)}CHECK {check.definition}"


def _dacfx_fk(fk: ForeignKey) -> str:
    text = (
        f"{_dacfx_named(fk.name)}FOREIGN KEY ({', '.join(bracket(c) for c in fk.columns)}) "
        f"REFERENCES {bracket(fk.ref_schema)}.{bracket(fk.ref_table)} ({', '.join(bracket(c) for c in fk.ref_columns)})"
    )
    if fk.on_delete != "NO_ACTION":
        text += f" ON DELETE {fk.on_delete.replace('_', ' ')}"
    if fk.on_update != "NO_ACTION":
        text += f" ON UPDATE {fk.on_update.replace('_', ' ')}"
    return text


def _dacfx_column_rest(col: Column, database_collation: str | None) -> str:
    if col.computed is not None:
        rest = col.computed
        if col.persisted:
            rest += " PERSISTED"
            if not col.nullable:
                rest += " NOT NULL"
        return rest
    parts: list[str] = []
    if col.collation and database_collation and col.collation != database_collation:
        parts.append(f"COLLATE {col.collation}")
    if col.identity is not None:
        parts.append(f"IDENTITY ({col.identity[0]}, {col.identity[1]})")
    if col.default_definition is not None:
        parts.append(f"{_dacfx_named(col.default_name)}DEFAULT {col.default_definition}")
    parts.append("NULL" if col.nullable else "NOT NULL")
    return " ".join(parts)


def _dacfx_index(name: str, idx: Index) -> str:
    if idx.index_type == "CLUSTERED COLUMNSTORE":
        return f"CREATE CLUSTERED COLUMNSTORE INDEX {bracket(idx.name)}\n    ON {name};"
    unique = "UNIQUE " if idx.unique else ""
    if idx.index_type == "NONCLUSTERED COLUMNSTORE":
        cols = ", ".join(bracket(c) for c, _ in idx.columns)
    else:
        cols = _dacfx_cols(idx.columns)
    text = f"CREATE {unique}{idx.index_type} INDEX {bracket(idx.name)}\n    ON {name}({cols})"
    if idx.included:
        text += f"\n    INCLUDE({', '.join(bracket(c) for c in idx.included)})"
    if idx.filter_definition:
        text += f" WHERE {idx.filter_definition}"
    return text + ";"


def script_table_dacfx(table: Table, *, database_collation: str | None) -> str:
    """The table as DacFx scripts it into a project (checked against SSMS 22's DacFx 170
    Extract): CREATE TABLE with its columns aligned and its constraints inside, primary
    key first, then checks, foreign keys and unique keys; a disabled constraint's
    NOCHECK right after it; then each index. Every statement ends with two blank lines
    and GO, and a blank line parts the table from each index (and the file ends with
    one; layout.py writes it, normalize() takes it off).

    DacFx does not script whether a constraint is trusted: one added WITH NOCHECK reads
    as an ordinary one, and only a disabled one is told apart, by its NOCHECK CONSTRAINT."""
    name = f"{bracket(table.schema)}.{bracket(table.name)}"
    rows: list[tuple[str, str, str]] = []
    for col in table.columns:
        kind = "AS" if col.computed is not None else dacfx_type(col)
        rows.append((bracket(col.name), kind, _dacfx_column_rest(col, database_collation)))
    name_width = max((len(r[0]) for r in rows), default=0)
    type_width = max((len(r[1]) for r in rows), default=0)
    items = [f"{n.ljust(name_width)} {t.ljust(type_width)} {rest}" for n, t, rest in rows]

    def order(name: str | None) -> tuple[bool, str]:
        return (name is not None, (name or "").lower())

    primary = [k for k in table.keys if k.primary]
    unique = sorted((k for k in table.keys if not k.primary), key=lambda k: (*order(k.name), _cols(k.columns)))
    checks = sorted(table.checks, key=lambda c: (*order(c.name), c.definition))
    fks = sorted(table.foreign_keys, key=lambda f: (*order(f.name), f.columns))
    items += [_dacfx_key(k) for k in primary]
    items += [_dacfx_check(c) for c in checks]
    items += [_dacfx_fk(f) for f in fks]
    items += [_dacfx_key(k) for k in unique]
    disabled = [c.name for c in [*checks, *fks] if c.disabled and c.name]

    lines = [f"CREATE TABLE {name} ("]
    lines += [f"    {item}," for item in items[:-1]]
    if items:
        lines.append(f"    {items[-1]}")
    statements = ["\n".join(lines) + "\n);"]
    statements += [f"ALTER TABLE {name} NOCHECK CONSTRAINT {bracket(n)};" for n in disabled]
    blocks = ["".join(f"{s}\n\n\nGO\n" for s in statements)]
    blocks += [f"{_dacfx_index(name, idx)}\n\n\nGO\n" for idx in sorted(table.indexes, key=lambda i: i.name.lower())]
    return normalize("\n".join(blocks))


def script_table(table: Table, *, database_collation: str | None, enforced_keys: bool = True) -> str:
    """The table's file text: DacFx's shape, or sqlsc's where keys are NOT ENFORCED
    (a Fabric Warehouse: DacFx's inline constraints would not run there)."""
    if not enforced_keys:
        return script_table_sqlsc(table, database_collation=database_collation, enforced_keys=False)
    return script_table_dacfx(table, database_collation=database_collation)


def script_module(definition: str, *, ansi_nulls: bool = True, quoted_identifier: bool = True) -> str:
    """A view, procedure, function or trigger: its own text, with SET options only when off."""
    lines: list[str] = []
    if not quoted_identifier:
        lines += ["SET QUOTED_IDENTIFIER OFF", "GO"]
    if not ansi_nulls:
        lines += ["SET ANSI_NULLS OFF", "GO"]
    lines.append(normalize(definition).rstrip("\n"))
    lines.append("GO")
    return normalize("\n".join(lines))


def script_schema(name: str, owner: str | None) -> str:
    text = f"CREATE SCHEMA {quote(name)}"
    if owner and owner != "dbo":
        text += f" AUTHORIZATION {quote(owner)}"
    return normalize(text + ";\nGO")
