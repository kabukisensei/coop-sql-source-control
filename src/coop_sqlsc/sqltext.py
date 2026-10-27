"""T-SQL text read token-aware: what is data (string literals, quoted names) and what
is formatting (everything else), and the name a module's CREATE declares.

Only the lexical shape is read: comments (line and nested block), string literals
('...' with '' escapes, N'...' included) and quoted names ([...] with ]] escapes,
"..." with "" escapes). Nothing is parsed beyond that, nothing is executed.
"""

from __future__ import annotations

import re

_TRAILING = re.compile(r"[^\S\n]+(?=\n)")
_LEADING_BLANK = re.compile(r"\A(?:[^\S\n]*\n)+")


def runs(text: str) -> list[tuple[str, str]]:
    """The text as (kind, text) runs: `string`, `ident`, `comment` or `code`.
    Joined back, the runs are the text. An unterminated literal, quoted name or
    block comment runs to the end of the text."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(text)
    code_from = 0

    def flush(upto: int) -> None:
        if upto > code_from:
            out.append(("code", text[code_from:upto]))

    while i < n:
        ch = text[i]
        if text.startswith("--", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            kind = "comment"
        elif text.startswith("/*", i):
            end, depth = i + 2, 1
            while end < n and depth:
                if text.startswith("/*", end):
                    depth, end = depth + 1, end + 2
                elif text.startswith("*/", end):
                    depth, end = depth - 1, end + 2
                else:
                    end += 1
            kind = "comment"
        elif ch in "'[\"":
            close = "]" if ch == "[" else ch
            end = i + 1
            while end < n:
                if text[end] == close:
                    if end + 1 < n and text[end + 1] == close:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            kind = "string" if ch == "'" else "ident"
        else:
            i += 1
            continue
        flush(i)
        out.append((kind, text[i:end]))
        i = code_from = end
    flush(n)
    return out


def _protected(kind: str) -> bool:
    """String literals and quoted names are data: their bytes never change."""
    return kind in ("string", "ident")


def normalize(text: str) -> str:
    """Line endings to LF, no trailing spaces, no leading or trailing blank lines,
    outside string literals and quoted names only. Inside them every character,
    a trailing space before a line break or a CR included, is kept as it is: it is
    the value, not formatting."""
    parts: list[tuple[bool, str]] = []
    for kind, piece in runs(text):
        keep = _protected(kind)
        if parts and not keep and not parts[-1][0]:
            parts[-1] = (False, parts[-1][1] + piece)  # a comment and the code after it are one free run
        else:
            parts.append((keep, piece))
    out: list[str] = []
    for index, (keep, piece) in enumerate(parts):
        if not keep:
            piece = piece.replace("\r\n", "\n").replace("\r", "\n")
            piece = _TRAILING.sub("", piece)
            if index == 0:
                piece = _LEADING_BLANK.sub("", piece)
            if index == len(parts) - 1:
                piece = piece.rstrip()
        out.append(piece)
    body = "".join(out)
    if not body or (parts[-1][0] and body.endswith("\n")):  # an unterminated literal runs to the end
        return body
    return body + "\n"


def same_line_endings(text: str) -> str:
    """CR LF and CR as LF everywhere, inside literals too: for comparing only. Git's
    `text` attribute stores a literal's line breaks as LF and checks them out in the
    machine's style, so a line-break style inside a literal cannot survive a clone
    and is not a difference two sides can settle. Every other character counts."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


_MODULE_WORDS = {"VIEW", "PROC", "PROCEDURE", "FUNCTION", "TRIGGER"}
_PART = r"(?:\[(?:[^\]]|\]\])*\]|\"(?:[^\"]|\"\")*\"|[^\s.;(\[\"'\-/]+)"
_NAME = re.compile(rf"\s*({_PART})(?:\s*\.\s*({_PART}))?(?:\s*\.\s*({_PART}))?")
_WORD = re.compile(r"[A-Za-z_]+")
_NEXT_WORD = re.compile(r"\s*([A-Za-z_]+)")
# What may stand before the CREATE: the SET batches a scripted module starts with.
_PREAMBLE_WORDS = {"SET", "QUOTED_IDENTIFIER", "ANSI_NULLS", "ON", "OFF", "GO"}


