"""Where each object lives in the repo folder, and reading the folder back.

Layout (the same shape Redgate SQL Source Control uses):

    <folder>/Security/Schemas/sales.sql
    <folder>/Tables/dbo.Customer.sql
    <folder>/Views/dbo.vCustomer.sql
    <folder>/Stored Procedures/dbo.usp_Load.sql
    <folder>/Functions/dbo.fn_Age.sql
    <folder>/Triggers/dbo.trCustomer_Audit.sql

Characters Windows does not allow in a file name, and a "." inside a schema
name, are written as %XX so every name maps to exactly one file and back.
"""

from __future__ import annotations

from pathlib import Path

from .objects import OBJECT_TYPES, ObjectKey, normalize

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


def relative_path(key: ObjectKey) -> str:
    """The object's file, relative to the scripts folder, with forward slashes."""
    folder = OBJECT_TYPES[key.type]
    if key.type == "schema":
        return f"{folder}/{_encode(key.name, dot=False)}.sql"
    return f"{folder}/{_encode(key.schema, dot=True)}.{_encode(key.name, dot=False)}.sql"


def key_for_path(relative: str) -> ObjectKey | None:
    """The object a file holds, or None when the file is not one of ours."""
    rel = relative.replace("\\", "/")
    if not rel.lower().endswith(".sql"):
        return None
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


def read_folder(root: Path) -> tuple[dict[ObjectKey, str], list[str]]:
    """Every object file under root, normalized, plus files we could not place.

    Files outside the known folders (a README, deploy scripts) are ignored, not
    reported: the folder may hold more than objects.
    """
    files: dict[str, str] = {}
    if root.is_dir():
        for folder in OBJECT_TYPES.values():
            base = root / folder
            if not base.is_dir():
                continue
            for path in sorted(base.iterdir()):
                if path.is_file():
                    files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8-sig")
    return objects_from_files(files)


def objects_from_files(files: dict[str, str]) -> tuple[dict[ObjectKey, str], list[str]]:
    """Object scripts from {path relative to the scripts folder: text}, from disk or from a git commit."""
    objects: dict[ObjectKey, str] = {}
    unknown: list[str] = []
    for rel in sorted(files):
        folder, _, rest = rel.rpartition("/")
        if folder.lower() not in {f.lower() for f in OBJECT_TYPES.values()} or not rest:
            continue  # not in an object folder (or in a sub-folder of one): not ours
        key = key_for_path(rel)
        if key is None:
            unknown.append(rel)
            continue
        objects[key] = normalize(files[rel].lstrip("\ufeff"))
    return objects, unknown


def write_object(root: Path, key: ObjectKey, script: str) -> Path:
    path = root / relative_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Always LF and UTF-8 without a BOM; .gitattributes decides checkout endings.
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(script)
    return path


def delete_object(root: Path, key: ObjectKey) -> Path:
    path = root / relative_path(key)
    if path.exists():
        path.unlink()
    return path
