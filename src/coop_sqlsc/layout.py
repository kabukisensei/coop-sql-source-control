"""Where each object lives in the repo folder, and reading the folder back.

Two layouts, one per repo (`layout:` in sqlsc.yml, or detected from the files):

`type` (the default; the shape Redgate SQL Source Control uses):

    <folder>/Security/Schemas/sales.sql
    <folder>/Tables/dbo.Customer.sql
    <folder>/Views/dbo.vCustomer.sql
    <folder>/Stored Procedures/dbo.usp_Load.sql
    <folder>/Functions/dbo.fn_Age.sql
    <folder>/Triggers/dbo.trCustomer_Audit.sql

`schema` (the shape SSMS's Create Project from Database, SqlPackage extract with
`ExtractTarget=SchemaObjectType` and Visual Studio's database projects write; checked
against SSMS 22 / DacFx 170 on 2026-10-27):

    <folder>/Security/sales.sql
    <folder>/dbo/Tables/Customer.sql          (its DML triggers follow it, after GO)
    <folder>/dbo/Views/vCustomer.sql
    <folder>/dbo/StoredProcedures/usp_Load.sql
    <folder>/dbo/Functions/fn_Age.sql

A DML trigger has no file of its own there: it is a batch at the end of its table's
file, the way DacFx writes it, and sqlsc reads and writes it there (a `Triggers/`
folder, which 1.0.9 wrote, is still read; a trigger written again moves into the
table's file). `DatabaseTriggers/` and `UserDefinedTypes/` hold objects sqlsc does not
script and are left alone. Static data (`Data/`) and migration scripts
(`Migrations/`) are sqlsc's own and keep one place in both. Characters Windows does not allow in a file name, and in the
`type` layout a "." inside a schema name, are written as %XX so every name maps to
exactly one file and back. The active layout is process-wide (one repo per run):
cli sets it from the config with use_layout().
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from . import sqltext
from .objects import OBJECT_TYPES, TYPE_LABELS, ObjectKey, batches, normalize

TYPE_LAYOUT, SCHEMA_LAYOUT = "type", "schema"
LAYOUTS = (TYPE_LAYOUT, SCHEMA_LAYOUT)

# `schema` layout: the folder under a schema's folder that holds each object type. On
# reading, the aliases are accepted too (DacFx has written both spellings over the years).
_SCHEMA_TYPE_FOLDERS: dict[str, str] = {
    "table": "Tables",
    "view": "Views",
    "function": "Functions",
    "procedure": "StoredProcedures",
    "trigger": "Triggers",  # read only: a trigger is written into its table's file (see trigger_home)
}
_SCHEMA_TYPE_ALIASES: dict[str, str] = {
    **{v.lower(): k for k, v in _SCHEMA_TYPE_FOLDERS.items()},
    "stored procedures": "procedure",  # Visual Studio's import and sqlsc 1.0.9 spelt it with a space
    "procedures": "procedure",
}
_SCHEMAS_FOLDER = "Security"  # `schema` layout: Security/<schema>.sql (one level up from `type`'s)
# Folders at the top of the scripts folder that are sqlsc's own in the `type` layout,
# and so cannot be a schema's folder in the `schema` layout.
_OWN_FOLDERS = frozenset(f.split("/")[0].lower() for f in OBJECT_TYPES.values()) | {"migrations"}
# Folders that belong to neither layout's object files and are never a schema's folder.
_NEVER_SCHEMA = _OWN_FOLDERS | {"bin", "obj", "security"}

_active = TYPE_LAYOUT
# `schema` layout: the table each DML trigger belongs to, learnt from every trigger
# text read (file or database), so relative_path can place a trigger in its table's
# file. {trigger key: table key}.
_trigger_homes: dict[ObjectKey, ObjectKey] = {}


def trigger_home(key: ObjectKey, text: str) -> ObjectKey | None:
    """Remembers (and returns) the table a DML trigger's text names with ON; None for a
    text that names none (then the trigger keeps a file of its own under Triggers/)."""
    target = sqltext.trigger_target(text)
    if target is None:
        return None
    schema, table = target
    home = ObjectKey("table", schema or key.schema, table)
    _trigger_homes[key] = home
    return home


def remember_homes(objects: dict[ObjectKey, str]) -> None:
    """Learns every trigger's table from a set of object texts (the database's, say)."""
    for key, text in objects.items():
        if key.type == "trigger":
            trigger_home(key, text)


def use_layout(name: str) -> None:
    """Makes `name` the layout every path function uses from now on (cli calls it once,
    after reading the config). Unknown names are a ConfigError-shaped ValueError."""
    global _active
    if name not in LAYOUTS:
        raise ValueError(f"layout must be one of {', '.join(LAYOUTS)}, not '{name}'.")
    _active = name
    _trigger_homes.clear()


def active_layout() -> str:
    return _active


def detect_layout(paths: Iterable[str]) -> str | None:
    """Which layout these paths (relative to the scripts folder) are in, when they say:
    `type` when any file is in sqlsc's type folders, else `schema` when any file is in
    a schema's type folder or Security/<schema>.sql, else None (an empty folder)."""
    listed = list(paths)
    if _layout_files(listed, TYPE_LAYOUT):
        return TYPE_LAYOUT
    if _layout_files(listed, SCHEMA_LAYOUT):
        return SCHEMA_LAYOUT
    return None


