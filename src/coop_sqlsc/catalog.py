"""Reading a live database's objects through the system catalog.

Read-only: every statement here is a fixed SELECT on catalog views. Optional
facts (indexes, triggers, keys) are read only where the target kind has them;
when a platform lacks a catalog view, that fact is skipped with a warning
instead of failing the whole read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Target
from .objects import ObjectKey
from .scripter import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    KeyConstraint,
    Table,
    script_module,
    script_schema,
    script_table,
)

# The sqlsc schema holds this tool's own lock table; it is never scripted.
# sysdiagrams and its helper procedures come from SSMS, not from the team.
_DIAGRAM_OBJECTS = {
    "sysdiagrams",
    "sp_upgraddiagrams",
    "sp_helpdiagrams",
    "sp_helpdiagramdefinition",
    "sp_creatediagram",
    "sp_renamediagram",
    "sp_alterdiagram",
    "sp_dropdiagram",
    "fn_diagramobjects",
}
# Schemas Fabric creates in every Warehouse, Lakehouse endpoint and SQL database:
# query insights views and the result-set cache. They are the platform's, not the team's.
_FABRIC_SYSTEM_SCHEMAS = {"queryinsights", "_rsc"}


def platform_schemas(target: Target) -> set[str]:
    """Schemas the platform owns on this target: never scripted, and files for them
    (from a repo scripted before they were skipped) are ignored rather than applied."""
    return _FABRIC_SYSTEM_SCHEMAS if target.kind.startswith("fabric_") else set()


def in_scope(target: Target, schema: str) -> bool:
    """Whether objects in this schema are ours: not the platform's, and not left out by
    sqlsc.yml's schemas / exclude_schemas. Out of scope is never scripted, compared
    or applied; files for it stay in the repo untouched."""
    return schema not in platform_schemas(target) and target.keeps_schema(schema)


def schema_of(key: ObjectKey) -> str:
    return key.name if key.type == "schema" else key.schema


_MODULE_TYPES = {"V": "view", "P": "procedure", "FN": "function", "IF": "function", "TF": "function", "TR": "trigger"}

OBJECTS_SQL = """
SELECT o.object_id, s.name, o.name, RTRIM(o.type)
FROM sys.objects AS o
JOIN sys.schemas AS s ON s.schema_id = o.schema_id
WHERE o.is_ms_shipped = 0
  AND o.type IN ('U', 'V', 'P', 'FN', 'IF', 'TF', 'TR')
  AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'sqlsc')
"""

SCHEMAS_SQL = """
SELECT s.name, p.name
FROM sys.schemas AS s
LEFT JOIN sys.database_principals AS p ON p.principal_id = s.principal_id
WHERE s.schema_id BETWEEN 5 AND 16383 AND s.name <> 'sqlsc'
"""

MODULES_SQL = """
SELECT m.object_id, m.definition, m.uses_ansi_nulls, m.uses_quoted_identifier
FROM sys.sql_modules AS m
JOIN sys.objects AS o ON o.object_id = m.object_id
WHERE o.is_ms_shipped = 0
"""

COLUMNS_SQL = """
SELECT c.object_id, c.column_id, c.name, t.name, SCHEMA_NAME(t.schema_id), t.is_user_defined,
       c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity, c.is_computed, c.collation_name
FROM sys.columns AS c
JOIN sys.types AS t ON t.user_type_id = c.user_type_id
JOIN sys.tables AS tb ON tb.object_id = c.object_id
ORDER BY c.object_id, c.column_id
"""

IDENTITY_SQL = """
SELECT object_id, column_id, CAST(seed_value AS varchar(40)), CAST(increment_value AS varchar(40))
FROM sys.identity_columns
"""

COMPUTED_SQL = "SELECT object_id, column_id, definition, is_persisted FROM sys.computed_columns"

DEFAULTS_SQL = """
SELECT parent_object_id, parent_column_id, name, definition, is_system_named
FROM sys.default_constraints
"""

KEYS_SQL = """
SELECT k.parent_object_id, k.name, k.type, i.type_desc, c.name, ic.is_descending_key, k.is_system_named
FROM sys.key_constraints AS k
JOIN sys.indexes AS i ON i.object_id = k.parent_object_id AND i.index_id = k.unique_index_id
JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
ORDER BY k.parent_object_id, k.name, ic.key_ordinal
"""

INDEXES_SQL = """
SELECT i.object_id, i.name, i.is_unique, i.type_desc, i.has_filter, i.filter_definition,
       c.name, ic.is_descending_key, ic.is_included_column, ic.key_ordinal, ic.index_column_id
