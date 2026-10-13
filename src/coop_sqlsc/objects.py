"""The object model shared by the database side and the folder side.

Both sides reduce to the same thing: a map from an ObjectKey to the normalized
script text for that object. Comparing the two maps is then plain text equality.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
        return f"[{self.schema}].[{self.name}]"

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (list(OBJECT_TYPES).index(self.type), self.schema.lower(), self.name.lower())


def normalize(text: str) -> str:
    """Line endings to LF, no trailing spaces, no leading or trailing blank lines.

    Applied to both sides so a script that only differs in whitespace at line
    ends (an editor setting, a CRLF checkout on Windows) never shows as a change.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.rstrip() for line in lines]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


_GO = re.compile(r"^[ \t]*GO[ \t]*(?:--[^\n]*)?$", re.IGNORECASE | re.MULTILINE)


def batches(text: str) -> list[str]:
    """A script split on its GO lines, the way SSMS and sqlcmd split it."""
    return [b.strip() for b in _GO.split(text) if b.strip()]
