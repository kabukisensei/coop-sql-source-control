"""Object checkout and check-in: a small shared lock table.

The table lives in a database the whole team can reach, `sqlsc.ObjectLock`:
by default the database being worked on (the shared-dev-database setup), or
the target `locks.target` names in sqlsc.yml (one small Azure SQL database
for teams where everyone has their own dev database, or for a Lakehouse,
which cannot hold the table).

Rows are scoped by repo, so one lock database can serve many repos. A lock
records who holds it (their git e-mail, or `--as`) and the database login
that took it, which the optional edit guard compares against.

Types stay within what Fabric Warehouse accepts (varchar, datetime2) so the
same table works on every kind of target.
"""

from __future__ import annotations

import getpass
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Target
from .objects import ObjectKey

SCHEMA = "sqlsc"
TABLE = "sqlsc.ObjectLock"

CREATE_SCHEMA = "IF SCHEMA_ID('sqlsc') IS NULL EXEC ('CREATE SCHEMA sqlsc')"
CREATE_TABLE = """
IF OBJECT_ID('sqlsc.ObjectLock') IS NULL
CREATE TABLE sqlsc.ObjectLock
(
      Repo varchar(400) NOT NULL
     ,ObjectType varchar(20) NOT NULL
     ,SchemaName varchar(128) NOT NULL
     ,ObjectName varchar(128) NOT NULL
     ,LockedBy varchar(256) NOT NULL
     ,LoginName varchar(256) NOT NULL
     ,LockedAt datetime2(0) NOT NULL
     ,Note varchar(400) NULL
)
"""
TABLE_EXISTS = "SELECT CASE WHEN OBJECT_ID('sqlsc.ObjectLock') IS NULL THEN 0 ELSE 1 END"
LIST = (
    "SELECT Repo, ObjectType, SchemaName, ObjectName, LockedBy, LoginName, LockedAt, Note "
    "FROM sqlsc.ObjectLock WHERE (? IS NULL OR Repo = ?) ORDER BY SchemaName, ObjectName"
)


class LockError(Exception):
    """The lock table cannot be reached or created; the message says what to do."""


@dataclass(frozen=True)
class Lock:
    repo: str
    key: ObjectKey
    locked_by: str
    login: str
    locked_at: datetime
    note: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "object": self.key.display,
            "type": self.key.type,
            "schema": self.key.schema,
            "name": self.key.name,
            "locked_by": self.locked_by,
            "locked_at": self.locked_at.replace(tzinfo=timezone.utc).isoformat(),
            "note": self.note,
            "repo": self.repo,
        }