def _unquote(part: str) -> str:
    if part.startswith("[") and part.endswith("]"):
        return part[1:-1].replace("]]", "]")
    if part.startswith('"') and part.endswith('"'):
        return part[1:-1].replace('""', '"')
    return part


def _blank(text: str) -> str:
    """Comments blanked (positions kept), so a name can be read past them."""
    return "".join(
        "".join(" " if c != "\n" else c for c in piece) if kind == "comment" else piece for kind, piece in runs(text)
    )


class Declaration:
    """The name a module's leading CREATE [OR ALTER] VIEW/PROCEDURE/FUNCTION/TRIGGER
    declares, with where its schema and name parts sit in the text."""

    def __init__(self, schema: tuple[int, int] | None, name: tuple[int, int], text: str) -> None:
        self.schema_span = schema
        self.name_span = name
        self.schema = _unquote(text[schema[0] : schema[1]]) if schema else None
        self.name = _unquote(text[name[0] : name[1]])


def declaration(text: str) -> Declaration | None:
    """The leading module declaration, or None when the text does not start (after
    comments and the SET batches of a scripted module) with one this reader can place."""
    plain = _blank(text)
    for match in _WORD.finditer(plain):
        word = match.group().upper()
        if word == "CREATE":
            break
        if word not in _PREAMBLE_WORDS:
            return None
    else:
        return None
    pos = match.end()
    words: list[str] = []
    for _ in range(3):
        word = _NEXT_WORD.match(plain, pos)
        if not word:
            break
        words.append(word.group(1).upper())
        pos = word.end()
        if words[-1] in _MODULE_WORDS:
            break
    if not words or words[-1] not in _MODULE_WORDS or words[:-1] not in ([], ["OR", "ALTER"]):
        return None
    name = _NAME.match(plain, pos)
    if not name:
        return None
    spans = [name.span(g) for g in (1, 2, 3) if name.group(g) is not None]
    if len(spans) > 2:
        return None  # a three-part name is not a module declaration this reader rewrites
    if len(spans) == 1:
        return Declaration(None, spans[0], text)
    return Declaration(spans[0], spans[1], text)


def quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


_ON = re.compile(r"\s+ON\s+", re.IGNORECASE)


def trigger_target(text: str) -> tuple[str | None, str] | None:
    """(schema, table) a DML trigger's `CREATE TRIGGER name ON table` names, read past
    comments; None when the text is not a trigger declaration or names no table (a
    database trigger's `ON DATABASE` / `ON ALL SERVER` gives None too)."""
    found = declaration(text)
    if found is None:
        return None
    plain = _blank(text)
    head = plain[: found.name_span[0]].upper()
    if "TRIGGER" not in head.split():
        return None
    on = _ON.match(plain, found.name_span[1])
    if not on:
        return None
    name = _NAME.match(plain, on.end())
    if not name:
        return None
    parts = [_unquote(name.group(g)) for g in (1, 2, 3) if name.group(g) is not None]
    if len(parts) > 2 or parts[-1].upper() in ("DATABASE", "ALL"):
        return None
    return (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])


def declared_as(text: str, schema: str, name: str) -> tuple[str, str | None]:
    """The text declaring `schema`.`name` (only the parts that differ are rewritten,
    as bracketed names; the rest of the text is untouched), and the name it declared
    before when that differed, else None. A schema compares case-insensitively (the
    catalog spells it as the schema is spelled, not as the CREATE did); the name
    compares exactly, since sp_rename can change only its case."""
    found = declaration(text)
    if found is None:
        return text, None
    edits: list[tuple[tuple[int, int], str]] = []
    if found.name != name:
        edits.append((found.name_span, quote(name)))
    if found.schema_span is not None and found.schema is not None and found.schema.lower() != schema.lower():
        edits.append((found.schema_span, quote(schema)))
    if not edits:
        return text, None
    before = (quote(found.schema) + "." if found.schema is not None else "") + quote(found.name)
    for (start, end), new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text, before
