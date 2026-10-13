"""Static data: the rows of small lookup tables, kept in the repo as MERGE scripts.

A table's data is tracked when the repo has `Data/<schema>.<table>.sql` (the
same idea as Redgate's static data, with the file itself as the switch):
`sqlsc data add` writes it, `sqlsc data remove` deletes it, and from then on
status, diff, commit, get-latest and deploy treat the rows like any other
object.

The script is one MERGE that makes the table hold exactly the rows in the
file: missing rows are inserted, differing ones updated and extra ones deleted,
matched on the primary key. Values are read through CONVERT on the server so
every type writes the same text every time (dates in ISO 8601, floats with
all 17 digits), and the same text is what a later read produces after the
script has run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Target
from .objects import ObjectKey, normalize
from .scripter import Column, qualified, quote, type_text

LARGE_TABLE = 1000

COLUMNS_SQL = """
SELECT c.name, CASE WHEN t.is_user_defined = 0 THEN t.name ELSE TYPE_NAME(c.system_type_id) END,
       c.max_length, c.precision, c.scale, c.is_identity, c.is_computed
FROM sys.columns AS c
JOIN sys.tables AS tb ON tb.object_id = c.object_id
JOIN sys.types AS t ON t.user_type_id = c.user_type_id
WHERE c.object_id = OBJECT_ID(?)
ORDER BY c.column_id
"""

# The primary key, else the first unique constraint: the columns rows are matched on.
KEY_SQL = """
SELECT k.type, k.name, c.name
FROM sys.key_constraints AS k
JOIN sys.index_columns AS ic ON ic.object_id = k.parent_object_id AND ic.index_id = k.unique_index_id
JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE k.parent_object_id = OBJECT_ID(?)
ORDER BY CASE k.type WHEN 'PK' THEN 0 ELSE 1 END, k.name, ic.key_ordinal
"""

_NUMBER = {"bigint", "int", "smallint", "tinyint", "bit", "decimal", "numeric"}
_MONEY = {"money", "smallmoney"}
_FLOAT = {"float", "real"}
_DATES = {"date", "time", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}
_TEXT = {"char", "varchar", "nchar", "nvarchar", "sysname"}
_LOB_TEXT = {"text", "ntext", "xml"}
_BINARY = {"binary", "varbinary", "image", "hierarchyid"}
_SKIPPED = {"timestamp"}  # rowversion: the server sets it, nobody can insert it
_UNSUPPORTED = {"geography", "geometry", "sql_variant"}


class DataError(Exception):
    """A table whose rows cannot be kept as static data; the message says why."""


class TableMissing(DataError):
    """The table does not exist in this database (yet)."""


@dataclass
class DataColumn:
    name: str
    base_type: str
    sql_type: str  # what the source rows are cast to
    identity: bool
    kind: str  # num, str, nstr, bin

    def select_expression(self) -> str:
        col = quote(self.name)
        if self.base_type in _MONEY:
            return f"CONVERT(varchar(60), {col}, 2)"
        if self.base_type in _FLOAT:
            return f"CONVERT(varchar(60), {col}, 3)"
        if self.base_type in _NUMBER:
            return f"CAST({col} AS varchar(60))"
        if self.base_type in _DATES:
            return f"CONVERT(varchar(60), {col}, 126)"
        if self.base_type == "uniqueidentifier":
            return f"CONVERT(char(36), {col})"
        if self.base_type in _LOB_TEXT:
            return f"CAST({col} AS nvarchar(max))"
        if self.base_type in _BINARY:
            return f"CAST({col} AS varbinary(max))"
        return col


def track_columns(conn: Any, key: ObjectKey) -> tuple[list[DataColumn], list[str]]:
    """The columns a data script holds, and the key columns rows are matched on."""
    name = qualified(key.schema, key.name)
    cur = conn.cursor()
    rows = cur.execute(COLUMNS_SQL, name).fetchall()
    if not rows:
        raise TableMissing(f"{key.display} is not a table in this database.")
    columns: list[DataColumn] = []
    for col_name, base, max_length, precision, scale, identity, computed in rows:
        if computed or base in _SKIPPED:
            continue
        if base in _UNSUPPORTED:
            raise DataError(f"{key.display} has a {base} column ({col_name}); static data cannot hold {base} values.")
        sql_type = _cast_type(base, int(max_length), int(precision), int(scale))
        columns.append(DataColumn(col_name, base, sql_type, bool(identity), _kind(base)))
    key_rows = cur.execute(KEY_SQL, name).fetchall()
    if not key_rows:
        raise DataError(f"{key.display} has no primary key or unique constraint, so its rows cannot be matched.")
    first = (key_rows[0][0], key_rows[0][1])
    key_columns = [r[2] for r in key_rows if (r[0], r[1]) == first]
    return columns, key_columns


def script_data(conn: Any, key: ObjectKey, target: Target) -> tuple[str, int]:
    """The data script for one table, and how many rows it holds."""
    columns, key_columns = track_columns(conn, key)
    table = qualified(key.schema, key.name)
    select = ", ".join(c.select_expression() for c in columns)
    order = ", ".join(quote(c) for c in key_columns)
    rows = conn.cursor().execute(f"SELECT {select} FROM {table} ORDER BY {order}").fetchall()
    return render(
        key, columns, key_columns, [tuple(r) for r in rows], binary_compare=target.kind != "fabric_warehouse"
    ), len(rows)


def render(
    key: ObjectKey,
    columns: list[DataColumn],
    key_columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    binary_compare: bool = True,
) -> str:
    table = qualified(key.schema, key.name)
    match_text = ", ".join(key_columns)
    if not rows:
        return normalize(f"-- Static data for {table}: no rows.\nDELETE FROM {table};\nGO\n")
    names = [quote(c.name) for c in columns]
    identity = any(c.identity for c in columns)
    keys = set(key_columns)
    updatable = [c for c in columns if c.name not in keys and not c.identity]

    lines = [f"-- Static data for {table}, kept by sqlsc. Rows are matched on {match_text}."]
    if identity:
        lines += [f"SET IDENTITY_INSERT {table} ON;", "GO"]
    lines += [f"MERGE INTO {table} AS target", "USING", "(", "    SELECT"]
    for i, (col, name) in enumerate(zip(columns, names, strict=True)):
        lines.append(f"    {'      ' if i == 0 else '     ,'}CAST(v.{name} AS {col.sql_type}) AS {name}")
    lines += ["    FROM", "    (", "        VALUES"]
    for i, row in enumerate(rows):
        values = ", ".join(_literal(value, col.kind) for value, col in zip(row, columns, strict=True))
        lines.append(f"        {'      ' if i == 0 else '     ,'}({values})")
    lines += [f"    ) AS v ({', '.join(names)})", ") AS source"]
    lines.append("ON " + " AND ".join(f"target.{quote(c)} = source.{quote(c)}" for c in key_columns))
    if updatable:
        left = ", ".join(_comparable("target", c, binary_compare) for c in updatable)
        right = ", ".join(_comparable("source", c, binary_compare) for c in updatable)
        lines += ["WHEN MATCHED AND EXISTS", "(", f"    SELECT {left}", "    EXCEPT", f"    SELECT {right}", ") THEN"]
        lines.append("    UPDATE SET")
        for i, col in enumerate(updatable):
            lines.append(f"        {'      ' if i == 0 else '     ,'}{quote(col.name)} = source.{quote(col.name)}")
    lines.append("WHEN NOT MATCHED BY TARGET THEN")
    lines.append(f"    INSERT ({', '.join(names)}) VALUES ({', '.join('source.' + n for n in names)})")
    lines += ["WHEN NOT MATCHED BY SOURCE THEN", "    DELETE;", "GO"]
    if identity:
        lines += [f"SET IDENTITY_INSERT {table} OFF;", "GO"]
    return normalize("\n".join(lines))


def identity_off(text: str) -> str | None:
    """The statement that undoes the script's IDENTITY_INSERT if the MERGE fails part way."""
    for line in text.split("\n"):
        if line.startswith("SET IDENTITY_INSERT ") and line.endswith(" ON;"):
            return line[: -len(" ON;")] + " OFF"
    return None


