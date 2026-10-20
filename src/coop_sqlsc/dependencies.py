"""The database's own dependency list, kept in the repo for lineage tools.

`sqlsc dependencies` writes `Dependencies.json` at the root of the scripts folder:
what each view, procedure, function and trigger references, as the database
itself records it in sys.sql_expression_dependencies. From then on `commit` and
`checkin` refresh it, so the committed list matches the committed scripts.

coop-data-doc reads the file (its `catalog_dependencies` setting) and warns
about each reference its own parse of the scripts did not find, so a lineage
gap (dynamic SQL, an unparsed statement) shows up instead of passing silently.

The list says that one object references another, not whether it reads or
writes it. It is opt-in: no file, no query. The file is sorted and has no
timestamps, so it changes only when a dependency does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FILE = "Dependencies.json"
VERSION = 1

# One row per (referencing object, referenced object). referenced_schema_name is
# NULL when the module names the object without a schema; the resolved
# referenced_id then gives it, and when neither does the schema stays unknown
# (null in the file). Cross-database and cross-server references keep their
# database and server names.
DEPENDENCIES_SQL = """
SELECT DISTINCT
    rs.name, ro.name, RTRIM(ro.type),
    COALESCE(d.referenced_schema_name, ts.name), d.referenced_entity_name, RTRIM(t.type),
    d.referenced_database_name, d.referenced_server_name
FROM sys.sql_expression_dependencies AS d
JOIN sys.objects AS ro ON ro.object_id = d.referencing_id
JOIN sys.schemas AS rs ON rs.schema_id = ro.schema_id
LEFT JOIN sys.objects AS t ON t.object_id = d.referenced_id
LEFT JOIN sys.schemas AS ts ON ts.schema_id = t.schema_id
WHERE d.referencing_class = 1
  AND d.referenced_class = 1
  AND ro.is_ms_shipped = 0
  AND rs.name NOT IN ('sys', 'INFORMATION_SCHEMA', 'sqlsc')
"""

# sys.objects type codes, as words a reader (or another tool) understands.
_TYPES = {
    "U": "table",
    "V": "view",
    "P": "procedure",
    "FN": "function",
    "IF": "function",
    "TF": "function",
    "FS": "function",
    "FT": "function",
    "TR": "trigger",
    "SN": "synonym",
    "SO": "sequence",
}


@dataclass(frozen=True, order=True)
class Dependency:
    schema: str
    name: str
    type: str
    ref_schema: str | None
    ref_name: str
    ref_type: str | None
    ref_database: str | None
    ref_server: str | None

    def to_json(self) -> dict[str, Any]:
        ref: dict[str, Any] = {"schema": self.ref_schema, "name": self.ref_name, "type": self.ref_type}
        if self.ref_database:
            ref["database"] = self.ref_database
        if self.ref_server:
            ref["server"] = self.ref_server
        return {"object": {"schema": self.schema, "name": self.name, "type": self.type}, "references": ref}


def path(scripts_dir: Path) -> Path:
    return scripts_dir / FILE


def tracked(scripts_dir: Path) -> bool:
    return path(scripts_dir).is_file()


def read(conn: Any, skip_schemas: set[str] | frozenset[str] = frozenset()) -> list[Dependency]:
    """The database's references, sorted. Raises the driver's error when the
    platform has no sys.sql_expression_dependencies."""
    cur = conn.cursor()
    cur.execute(DEPENDENCIES_SQL)
    found: set[Dependency] = set()
    for schema, name, otype, ref_schema, ref_name, ref_type, ref_db, ref_server in cur.fetchall():
        if schema in skip_schemas or not ref_name:
            continue
        found.add(
            Dependency(
                schema=schema,
                name=name,
                type=_TYPES.get(otype or "", "object"),
                ref_schema=ref_schema or None,
                ref_name=ref_name,
                ref_type=_TYPES.get(ref_type or "") if ref_type else None,
                ref_database=ref_db or None,
                ref_server=ref_server or None,
            )
        )
    return sorted(found, key=_sort_key)


def _sort_key(dep: Dependency) -> tuple[str, ...]:
    return (
        dep.schema.lower(),
        dep.name.lower(),
        (dep.ref_server or "").lower(),
        (dep.ref_database or "").lower(),
        (dep.ref_schema or "").lower(),
        dep.ref_name.lower(),
    )


def render(deps: list[Dependency]) -> str:
    """The file's text: stable, so it only changes when a dependency does."""
    document = {
        "version": VERSION,
        "source": "sys.sql_expression_dependencies",
        "dependencies": [d.to_json() for d in deps],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def write(scripts_dir: Path, text: str) -> bool:
    """Writes the file when its text changed; returns whether it did."""
    target = path(scripts_dir)
    if target.is_file() and target.read_text(encoding="utf-8") == text:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return True
