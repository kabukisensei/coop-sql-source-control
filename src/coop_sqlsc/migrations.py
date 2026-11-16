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

from . import sqltext
from .objects import ObjectKey, batches

FOLDER = "Migrations"
BEFORE, AFTER = "before", "after"
# sqlsc.Migration's MigrationId is varchar(200): a longer id could not be recorded as
# it is, and the script would run again on every update (#51). Checked before any
# migration runs; new_file never makes one.
MAX_ID = 200
# The marker is one line comment at the head of the script: among the first three
# lines and before any code. A marker-shaped line inside a string literal, a quoted
# name or a block comment is that literal's text, not a directive (#92).
_WHEN = re.compile(r"^--\s*sqlsc\s*:\s*(before|after)\s*$", re.IGNORECASE)
_HEADER_LINES = 3

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


def _header(text: str) -> tuple[str, int, int] | None:
    """The marker's (when, start, end) when the script opens with one, else None.

    Walks the script's lexical runs from the top: whitespace and block comments are
    skipped, a line comment is read as a candidate, and the first code stops the
    search. `end` is past the marker line's line break, so text[start:end] is the
    whole line."""
    pos = 0
    for kind, run in sqltext.runs(text):
        start, pos = pos, pos + len(run)
        if text.count("\n", 0, start) >= _HEADER_LINES:
            return None
        if kind == "comment" and run.startswith("--"):
            match = _WHEN.match(run.rstrip("\r"))
            if match:
                end = pos + 1 if text.startswith("\n", pos) else pos
                return match.group(1).lower(), start, end
        elif kind != "comment" and run.strip():
            return None
    return None


def parse(id_: str, text: str) -> Migration:
    header = _header(text)
    return Migration(id_, text, header[0] if header else AFTER)


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


def too_long(migrations: list[Migration]) -> list[Migration]:
    """Migrations whose id sqlsc.Migration cannot hold (#51): they must not run."""
    return [m for m in migrations if len(m.id) > MAX_ID]


def too_long_text(migrations: list[Migration]) -> str:
    names = ", ".join(f"{m.path} ({len(m.id)} characters)" for m in migrations)
    return (
        f"The name of {names} is longer than the {MAX_ID} characters sqlsc.Migration records, so a run "
        "could not be recorded and the script would run again on every update. Rename the file "
        "(git mv) to a shorter name."
    )


def record_sql(migration: Migration, who: str, commit: str | None) -> str:
    def text(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    if len(migration.id) > MAX_ID:
        raise ValueError(too_long_text([migration]))
    return (
        "INSERT INTO sqlsc.Migration (MigrationId, AppliedAt, AppliedBy, LoginName, Checksum, CommitId)\n"
        f"VALUES ({text(migration.id)}, SYSUTCDATETIME(), {text(who[:256])}, SUSER_SNAME(), "
        f"{text(migration.checksum)}, {text(commit[:40]) if commit else 'NULL'})"
    )


def statements(migration: Migration) -> list[str]:
    """The script's batches, without the `-- sqlsc:` marker line. Only the header
    line is taken out: a marker-shaped line in the body, or inside a literal, is
    the script's own text and stays as written (#92)."""
    header = _header(migration.text)
    if header is None:
        return batches(migration.text)
    _, start, end = header
    return batches(migration.text[:start] + migration.text[end:])


def new_file(root: Path, name: str, when: str = AFTER, now: datetime | None = None) -> Path:
    """Creates Migrations/<yyyymmddHHMMSS>_<name>.sql with a short header, and returns it.

    Stamped to the second so two scripts made in a row run in the order they were made."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-") or "migration"
    slug = slug[: MAX_ID - len(stamp) - 1].rstrip("-")  # the whole id must fit sqlsc.Migration (#51)
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