FROM sys.indexes AS i
JOIN sys.tables AS t ON t.object_id = i.object_id
LEFT JOIN sys.index_columns AS ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
LEFT JOIN sys.columns AS c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE i.type IN (1, 2, 5, 6) AND i.is_primary_key = 0 AND i.is_unique_constraint = 0
  AND i.is_hypothetical = 0
ORDER BY i.object_id, i.name, ic.key_ordinal, ic.index_column_id
"""

CHECKS_SQL = """
SELECT parent_object_id, name, definition, is_not_trusted, is_disabled, is_system_named
FROM sys.check_constraints
"""

FKS_SQL = """
SELECT f.parent_object_id, f.name, SCHEMA_NAME(r.schema_id), r.name,
       f.delete_referential_action_desc, f.update_referential_action_desc,
       f.is_not_trusted, f.is_disabled, pc.name, rc.name, f.is_system_named
FROM sys.foreign_keys AS f
JOIN sys.objects AS r ON r.object_id = f.referenced_object_id
JOIN sys.foreign_key_columns AS fc ON fc.constraint_object_id = f.object_id
JOIN sys.columns AS pc ON pc.object_id = fc.parent_object_id AND pc.column_id = fc.parent_column_id
JOIN sys.columns AS rc ON rc.object_id = fc.referenced_object_id AND rc.column_id = fc.referenced_column_id
ORDER BY f.parent_object_id, f.name, fc.constraint_column_id
"""

COLLATION_SQL = "SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS nvarchar(128))"


@dataclass
class CatalogResult:
    objects: dict[ObjectKey, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def read_database(conn: Any, target: Target) -> CatalogResult:
    caps = target.caps
    result = CatalogResult()
    cur = conn.cursor()

    def rows(sql: str, what: str, *, required: bool = False) -> list[Any]:
        try:
            cur.execute(sql)
            return cur.fetchall()
        except Exception as exc:  # pyodbc.Error, kept generic for fakes in tests
            if required:
                raise
            result.warnings.append(f"Skipped {what}: {target.label} did not answer ({_first_line(exc)}).")
            return []

    collation_rows = rows(COLLATION_SQL, "collation")
    collation = collation_rows[0][0] if collation_rows else None

    for name, owner in rows(SCHEMAS_SQL, "schemas"):
        if not in_scope(target, name):
            continue
        result.objects[ObjectKey("schema", "", name)] = script_schema(name, owner)

    objects = rows(OBJECTS_SQL, "objects", required=True)
    tables: dict[int, Table] = {}
    module_keys: dict[int, ObjectKey] = {}
    for object_id, schema, name, otype in objects:
        if (schema == "dbo" and name in _DIAGRAM_OBJECTS) or not in_scope(target, schema):
            continue
        if otype == "U":
            if caps["tables"]:
                tables[object_id] = Table(schema=schema, name=name)
        elif otype in _MODULE_TYPES:
            obj_type = _MODULE_TYPES[otype]
            if obj_type == "trigger" and not caps["triggers"]:
                continue
            module_keys[object_id] = ObjectKey(obj_type, schema, name)

    if module_keys:
        for object_id, definition, ansi_nulls, quoted_identifier in rows(MODULES_SQL, "definitions", required=True):
            key = module_keys.get(object_id)
            if key is None:
                continue
            if definition is None:
                result.warnings.append(f"{key.display} is encrypted; its definition cannot be scripted.")
                continue
            result.objects[key] = script_module(
                definition, ansi_nulls=bool(ansi_nulls), quoted_identifier=bool(quoted_identifier)
            )

    if tables:
        _read_tables(rows, tables, caps)
        for table in tables.values():
            key = ObjectKey("table", table.schema, table.name)
            result.objects[key] = script_table(table, database_collation=collation, enforced_keys=caps["enforced_keys"])
    return result


def _read_tables(rows: Any, tables: dict[int, Table], caps: dict[str, Any]) -> None:
    identity = {(r[0], r[1]): (r[2], r[3]) for r in rows(IDENTITY_SQL, "identity columns")}
    computed = {(r[0], r[1]): (r[2], bool(r[3])) for r in rows(COMPUTED_SQL, "computed columns")}
    defaults = {(r[0], r[1]): (None if r[4] else r[2], r[3]) for r in rows(DEFAULTS_SQL, "defaults")}

    for (
        object_id,
        column_id,
        name,
        type_name,
        type_schema,
        user_type,
        max_length,
        precision,
        scale,
        nullable,
        is_identity,
        is_computed,
        collation,
    ) in rows(COLUMNS_SQL, "columns", required=True):
        table = tables.get(object_id)
        if table is None:
            continue
        col = Column(
            name=name,
            type_name=type_name,
            type_schema=type_schema,
            user_type=bool(user_type),
            max_length=int(max_length),
            precision=int(precision),
            scale=int(scale),
            nullable=bool(nullable),
            collation=collation,
        )
        if is_identity and (object_id, column_id) in identity:
            col.identity = identity[(object_id, column_id)]
        if is_computed and (object_id, column_id) in computed:
            col.computed, col.persisted = computed[(object_id, column_id)]
        if (object_id, column_id) in defaults:
            col.default_name, col.default_definition = defaults[(object_id, column_id)]
        table.columns.append(col)

    keys: dict[tuple[int, str], KeyConstraint] = {}
    for parent, name, ktype, type_desc, column, desc, system_named in rows(KEYS_SQL, "keys"):
        if parent not in tables:
            continue
        key = keys.get((parent, name))
        if key is None:
            key = KeyConstraint(
                name=None if system_named else name, primary=ktype.strip() == "PK", index_type=type_desc, columns=[]
            )
            keys[(parent, name)] = key
            tables[parent].keys.append(key)
        key.columns.append((column, bool(desc)))

    if caps["indexes"]:
        indexes: dict[tuple[int, str], Index] = {}
        for (
            object_id,
            name,
            unique,
            type_desc,
            has_filter,
            filter_def,
            column,
            desc,
            included,
            _ordinal,
            _col_id,
        ) in rows(INDEXES_SQL, "indexes"):
            if object_id not in tables or name is None:
                continue
            idx = indexes.get((object_id, name))
            if idx is None:
                idx = Index(
                    name=name,
                    unique=bool(unique),
                    index_type=type_desc.replace("_", " "),
                    columns=[],
                    filter_definition=filter_def if has_filter else None,
                )
                indexes[(object_id, name)] = idx
                tables[object_id].indexes.append(idx)
            if column is None or idx.index_type == "CLUSTERED COLUMNSTORE":
                continue
            if idx.index_type == "NONCLUSTERED COLUMNSTORE":
                idx.columns.append((column, False))
            elif included:
                idx.included.append(column)
            else:
                idx.columns.append((column, bool(desc)))

    for parent, name, definition, not_trusted, disabled, system_named in rows(CHECKS_SQL, "check constraints"):
        if parent in tables:
            tables[parent].checks.append(
                CheckConstraint(None if system_named else name, definition, bool(not_trusted), bool(disabled))
            )

    fks: dict[tuple[int, str], ForeignKey] = {}
    for (
        parent,
        name,
        ref_schema,
        ref_table,
        on_delete,
        on_update,
        not_trusted,
        disabled,
        column,
        ref_column,
        system_named,
    ) in rows(FKS_SQL, "foreign keys"):
        if parent not in tables:
            continue
        fk = fks.get((parent, name))
        if fk is None:
            fk = ForeignKey(
                name=None if system_named else name,
                columns=[],
                ref_schema=ref_schema,
                ref_table=ref_table,
                ref_columns=[],
                on_delete=on_delete,
                on_update=on_update,
                not_trusted=bool(not_trusted),
                disabled=bool(disabled),
            )
            fks[(parent, name)] = fk
            tables[parent].foreign_keys.append(fk)
        fk.columns.append(column)
        fk.ref_columns.append(ref_column)


def _first_line(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0][:200] if text else type(exc).__name__
