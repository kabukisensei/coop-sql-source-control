"""The object model shared by the database side and the folder side.

Both sides reduce to the same thing: a map from an ObjectKey to the normalized
script text for that object. Comparing the two maps is then plain text equality.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import sqltext

# Object type -> folder name in the repo. The order is also the order objects are
# listed in, and the order a deploy creates them in (schemas before tables, tables
# before the views that read them).
OBJECT_TYPES: dict[str, str] = {
    "schema": "Security/Schemas",
    "table": "Tables",
    "view": "Views",
    "function": "Functions",
    "procedure": "Stored Procedures",
    "trigger": "Triggers",
    "data": "Data",
}

TYPE_LABELS: dict[str, str] = {
    "schema": "Schema",
    "table": "Table",
    "view": "View",
    "function": "Function",
    "procedure": "Stored procedure",
    "trigger": "Trigger",
    "data": "Static data",
    "migration": "Migration",  # a Migrations/ script; not an object folder (see migrations.py)
}


@dataclass(frozen=True, order=True)
class ObjectKey:
    type: str
    schema: str
    name: str

    @property
    def display(self) -> str:
        if self.type == "schema":
            return f"[{self.name}]"
        if self.type == "migration":
            return f"Migrations/{self.name}.sql"
        return f"[{self.schema}].[{self.name}]"

    @property
    def sort_key(self) -> tuple[int, str, str]:
        order = list(OBJECT_TYPES).index(self.type) if self.type in OBJECT_TYPES else len(OBJECT_TYPES)
        return (order, self.schema.lower(), self.name.lower())


def normalize(text: str) -> str:
    """Line endings to LF, no trailing spaces, no leading or trailing blank lines,
    outside string literals and quoted names (sqltext.normalize).

    Applied to both sides so a script that only differs in whitespace at line
    ends (an editor setting, a CRLF checkout on Windows) never shows as a change,
    while a space or line break inside a literal is the literal's value and is
    kept, scripted and compared as it is.
    """
    return sqltext.normalize(text)


# sqlcmd's batch separator: `GO`, `GO 3`, `GO;`, with a line comment allowed after it.
# The same expression as coop-schema-compare's, so both tools cut a file alike.
_GO = re.compile(r"^[ \t]*GO(?:[ \t]+\d+)?[ \t]*;?[ \t]*(?:--[^\n]*)?$", re.IGNORECASE | re.MULTILINE)


def batches(text: str) -> list[str]:
    """A script split on its GO lines, the way SSMS and sqlcmd split it.

    A GO line counts only in plain code: a line reading GO inside a string literal,
    a quoted name or a block comment is the literal's, name's or comment's text and
    stays where it is (sqltext.runs tells the two apart). Each batch keeps its
    original bytes; only the GO lines are taken out.
    """
    masked = "".join(run if kind == "code" or run.startswith("--") else _mask(run) for kind, run in sqltext.runs(text))
    out, start = [], 0
    for match in _GO.finditer(masked):
        out.append(text[start : match.start()])
        start = match.end()
    out.append(text[start:])
    return [b.strip() for b in out if b.strip()]


def _mask(run: str) -> str:
    """The run with every character but a line break replaced, same length."""
    return "".join("\n" if ch == "\n" else "x" for ch in run)
