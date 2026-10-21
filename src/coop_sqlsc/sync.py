"""The sync record: what this working copy and a database last agreed on.

Without it, an object that is in the repo but not in the database reads one
way only: "deleted". That is right when the object was dropped from the
database (commit should remove its file) and wrong when a teammate committed
a new object you have not applied yet (commit would throw their work away).
The same ambiguity hides in every changed object: did the database move, or
did the repo?

So each time an object is seen to match on both sides (after status, script,
commit or get-latest), its text is remembered as the last synced version, per
target, in `.sqlsc/sync/<target>.json` next to sqlsc.yml. A later difference
is then read against that record: the side that still matches it is the side
that did not change. The record belongs to one working copy and one database,
so it stays out of git (init adds `.sqlsc/` to .gitignore). Redgate keeps the
same thing in its per-user working base.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .layout import key_for_path, relative_path
from .objects import ObjectKey

SYNC_DIR = Path(".sqlsc") / "sync"

# Which side moved since the last sync, as seen from the database.
DATABASE, REPO, BOTH, UNKNOWN = "database", "repo", "both", "unknown"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SyncRecord:
    """{object: digest of its comparable text when both sides last matched}."""

    def __init__(self, path: Path | None, hashes: dict[ObjectKey, str] | None = None):
        self.path = path
        self.hashes: dict[ObjectKey, str] = dict(hashes or {})
        self.dirty = False

    @classmethod
    def load(cls, root: Path, target_name: str) -> SyncRecord:
        path = root / SYNC_DIR / f"{_safe(target_name)}.json"
        hashes: dict[ObjectKey, str] = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            for rel, value in (raw.get("objects") or {}).items():
                key = key_for_path(rel)
                if key is not None and isinstance(value, str):
                    hashes[key] = value
        return cls(path, hashes)

    def save(self) -> None:
        if self.path is None or not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "note": "Written by sqlsc: the last text each object was seen with on both sides. Safe to delete.",
            "objects": {relative_path(k): v for k, v in sorted(self.hashes.items(), key=lambda kv: kv[0].sort_key)},
        }
        self.path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        self.dirty = False

    def __contains__(self, key: ObjectKey) -> bool:
        return key in self.hashes

    def record(self, key: ObjectKey, comparable_text: str) -> None:
        value = digest(comparable_text)
        if self.hashes.get(key) != value:
            self.hashes[key] = value
            self.dirty = True

    def forget(self, key: ObjectKey) -> None:
        if self.hashes.pop(key, None) is not None:
            self.dirty = True

    def origin(self, key: ObjectKey, db_text: str | None, file_text: str | None) -> str:
        """Which side moved since the last sync, for an object that now differs.
        Texts are the comparable forms (see compare.comparable)."""
        last = self.hashes.get(key)
        if db_text is None:  # in the repo only
            # Never seen matching here: a teammate's new object, not applied yet.
            # Seen before: it was dropped from this database, unless the file moved
            # on since too; then a drop and an edit meet, and neither side wins.
            if last is None:
                return REPO
            return DATABASE if digest(file_text or "") == last else BOTH
        if file_text is None:  # in the database only
            # Seen before: the repo removed it (unless the database changed it since:
            # both moved). Never seen: new in the database.
            if last is None:
                return DATABASE
            return REPO if digest(db_text) == last else BOTH
        if last is None:
            return UNKNOWN
        file_same, db_same = digest(file_text) == last, digest(db_text) == last
        if file_same and not db_same:
            return DATABASE
        if db_same and not file_same:
            return REPO
        return BOTH


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name) or "target"