def _layout_files(paths: Iterable[str], layout: str) -> list[str]:
    """The object files among `paths` that belong to `layout` (static data and
    migrations, shared by both, are left out)."""
    found: list[str] = []
    for raw in paths:
        rel = raw.replace("\\", "/")
        if not rel.lower().endswith(".sql"):
            continue
        parts = rel.split("/")
        if layout == TYPE_LAYOUT:
            if len(parts) == 2 and parts[0].lower() in _OWN_FOLDERS - {"data", "migrations"}:
                found.append(rel)
            elif len(parts) == 3 and (parts[0].lower(), parts[1].lower()) == ("security", "schemas"):
                found.append(rel)
        elif len(parts) == 3 and parts[0].lower() not in _NEVER_SCHEMA and parts[1].lower() in _SCHEMA_TYPE_ALIASES:
            found.append(rel)
        elif len(parts) == 2 and parts[0].lower() == "security":
            found.append(rel)
    return sorted(found)


_UNSAFE = set('<>:"/\\|?*%') | {chr(c) for c in range(32)}


def _encode(part: str, *, dot: bool) -> str:
    out = []
    for ch in part:
        if ch in _UNSAFE or (dot and ch == "."):
            out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
        else:
            out.append(ch)
    encoded = "".join(out)
    # Windows also refuses names that end in a space or a dot.
    if encoded.endswith((" ", ".")):
        encoded = encoded[:-1] + f"%{ord(encoded[-1]):02X}"
    return encoded


def _decode(part: str) -> str:
    raw = bytearray()
    i = 0
    while i < len(part):
        if part[i] == "%" and i + 2 < len(part) and _is_hex(part[i + 1 : i + 3]):
            raw.append(int(part[i + 1 : i + 3], 16))
            i += 3
        else:
            raw.extend(part[i].encode("utf-8"))
            i += 1
    return raw.decode("utf-8")


def _is_hex(text: str) -> bool:
    return len(text) == 2 and all(c in "0123456789abcdefABCDEF" for c in text)


# The longest file name component NTFS and ext4 accept. SQL Server allows 128
# characters for a schema and 128 for a name, so `schema.name.sql` can exceed it.
MAX_FILE_NAME = 255


class FileNameTooLong(Exception):
    """The object's file name is longer than the file system allows."""

    def __init__(self, key: ObjectKey, relative: str) -> None:
        self.key, self.relative = key, relative
        super().__init__(
            f"{TYPE_LABELS.get(key.type, key.type)} {key.display} needs a file name of "
            f"{_name_length(relative)} characters; the file system allows {MAX_FILE_NAME}."
        )


