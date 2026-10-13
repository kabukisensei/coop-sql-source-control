"""Database versus folder: which objects differ, and how.

Directions are named from the database's point of view, like Redgate's
"Commit changes" tab: an object that exists only in the database is `new`
(not yet in source control), one that differs is `changed`, and one that
exists only in the folder is `deleted` (dropped from the database, or not yet
applied to it).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from .layout import relative_path
from .objects import TYPE_LABELS, ObjectKey, batches

NEW, CHANGED, DELETED = "new", "changed", "deleted"


@dataclass(frozen=True)
class Change:
    key: ObjectKey
    action: str
    db_text: str | None
    file_text: str | None

    @property
    def path(self) -> str:
        return relative_path(self.key)

    def to_json(self) -> dict[str, str]:
        return {
            "action": self.action,
            "type": self.key.type,
            "schema": self.key.schema,
            "name": self.key.name,
            "object": self.key.display,
            "path": self.path,
        }

    def unified_diff(self, *, context: int = 3) -> str:
        before = (self.file_text or "").splitlines(keepends=True)
        after = (self.db_text or "").splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(before, after, fromfile=f"repo/{self.path}", tofile=f"database/{self.path}", n=context)
        )


def compare(db: dict[ObjectKey, str], folder: dict[ObjectKey, str]) -> list[Change]:
    changes: list[Change] = []
    for key in db.keys() | folder.keys():
        in_db, in_folder = key in db, key in folder
        if in_db and not in_folder:
            changes.append(Change(key, NEW, db[key], None))
        elif in_folder and not in_db:
            changes.append(Change(key, DELETED, None, folder[key]))
        elif comparable(key, db[key]) != comparable(key, folder[key]):
            changes.append(Change(key, CHANGED, db[key], folder[key]))
    changes.sort(key=lambda c: c.key.sort_key)
    return changes


def comparable(key: ObjectKey, text: str) -> str:
    """The text two sides are compared on. For a table, the order of its columns and
    of the statements after CREATE TABLE does not count: ALTER TABLE can only add
    columns at the end, so a column inserted mid-table in the repo would otherwise
    differ forever (SQL Compare ignores column order by default too).
    """
    if key.type != "table":
        return text
    lines = text.split("\n")
    try:
        open_at, close_at = lines.index("("), lines.index(");")
    except ValueError:
        return text
    columns = sorted(line.strip().lstrip(",").strip() for line in lines[open_at + 1 : close_at])
    rest = sorted(batches("\n".join(lines[close_at + 1 :])))
    return "\n".join(lines[: open_at + 1] + columns + [");"] + rest)


def summarize(changes: list[Change]) -> dict[str, int]:
    counts = {NEW: 0, CHANGED: 0, DELETED: 0}
    for change in changes:
        counts[change.action] += 1
    return counts


def matches(key: ObjectKey, pattern: str) -> bool:
    """A user's object name: `dbo.Customer`, `[dbo].[Customer]`, `Customer` or a schema name."""
    wanted = pattern.replace("[", "").replace("]", "").strip().lower()
    full = f"{key.schema}.{key.name}".lower() if key.schema else key.name.lower()
    return wanted in (full, key.name.lower()) or wanted == key.display.lower()


__all__ = ["Change", "compare", "summarize", "matches", "NEW", "CHANGED", "DELETED", "TYPE_LABELS"]