def _comparable(side: str, col: DataColumn, binary_compare: bool) -> str:
    ref = f"{side}.{quote(col.name)}"
    if col.base_type in _LOB_TEXT:
        ref = f"CAST({ref} AS nvarchar(max))"
    elif col.base_type == "image":
        ref = f"CAST({ref} AS varbinary(max))"
    # Case and accent count: a lookup value changed from 'Ok' to 'OK' must update.
    if binary_compare and (col.base_type in _TEXT or col.base_type in _LOB_TEXT):
        ref += " COLLATE Latin1_General_100_BIN2"
    return ref


def _literal(value: Any, kind: str) -> str:
    if value is None:
        return "NULL"
    if kind == "num":
        return str(value)
    if kind == "bin":
        return "0x" + bytes(value).hex().upper()
    text = str(value)
    prefix = "N" if kind == "nstr" else ""
    # Control characters (line breaks, tabs) are written as NCHAR(n) so the
    # script's own line endings and trailing spaces never change a value.
    parts: list[str] = []
    run = ""
    for ch in text:
        if ord(ch) < 32:
            if run:
                parts.append(f"{prefix}'{run}'")
                run = ""
            parts.append(f"NCHAR({ord(ch)})")
        else:
            run += "''" if ch == "'" else ch
    if run or not parts:
        parts.append(f"{prefix}'{run}'")
    return " + ".join(parts)


def _kind(base: str) -> str:
    if base in _NUMBER or base in _MONEY or base in _FLOAT:
        return "num"
    if base in _BINARY:
        return "bin"
    if base in _DATES or base == "uniqueidentifier" or base in ("char", "varchar", "text"):
        return "str"
    return "nstr"


def _cast_type(base: str, max_length: int, precision: int, scale: int) -> str:
    if base in ("text", "ntext", "xml"):
        return "nvarchar(max)"
    if base == "image":
        return "varbinary(max)"
    if base == "hierarchyid":
        return "hierarchyid"
    return type_text(Column(name="", type_name=base, max_length=max_length, precision=precision, scale=scale))