def _name_length(relative: str) -> int:
    name = PurePosixPath(relative).name
    return max(len(name.encode("utf-8")), len(name.encode("utf-16-le")) // 2)


def file_name_problem(key: ObjectKey) -> FileNameTooLong | None:
    """Why the object cannot have a file, or None when its name fits."""
    relative = relative_path(key)
    return FileNameTooLong(key, relative) if _name_length(relative) > MAX_FILE_NAME else None


def relative_path(key: ObjectKey) -> str:
    """The object's file in the active layout, relative to the scripts folder, with forward slashes."""
    if _active == SCHEMA_LAYOUT and key.type == "trigger" and key in _trigger_homes:
        return relative_path(_trigger_homes[key])  # a batch in its table's file
    if _active == SCHEMA_LAYOUT and key.type in _SCHEMA_TYPE_FOLDERS:
        return f"{_encode(key.schema, dot=False)}/{_SCHEMA_TYPE_FOLDERS[key.type]}/{_encode(key.name, dot=False)}.sql"
    if _active == SCHEMA_LAYOUT and key.type == "schema":
        return f"{_SCHEMAS_FOLDER}/{_encode(key.name, dot=False)}.sql"
    folder = OBJECT_TYPES[key.type]
    if key.type == "schema":
        return f"{folder}/{_encode(key.name, dot=False)}.sql"
    return f"{folder}/{_encode(key.schema, dot=True)}.{_encode(key.name, dot=False)}.sql"


def key_for_path(relative: str) -> ObjectKey | None:
    """The object a file holds in the active layout, or None when the file is not one of ours."""
    rel = relative.replace("\\", "/")
    if not rel.lower().endswith(".sql"):
        return None
    if _active == SCHEMA_LAYOUT:
        return _schema_layout_key(rel)
    for obj_type, folder in OBJECT_TYPES.items():
        prefix = folder + "/"
        if not rel.lower().startswith(prefix.lower()):
            continue
        stem = rel[len(prefix) : -4]
        if "/" in stem or not stem:
            return None
        if obj_type == "schema":
            return ObjectKey("schema", "", _decode(stem))
        schema, dot, name = stem.partition(".")
        if not dot or not schema or not name:
            return None
        return ObjectKey(obj_type, _decode(schema), _decode(name))
    return None


def other_layout(paths: Iterable[str]) -> list[str]:
    """The object files, among these paths relative to the scripts folder, that are in
    the layout that is NOT active: `dbo/Tables/Customer.sql` in a `type` repo, or
    `Tables/dbo.Customer.sql` in a `schema` repo. The active layout never reads them,
    so every such object would look missing from the repo: script would write a
    second copy, and a release could drop the lot. Callers stop on a non-empty answer.
    Sorted; anything else (README, the SQL project, migrations) is left out."""
    return _layout_files(paths, SCHEMA_LAYOUT if _active == TYPE_LAYOUT else TYPE_LAYOUT)


def sql_files(root: Path, *, depth: int = 3) -> list[str]:
    """Every .sql file under root up to `depth` folders deep, relative, forward slashes,
    sorted: the listing other_layout reads for the working folder."""
    if not root.is_dir():
        return []
    found: list[str] = []

    def walk(folder: Path, level: int) -> None:
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix.lower() == ".sql":
                found.append(path.relative_to(root).as_posix())
            elif path.is_dir() and level < depth and not path.name.startswith("."):
                walk(path, level + 1)

    walk(root, 1)
    return found


def _schema_layout_key(rel: str) -> ObjectKey | None:
    parts = rel.split("/")
    stem = parts[-1][:-4]
    if not stem:
        return None
    if len(parts) == 2:
        if parts[0].lower() == _SCHEMAS_FOLDER.lower():
            return ObjectKey("schema", "", _decode(stem))
        if parts[0].lower() == "data":  # Data/<schema>.<name>.sql, the same in both layouts
            schema, dot, name = stem.partition(".")
            return ObjectKey("data", _decode(schema), _decode(name)) if dot and schema and name else None
        return None
    if len(parts) == 3 and parts[0].lower() not in _NEVER_SCHEMA:
        obj_type = _SCHEMA_TYPE_ALIASES.get(parts[1].lower())
        return ObjectKey(obj_type, _decode(parts[0]), _decode(stem)) if obj_type else None
    return None


def _object_folders(rel: str) -> bool:
    """Whether a file's folder is one that holds object files in the active layout."""
    folder = rel.rpartition("/")[0]
    if _active == SCHEMA_LAYOUT:
        parts = folder.split("/")
        if len(parts) == 1:
            return parts[0].lower() in (_SCHEMAS_FOLDER.lower(), "data")
        return len(parts) == 2 and parts[0].lower() not in _NEVER_SCHEMA and parts[1].lower() in _SCHEMA_TYPE_ALIASES
    return folder.lower() in {f.lower() for f in OBJECT_TYPES.values()}


def read_folder(root: Path) -> tuple[dict[ObjectKey, str], list[str]]:
    """Every object file under root in the active layout, normalized, plus files we
    could not place.

    Files outside the known folders (a README, deploy scripts, the SQL project) are
    ignored, not reported: the folder may hold more than objects.
    """
    files: dict[str, str] = {}
    for rel in sql_files(root):
        if _object_folders(rel):
            files[rel] = (root / rel).read_text(encoding="utf-8-sig")
    return objects_from_files(files)


def objects_from_files(files: dict[str, str]) -> tuple[dict[ObjectKey, str], list[str]]:
    """Object scripts from {path relative to the scripts folder: text}, from disk or from a git commit."""
    objects: dict[ObjectKey, str] = {}
    unknown: list[str] = []
    for rel in sorted(files):
        if not _object_folders(rel) or rel.endswith("/"):
            continue  # not in an object folder (or in a sub-folder of one): not ours
        key = key_for_path(rel)
        if key is None:
            unknown.append(rel)
            continue
        text = normalize(files[rel].lstrip("\ufeff"))
        if _active == SCHEMA_LAYOUT and key.type == "table":
            table_text, triggers = split_table_file(key, text)
            if table_text:
                objects[key] = table_text
            objects.update(triggers)
        elif _active == SCHEMA_LAYOUT and key.type == "trigger":
            trigger_home(key, text)  # a Triggers/ file from 1.0.9: read, and moved on the next write
            objects[key] = text
        else:
            objects[key] = text
    return objects, unknown


_SET_ONLY = ("SET QUOTED_IDENTIFIER", "SET ANSI_NULLS")


def split_table_file(table: ObjectKey, text: str) -> tuple[str, dict[ObjectKey, str]]:
    """A `schema`-layout table file into the table's own script and the DML triggers
    DacFx (and sqlsc) append to it, each a module script of its own. The SET batches a
    scripted module starts with travel with the trigger that follows them. The table
    text is "" when the file holds only triggers."""
    own: list[str] = []
    triggers: dict[ObjectKey, str] = {}
    pending: list[str] = []
    for batch in batches(text):
        found = sqltext.declaration(batch)
        if found is not None and sqltext.trigger_target(batch) is not None:
            key = ObjectKey("trigger", found.schema or table.schema, found.name)
            triggers[key] = normalize("\n".join([*(f"{b}\nGO" for b in pending), batch, "GO"]))
            _trigger_homes[key] = table
            pending = []
        elif batch.upper().startswith(_SET_ONLY):
            pending.append(batch)
        else:
            own += pending
            pending = []
            own.append(batch)
    own += pending
    return (normalize("\n".join(f"{b}\nGO" for b in own)) if own else ""), triggers


def join_table_file(table_text: str, triggers: dict[ObjectKey, str]) -> str:
    """The table's script with its triggers after it, in name order: the file DacFx shape."""
    parts = [table_text.rstrip("\n")] if table_text else []
    parts += [triggers[k].rstrip("\n") for k in sorted(triggers, key=lambda k: k.sort_key)]
    return normalize("\n".join(parts))


def write_object(root: Path, key: ObjectKey, script: str) -> Path:
    problem = file_name_problem(key)
    if problem:
        raise problem
    if _active == SCHEMA_LAYOUT and key.type == "trigger" and trigger_home(key, script) is not None:
        own_file = root / f"{_encode(key.schema, dot=False)}/Triggers/{_encode(key.name, dot=False)}.sql"
        if own_file.is_file():
            own_file.unlink()  # written by 1.0.9; the trigger lives in its table's file now
        return _write_into_table_file(root, _trigger_homes[key], {key: normalize(script)})
    if _active == SCHEMA_LAYOUT and key.type == "table":
        return _write_into_table_file(root, key, {}, table_text=normalize(script))
    return _write_file(root / relative_path(key), script)


def _write_into_table_file(
    root: Path, table: ObjectKey, triggers: dict[ObjectKey, str], *, table_text: str | None = None
) -> Path:
    """Rewrites a `schema`-layout table file keeping what it does not change: the table's
    own script (unless `table_text` replaces it) and the other triggers in it."""
    path = root / relative_path(table)
    current_text, current = ("", {})
    if path.is_file():
        current_text, current = split_table_file(table, normalize(path.read_text(encoding="utf-8-sig")))
    current.update(triggers)
    return _write_file(path, join_table_file(current_text if table_text is None else table_text, current))


def _write_file(path: Path, script: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Always LF and UTF-8; .gitattributes decides checkout endings. A new file gets no
    # BOM, and a file that has one (SSMS and Visual Studio save .sql files with one)
    # keeps it, so rewriting it never shows up in git as a one-byte change.
    bom = path.is_file() and _has_bom(path)
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="\n") as handle:
        handle.write(script)
    return path


def _has_bom(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(3) == b"\xef\xbb\xbf"


def delete_object(root: Path, key: ObjectKey) -> Path:
    path = root / relative_path(key)
    if _active == SCHEMA_LAYOUT and key.type == "trigger" and key in _trigger_homes and path.is_file():
        # Take the trigger's batch out of its table's file; the file goes when nothing is left.
        table = _trigger_homes[key]
        table_text, triggers = split_table_file(table, normalize(path.read_text(encoding="utf-8-sig")))
        triggers.pop(key, None)
        if table_text or triggers:
            _write_file(path, join_table_file(table_text, triggers))
        else:
            path.unlink()
        return path
    if path.exists():
        path.unlink()
    return path
