"""Migration scripts: hand-written SQL that runs once per database.

Most changes need no script: get-latest and deploy work them out from the
object files. A few cannot be worked out, or must move data as well as
structure: splitting a column, backfilling a new NOT NULL column, renaming a
table without losing its rows. Those go in `Migrations/` next to the object
folders, one file each, named so they sort in the order they should run:

    database/Migrations/202610201030_split-customer-name.sql

Every update (get-latest, deploy) runs the ones the database has not had yet,
in file-name order, and records each in `sqlsc.Migration` so it never runs
twice there. A script runs after the structural changes by default, where it
can rely on the new columns and tables. One whose first line is

    -- sqlsc: before

runs before them instead, for work the structural change depends on (filling a
column before it becomes NOT NULL). An applied script that is edited later is
not run again; `sqlsc migrations` points it out.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .objects import ObjectKey, batches

FOLDER = "Migrations"
BEFORE, AFTER = "before", "after"
_WHEN = re.compile(r"^\s*--\s*sqlsc\s*:\s*(before|after)\s*$", re.IGNORECASE | re.MULTILINE)

CREATE_TABLE = """
IF OBJECT_ID('sqlsc.Migration') IS NULL
CREATE TABLE sqlsc.Migration
(
      MigrationId varchar(200) NOT NULL
     ,AppliedAt datetime2(6) NOT NULL
     ,AppliedBy varchar(256) NOT NULL
     ,LoginName varchar(256) NOT NULL
     ,Checksum varchar(64) NOT NULL
     ,CommitId varchar(40) NULL
)
""".strip()
APPLIED_SQL = "SELECT MigrationId, AppliedAt, AppliedBy, Checksum FROM sqlsc.Migration"


def _checksum_text(text: str) -> str:
    """The text a checksum is taken over: line ends trimmed everywhere, the way every
    release before 1.0.4 did it, so a migration already applied keeps its checksum and
    is not reported as edited. (Object scripts keep spaces inside literals; a checksum
    only has to stay stable.)"""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


@dataclass(frozen=True)
class Migration:
    id: str  # the file name without .sql
    text: str
    when: str  # BEFORE or AFTER the structural changes

    @property
    def checksum(self) -> str:
        return hashlib.sha256(_checksum_text(self.text).encode("utf-8")).hexdigest()[:32]

    @property
    def key(self) -> ObjectKey:
        return ObjectKey("migration", "", self.id)

    @property
    def path(self) -> str:
        return f"{FOLDER}/{self.id}.sql"

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "path": self.path, "when": self.when, "checksum": self.checksum}


@dataclass(frozen=True)
class Applied:
    id: str
    at: datetime
    by: str
    checksum: str

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "applied_at": self.at.replace(tzinfo=timezone.utc).isoformat(), "applied_by": self.by}


def parse(id_: str, text: str) -> Migration:
    head = "\n".join(text.split("\n", 3)[:3])  # the marker is a header line, not buried in the body
    match = _WHEN.search(head)
    return Migration(id_, text, match.group(1).lower() if match else AFTER)


def from_files(files: dict[str, str]) -> list[Migration]:
    """The migrations in {path relative to the scripts folder: text}, in run order."""
    found: list[Migration] = []
    for rel in sorted(files, key=str.lower):
        folder, _, name = rel.replace("\\", "/").rpartition("/")
        if folder.lower() == FOLDER.lower() and name.lower().endswith(".sql"):
            found.append(parse(name[:-4], files[rel].lstrip("﻿")))
    return found


def read_folder(root: Path) -> list[Migration]:
    base = root / FOLDER
    if not base.is_dir():
        return []
    files = {f"{FOLDER}/{p.name}": p.read_text(encoding="utf-8-sig") for p in base.iterdir() if p.is_file()}
    return from_files(files)


def read_applied(conn: Any) -> dict[str, Applied]:
    """What sqlsc.Migration says has run here; empty when the table does not exist yet."""
    cur = conn.cursor()
    exists = cur.execute("SELECT CASE WHEN OBJECT_ID('sqlsc.Migration') IS NULL THEN 0 ELSE 1 END").fetchone()[0]
    if not exists:
        return {}
    return {r[0]: Applied(r[0], r[1], r[2], r[3]) for r in cur.execute(APPLIED_SQL).fetchall()}


def pending(migrations: list[Migration], applied: dict[str, Applied]) -> list[Migration]:
    return [m for m in migrations if m.id not in applied]


def edited(migrations: list[Migration], applied: dict[str, Applied]) -> list[Migration]:
    """Applied migrations whose file changed since: they will not run again."""
    return [m for m in migrations if m.id in applied and applied[m.id].checksum != m.checksum]


def record_sql(migration: Migration, who: str, commit: str | None) -> str:
    def text(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    return (
        "INSERT INTO sqlsc.Migration (MigrationId, AppliedAt, AppliedBy, LoginName, Checksum, CommitId)\n"
        f"VALUES ({text(migration.id[:200])}, SYSUTCDATETIME(), {text(who[:256])}, SUSER_SNAME(), "
        f"{text(migration.checksum)}, {text(commit[:40]) if commit else 'NULL'})"
    )


def statements(migration: Migration) -> list[str]:
    """The script's batches, without the `-- sqlsc:` marker line."""
    return batches(_WHEN.sub("", migration.text, count=1))


def new_file(root: Path, name: str, when: str = AFTER, now: datetime | None = None) -> Path:
    """Creates Migrations/<yyyymmddHHMMSS>_<name>.sql with a short header, and returns it.

    Stamped to the second so two scripts made in a row run in the order they were made."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-") or "migration"
    base = root / FOLDER
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{stamp}_{slug}.sql"
    lines = [
        f"-- sqlsc: {when}",
        f"-- Runs once per database, {when} the structural changes of the same update.",
        "-- Separate batches with GO. Keep it re-runnable where you can (IF EXISTS, MERGE).",
        "",
    ]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
    return path