class LockStore:
    def __init__(self, conn: Any, *, transactional: bool) -> None:
        self.conn = conn
        self.transactional = transactional

    # --- reading -----------------------------------------------------------------------

    def exists(self) -> bool:
        return bool(self.conn.cursor().execute(TABLE_EXISTS).fetchone()[0])

    def list(self, repo: str | None = None) -> list[Lock]:
        if not self.exists():
            return []
        rows = self.conn.cursor().execute(LIST, repo, repo).fetchall()
        return [
            Lock(r[0], ObjectKey(r[1], "" if r[1] == "schema" else r[2], r[3]), r[4], r[5], r[6], r[7]) for r in rows
        ]

    def login(self) -> str:
        return self.conn.cursor().execute("SELECT SUSER_SNAME()").fetchone()[0] or ""

    # --- writing -----------------------------------------------------------------------

    def ensure(self) -> bool:
        """Creates the schema and table when missing. Returns True when it created them."""
        if self.exists():
            return False
        cur = self.conn.cursor()
        try:
            cur.execute(CREATE_SCHEMA)
            cur.execute(CREATE_TABLE)
        except Exception as exc:  # pyodbc.Error
            raise LockError(
                f"Could not create the lock table sqlsc.ObjectLock: {exc}. Ask someone with CREATE TABLE "
                "rights to run `sqlsc checkout` once, or point `locks: target:` in sqlsc.yml at another database."
            ) from exc
        return True

    def acquire(
        self, keys: list[ObjectKey], *, repo: str, who: str, note: str | None
    ) -> tuple[list[ObjectKey], list[Lock]]:
        """Locks every key that is free or already ours. Returns (locked, held by someone else)."""
        login = self.login()
        now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        granted: list[ObjectKey] = []
        conflicts: list[Lock] = []
        self._begin()
        try:
            held = {lock.key: lock for lock in self._locked_for_update(repo)}
            cur = self.conn.cursor()
            for key in keys:
                current = held.get(key)
                if current is not None and current.locked_by.lower() != who.lower():
                    conflicts.append(current)
                    continue
                if current is None:
                    cur.execute(
                        "INSERT INTO sqlsc.ObjectLock (Repo, ObjectType, SchemaName, ObjectName, LockedBy, LoginName,"
                        " LockedAt, Note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        repo,
                        key.type,
                        key.schema,
                        key.name,
                        who,
                        login,
                        now,
                        note,
                    )
                granted.append(key)
            self._commit()
        except Exception:
            self._rollback()
            raise
        return granted, conflicts

    def release(
        self, keys: list[ObjectKey] | None, *, repo: str, who: str, force: bool = False
    ) -> tuple[list[ObjectKey], list[Lock]]:
        """Releases our locks on keys (all of ours when keys is None). Others' locks need force."""
        released: list[ObjectKey] = []
        refused: list[Lock] = []
        if not self.exists():
            return released, refused
        self._begin()
        try:
            cur = self.conn.cursor()
            for lock in self._locked_for_update(repo):
                if keys is not None and lock.key not in keys:
                    continue
                mine = lock.locked_by.lower() == who.lower()
                if keys is None and not mine:
                    continue
                if not mine and not force:
                    refused.append(lock)
                    continue
                cur.execute(
                    "DELETE FROM sqlsc.ObjectLock WHERE Repo = ? AND ObjectType = ? AND SchemaName = ? "
                    "AND ObjectName = ?",
                    repo,
                    lock.key.type,
                    lock.key.schema,
                    lock.key.name,
                )
                released.append(lock.key)
            self._commit()
        except Exception:
            self._rollback()
            raise
        return released, refused

    def _locked_for_update(self, repo: str) -> list[Lock]:
        # On SQL Server the hints hold the rows (and the gaps) until commit, so two
        # people checking out the same object at once cannot both win.
        hint = " WITH (UPDLOCK, HOLDLOCK)" if self.transactional else ""
        rows = (
            self.conn.cursor()
            .execute(
                "SELECT Repo, ObjectType, SchemaName, ObjectName, LockedBy, LoginName, LockedAt, Note "
                f"FROM sqlsc.ObjectLock{hint} WHERE Repo = ?",
                repo,
            )
            .fetchall()
        )
        return [
            Lock(r[0], ObjectKey(r[1], "" if r[1] == "schema" else r[2], r[3]), r[4], r[5], r[6], r[7]) for r in rows
        ]

    def _begin(self) -> None:
        self.conn.autocommit = False

    def _commit(self) -> None:
        self.conn.commit()
        self.conn.autocommit = True

    def _rollback(self) -> None:
        self.conn.rollback()
        self.conn.autocommit = True


# --- which database holds the locks, and what a name refers to -------------------------------

# Kinds that run the lock table's read-and-insert in one locked transaction, and
# that can carry the database-level edit guard trigger.
TRANSACTIONAL_KINDS = ("sql_server", "azure_sql", "fabric_sql_database")
GUARD_KINDS = TRANSACTIONAL_KINDS


def check_lock_target(target: Target, *, configured: bool) -> None:
    if target.caps["tables"]:
        return
    where = f"The locks target '{target.name}'" if configured else f"'{target.name}' is a {target.label} and"
    raise LockError(
        f"{where} cannot hold the lock table. Add `locks: target: <name>` to sqlsc.yml naming an Azure SQL, "
        "SQL Server or Fabric Warehouse target the whole team can reach."
    )


_LOOKUP_TYPES = {
    "U": "table",
    "V": "view",
    "P": "procedure",
    "FN": "function",
    "IF": "function",
    "TF": "function",
    "TR": "trigger",
}
LOOKUP_OBJECT = (
    "SELECT RTRIM(o.type), SCHEMA_NAME(o.schema_id), o.name FROM sys.objects AS o WHERE o.object_id = OBJECT_ID(?)"
)
LOOKUP_SCHEMA = "SELECT name FROM sys.schemas WHERE name = ?"
LOOKUP_ANY_SCHEMA = (
    "SELECT RTRIM(o.type), SCHEMA_NAME(o.schema_id), o.name FROM sys.objects AS o "
    "WHERE o.name = ? AND o.is_ms_shipped = 0 AND o.type IN ('U', 'V', 'P', 'FN', 'IF', 'TF', 'TR')"
)


def lookup(conn: Any, name: str) -> ObjectKey | None:
    """What a user's name (`dbo.Foo`, `[dbo].[Foo]`, `Foo`, `sales`) means in a live database."""
    row = conn.cursor().execute(LOOKUP_OBJECT, name).fetchone()
    if row is not None and row[0] in _LOOKUP_TYPES:
        return ObjectKey(_LOOKUP_TYPES[row[0]], row[1], row[2])
    bare = name.replace("[", "").replace("]", "").strip()
    if "." not in bare:
        # A bare name means that object in any schema, as everywhere else in sqlsc, when only one has it.
        found = conn.cursor().execute(LOOKUP_ANY_SCHEMA, bare).fetchall()
        if len(found) == 1 and found[0][0] in _LOOKUP_TYPES:
            return ObjectKey(_LOOKUP_TYPES[found[0][0]], found[0][1], found[0][2])
        row = conn.cursor().execute(LOOKUP_SCHEMA, bare).fetchone()
        if row is not None:
            return ObjectKey("schema", "", row[0])
    return None


def key_from_name(object_type: str, name: str) -> ObjectKey:
    """A key for an object that does not exist yet (`checkout --type view sales.vNew`)."""
    bare = name.replace("[", "").replace("]", "").strip()
    if object_type == "schema":
        return ObjectKey("schema", "", bare)
    schema, _, rest = bare.rpartition(".")
    return ObjectKey(object_type, schema or "dbo", rest)


# --- who and which repo -------------------------------------------------------------------


def current_user(root: Path, override: str | None = None) -> str:
    if override:
        return override.strip()
    for setting in ("user.email", "user.name"):
        value = _git_config(root, setting)
        if value:
            return value
    return getpass.getuser()


def repo_id(root: Path, configured: str | None = None) -> str:
    """The name locks are scoped by: sqlsc.yml's locks.repo, else the origin URL, else the folder."""
    if configured:
        return configured.strip()
    url = _git_config(root, "remote.origin.url")
    if url:
        return normalize_remote(url)
    return root.resolve().name


def normalize_remote(url: str) -> str:
    """git@github.com:org/repo.git and https://user@github.com/org/repo -> github.com/org/repo."""
    text = url.strip()
    text = re.sub(r"^[a-z+]+://", "", text)
    text = re.sub(r"^[^@/]+@", "", text)
    text = text.replace(":", "/", 1) if re.match(r"^[^/]+:[^/]", text) and "://" not in url else text
    text = re.sub(r"\.git/?$", "", text).rstrip("/")
    return text.lower()


def _git_config(root: Path, key: str) -> str:
    try:
        done = subprocess.run(["git", "config", "--get", key], cwd=root, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


# --- the edit guard -------------------------------------------------------------------------

GUARD_NAME = "sqlsc_guard"
GUARD_EVENTS = (
    "ALTER_TABLE, DROP_TABLE, ALTER_VIEW, DROP_VIEW, ALTER_PROCEDURE, DROP_PROCEDURE, "
    "ALTER_FUNCTION, DROP_FUNCTION, ALTER_TRIGGER, DROP_TRIGGER, CREATE_INDEX, ALTER_INDEX, DROP_INDEX"
)
GUARD_SQL = f"""CREATE OR ALTER TRIGGER {GUARD_NAME} ON DATABASE
FOR {GUARD_EVENTS}
AS
BEGIN
    SET NOCOUNT ON;
    IF OBJECT_ID('sqlsc.ObjectLock') IS NULL RETURN;
    DECLARE @event xml = EVENTDATA();
    DECLARE @type nvarchar(128) = @event.value('(/EVENT_INSTANCE/ObjectType)[1]', 'nvarchar(128)');
    DECLARE @schema nvarchar(128) = @event.value('(/EVENT_INSTANCE/SchemaName)[1]', 'nvarchar(128)');
    DECLARE @name nvarchar(128) = CASE WHEN @type = N'INDEX'
        THEN @event.value('(/EVENT_INSTANCE/TargetObjectName)[1]', 'nvarchar(128)')
        ELSE @event.value('(/EVENT_INSTANCE/ObjectName)[1]', 'nvarchar(128)') END;
    DECLARE @owner varchar(256), @since datetime2(0);
    SELECT TOP (1) @owner = LockedBy, @since = LockedAt
    FROM sqlsc.ObjectLock
    WHERE SchemaName = @schema AND ObjectName = @name AND LoginName <> ORIGINAL_LOGIN();
    IF @owner IS NOT NULL
    BEGIN
        DECLARE @message nvarchar(2048) = CONCAT(N'[', @schema, N'].[', @name, N'] is checked out by ', @owner,
            N' since ', CONVERT(nvarchar(20), @since, 120), N' UTC. Ask them to check it in first (sqlsc locks).');
        ROLLBACK;
        THROW 50001, @message, 1;
    END
END"""
GUARD_EXISTS = f"SELECT COUNT(*) FROM sys.triggers WHERE parent_class = 0 AND name = '{GUARD_NAME}'"
GUARD_DROP = f"DROP TRIGGER IF EXISTS {GUARD_NAME} ON DATABASE"
