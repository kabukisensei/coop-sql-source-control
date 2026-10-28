"""Reading a table file whatever wrote it: SSMS 22 / DacFx, sqlsc, or a person.

DacFx (Create Project from Database, Schema Compare's Update to a project) writes a
table with its constraints inside the CREATE TABLE, upper-case types and aligned
columns; sqlsc before 1.0.11 wrote its own shape, constraints as ALTER TABLE after it.
Both describe the same table. `sqlsc_shape` reads either into the catalog's model
(scripter.Table) and writes it back in the sqlsc shape, the one form compare and
get-latest reason on: two files that describe the same table read the same, however
they are laid out, and get-latest works out its ALTER statements from either.

Only what sqlsc scripts from the catalog is read: columns (type, collation, identity,
default, computed), keys, checks, foreign keys, indexes (with INCLUDE and a filter),
and disabled or untrusted constraints. A table text with anything else (index options,
a filegroup, ROWGUIDCOL, SPARSE, ...) is left as it is: it then shows as changed and
get-latest holds it back, rather than an option being dropped without a word.
DacFx's extended properties (sp_addextendedproperty) are not tracked by sqlsc: they
are read past here, and kept in the file when sqlsc rewrites it (layout.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .objects import batches
from .scripter import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    KeyConstraint,
    Table,
    script_table_sqlsc,
)


class Unreadable(ValueError):
    """The table text holds something this reader does not model."""


@dataclass
class Token:
    kind: str  # word, ident, string, number, punct
    text: str
    start: int
    end: int

    @property
    def upper(self) -> str:
        return self.text.upper() if self.kind == "word" else ""

    @property
    def name(self) -> str:
        """The name a word or quoted name stands for."""
        if self.kind == "ident":
            if self.text.startswith("["):
                return self.text[1:-1].replace("]]", "]")
            return self.text[1:-1].replace('""', '"')
        return self.text


_WORD = re.compile(r"[A-Za-z_@#][A-Za-z0-9_@#$]*")
_NUMBER = re.compile(r"\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+")


def tokenize(text: str) -> list[Token]:
    out: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j, depth = i + 2, 1
            while j < n and depth:
                if text.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            i = j
        elif ch in '["' or (ch in "Nn" and text.startswith("'", i + 1)) or ch == "'":
            start = i
            if ch in "Nn":
                i += 1
            close = {"[": "]", '"': '"', "'": "'"}[text[i]]
            kind = "string" if close == "'" else "ident"
            j = i + 1
            while j < n:
                if text[j] == close:
                    if j + 1 < n and text[j + 1] == close:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            else:
                raise Unreadable("an unterminated literal or name")
            out.append(Token(kind, text[start:j], start, j))
            i = j
        elif m := _WORD.match(text, i):
            out.append(Token("word", m.group(), i, m.end()))
            i = m.end()
        elif m := _NUMBER.match(text, i):
            out.append(Token("number", m.group(), i, m.end()))
            i = m.end()
        else:
            out.append(Token("punct", ch, i, i + 1))
            i += 1
    return out


class _Reader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.pos = 0

    def peek(self, ahead: int = 0) -> Token | None:
        i = self.pos + ahead
        return self.tokens[i] if i < len(self.tokens) else None

    def done(self) -> bool:
        return self.pos >= len(self.tokens)

    def take(self) -> Token:
        tok = self.peek()
        if tok is None:
            raise Unreadable("the statement ends too early")
        self.pos += 1
        return tok

    def is_word(self, *words: str, ahead: int = 0) -> bool:
        tok = self.peek(ahead)
        return tok is not None and tok.upper in words

    def is_punct(self, ch: str, ahead: int = 0) -> bool:
        tok = self.peek(ahead)
        return tok is not None and tok.kind == "punct" and tok.text == ch

    def accept(self, *words: str) -> bool:
        if self.is_word(*words):
            self.pos += 1
            return True
        return False

    def accept_punct(self, ch: str) -> bool:
        if self.is_punct(ch):
            self.pos += 1
            return True
        return False

    def expect(self, *words: str) -> str:
        if not self.is_word(*words):
            raise Unreadable(f"expected {' or '.join(words)} at: {self._here()}")
        return self.take().upper

    def expect_punct(self, ch: str) -> None:
        if not self.accept_punct(ch):
            raise Unreadable(f"expected {ch} at: {self._here()}")

    def _here(self) -> str:
        tok = self.peek()
        return "the end" if tok is None else self.text[tok.start : tok.start + 40]

    def name_part(self) -> str:
        tok = self.take()
        if tok.kind not in ("word", "ident"):
            raise Unreadable(f"expected a name at: {tok.text}")
        return tok.name

    def multipart(self) -> list[str]:
        parts = [self.name_part()]
        while self.accept_punct("."):
            parts.append(self.name_part())
        return parts

    def group(self) -> str:
        """A parenthesised expression as written, parentheses included."""
        first = self.peek()
        if first is None or not self.is_punct("("):
            raise Unreadable(f"expected ( at: {self._here()}")
        depth = 0
        while True:
            tok = self.take()
            if tok.kind == "punct" and tok.text == "(":
                depth += 1
            elif tok.kind == "punct" and tok.text == ")":
                depth -= 1
                if depth == 0:
                    return self.text[first.start : tok.end]

    def column_list(self, *, sort_order: bool) -> list[tuple[str, bool]]:
        self.expect_punct("(")
        cols: list[tuple[str, bool]] = []
        while True:
            col = self.name_part()
            desc = False
            if sort_order and self.is_word("ASC", "DESC"):
                desc = self.take().upper == "DESC"
            cols.append((col, desc))
            if self.accept_punct(")"):
                return cols
            self.expect_punct(",")


def _table_name(parts: list[str]) -> tuple[str, str]:
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return "dbo", parts[0]
    raise Unreadable("a three-part table name")


# --- the type of a column ---------------------------------------------------------------

_TYPE_ALIASES = {"rowversion": "timestamp", "integer": "int", "dec": "decimal", "character": "char"}
_SYSTEM_CLR = {"hierarchyid", "geometry", "geography"}


def _column_type(r: _Reader, col: Column) -> None:
    parts = r.multipart()
    if len(parts) == 2 and parts[0].lower() == "sys":
        parts = parts[1:]
    if len(parts) == 2:
        col.user_type, col.type_schema, col.type_name = True, parts[0], parts[1]
        return
    if len(parts) != 1:
        raise Unreadable("a type with three parts")
    t = _TYPE_ALIASES.get(parts[0].lower(), parts[0].lower())
    col.type_name = t
    args: list[str] = []
    if r.is_punct("("):
        r.take()
        while True:
            tok = r.take()
            if tok.kind == "number" or tok.upper == "MAX":
                args.append(tok.text.upper())
            else:
                raise Unreadable(f"cannot read the type of column {col.name}")
            if r.accept_punct(")"):
                break
            r.expect_punct(",")
    if t in ("varchar", "char", "varbinary", "binary", "nvarchar", "nchar"):
        size = args[0] if args else "1"
        if size == "MAX":
            col.max_length = -1
        else:
            col.max_length = int(size) * (2 if t.startswith("n") else 1)
    elif t in ("decimal", "numeric"):
        col.precision = int(args[0]) if args else 18
        col.scale = int(args[1]) if len(args) > 1 else 0
    elif t in ("datetime2", "time", "datetimeoffset"):
        col.scale = int(args[0]) if args else 7
    elif t == "float":
        bits = int(args[0]) if args else 53
        if bits <= 24:
            col.type_name = "real"
        col.precision = 24 if bits <= 24 else 53
    elif args and t not in _SYSTEM_CLR:
        raise Unreadable(f"the type of column {col.name} takes no arguments")


# --- CREATE TABLE ---------------------------------------------------------------------


def _index_kind(r: _Reader, default: str) -> str:
    if r.is_word("CLUSTERED", "NONCLUSTERED"):
        return r.take().upper
    return default


def _references(r: _Reader, name: str | None, columns: list[str], table: Table) -> ForeignKey:
    r.expect("REFERENCES")
    ref_schema, ref_table = _table_name(r.multipart())
    ref_columns = [c for c, _ in r.column_list(sort_order=False)] if r.is_punct("(") else []
    fk = ForeignKey(name, columns, ref_schema, ref_table, ref_columns)
    while r.is_word("ON") and r.is_word("DELETE", "UPDATE", ahead=1):
        r.take()
        which = r.take().upper
        if r.accept("NO"):
            r.expect("ACTION")
            action = "NO_ACTION"
        elif r.accept("SET"):
            action = "SET_" + r.expect("NULL", "DEFAULT")
        else:
            action = r.expect("CASCADE")
        if which == "DELETE":
            fk.on_delete = action
        else:
            fk.on_update = action
    if r.is_word("NOT") and r.is_word("FOR", ahead=1):
        raise Unreadable("NOT FOR REPLICATION is not scripted by sqlsc")
    if not ref_columns:
        raise Unreadable("a foreign key that names no referenced columns")
    if len(ref_columns) != len(columns):
        raise Unreadable("a foreign key whose column lists differ in length")
    return fk


def _constraint(r: _Reader, table: Table, column: str | None) -> object:
    """One constraint, at table level or (column given) on a column; added to table."""
    name = r.name_part() if r.accept("CONSTRAINT") else None
    if r.is_word("PRIMARY", "UNIQUE"):
        primary = r.take().upper == "PRIMARY"
        if primary:
            r.expect("KEY")
        kind = _index_kind(r, "CLUSTERED" if primary else "NONCLUSTERED")
        cols = r.column_list(sort_order=True) if column is None or r.is_punct("(") else [(column, False)]
        if r.is_word("WITH", "ON"):
            raise Unreadable("a key with index options or a filegroup")
        if r.is_word("NOT") and r.is_word("ENFORCED", ahead=1):
            raise Unreadable("a NOT ENFORCED key is read in the sqlsc shape only")
        key = KeyConstraint(name, primary, kind, cols)
        table.keys.append(key)
        return key
    if r.accept("CHECK"):
        if r.is_word("NOT"):
            raise Unreadable("NOT FOR REPLICATION is not scripted by sqlsc")
        check = CheckConstraint(name, r.group())
        table.checks.append(check)
        return check
    if r.accept("FOREIGN"):
        r.expect("KEY")
        cols = [c for c, _ in r.column_list(sort_order=False)]
        fk = _references(r, name, cols, table)
        table.foreign_keys.append(fk)
        return fk
    if column is not None and r.is_word("REFERENCES"):
        fk = _references(r, name, [column], table)
        table.foreign_keys.append(fk)
        return fk
    raise Unreadable(f"a constraint sqlsc does not read: {r._here()}")


def _column(r: _Reader, table: Table) -> str | None:
    """Reads one column into table; returns its name when it said neither NULL nor NOT NULL."""
    col = Column(name=r.name_part(), type_name="")
    if r.accept("AS"):
        col.computed = r.group()
        if r.accept("PERSISTED"):
            col.persisted = True
            if r.accept("NOT"):
                r.expect("NULL")
                col.nullable = False
        table.columns.append(col)
        while not (r.done() or r.is_punct(",") or r.is_punct(")")):
            _constraint(r, table, col.name)
        return None
    _column_type(r, col)
    nullable: bool | None = None
    while not (r.done() or r.is_punct(",") or r.is_punct(")")):
        if r.accept("COLLATE"):
            col.collation = r.name_part()
        elif r.accept("IDENTITY"):
            seed, step = "1", "1"
            if r.accept_punct("("):
                seed = _signed_number(r)
                r.expect_punct(",")
                step = _signed_number(r)
                r.expect_punct(")")
            col.identity = (seed, step)
        elif r.accept("NULL"):
            nullable = True
        elif r.is_word("NOT") and r.is_word("NULL", ahead=1):
            r.pos += 2
            nullable = False
        elif r.is_word("DEFAULT") or (r.is_word("CONSTRAINT") and r.is_word("DEFAULT", ahead=2)):
            if r.accept("CONSTRAINT"):
                col.default_name = r.name_part()
            r.expect("DEFAULT")
            col.default_definition = _default_value(r)
        elif r.is_word("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "REFERENCES"):
            _constraint(r, table, col.name)
        else:
            raise Unreadable(f"column {col.name}: {r._here()}")
    # No NULL / NOT NULL: nullable (ANSI_NULL_DFLT_ON), unless an identity or in the primary key.
    col.nullable = col.identity is None if nullable is None else nullable
    table.columns.append(col)
    return col.name if nullable is None else None


def _signed_number(r: _Reader) -> str:
    sign = ""
    if r.is_punct("-") or r.is_punct("+"):
        sign = r.take().text.replace("+", "")
    tok = r.take()
    if tok.kind != "number":
        raise Unreadable("an IDENTITY seed or increment that is not a number")
    return sign + tok.text


def _default_value(r: _Reader) -> str:
    if r.is_punct("("):
        return r.group()
    first = r.take()
    end = first.end
    if first.kind == "punct" and first.text in "+-":
        end = r.take().end
    elif first.kind == "word" and r.is_punct("("):
        start = r.peek().start  # type: ignore[union-attr]
        end = start + len(r.group())
    return r.text[first.start : end]


def _create_table(statement: str) -> Table:
    r = _Reader(statement)
    r.expect("CREATE")
    r.expect("TABLE")
    schema, name = _table_name(r.multipart())
    table = Table(schema=schema, name=name)
    r.expect_punct("(")
    unstated: set[str] = set()
    while True:
        if r.is_word("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"):
            _constraint(r, table, None)
        elif r.is_word("INDEX", "PERIOD"):
            raise Unreadable("an inline index or period is not read")
        elif (name := _column(r, table)) is not None:
            unstated.add(name)
        if r.accept_punct(")"):
            break
        r.expect_punct(",")
    r.accept_punct(";")
    if not r.done():
        raise Unreadable("the CREATE TABLE has options after its column list (a filegroup, WITH, ...)")
    # A primary key makes its columns NOT NULL when they did not say.
    in_key = {c for k in table.keys if k.primary for c, _ in k.columns}
    for col in table.columns:
        if col.name in unstated and col.name in in_key:
            col.nullable = False
    return table


# --- the statements after it ------------------------------------------------------------


def _same_table(parts: list[str], table: Table) -> None:
    schema, name = _table_name(parts)
    if (schema.lower(), name) != (table.schema.lower(), table.name):
        raise Unreadable(f"a statement for another table ({schema}.{name})")


def _index(r: _Reader, table: Table) -> None:
    unique = r.accept("UNIQUE")
    kind = _index_kind(r, "NONCLUSTERED")
    if r.accept("COLUMNSTORE"):
        kind += " COLUMNSTORE"
    r.expect("INDEX")
    name = r.name_part()
    r.expect("ON")
    _same_table(r.multipart(), table)
    idx = Index(name=name, unique=unique, index_type=kind, columns=[])
    if kind == "CLUSTERED COLUMNSTORE":
        if r.is_punct("("):
            raise Unreadable("an ordered clustered columnstore index")
    else:
        idx.columns = r.column_list(sort_order=True)
        if kind == "NONCLUSTERED COLUMNSTORE":
            idx.columns = [(c, False) for c, _ in idx.columns]
        if r.accept("INCLUDE"):
            idx.included = [c for c, _ in r.column_list(sort_order=False)]
        if r.accept("WHERE"):
            idx.filter_definition = r.group() if r.is_punct("(") else _rest_as_filter(r)
    r.accept_punct(";")
    if not r.done():
        raise Unreadable(f"index {name} has options sqlsc does not script")
    table.indexes.append(idx)


def _rest_as_filter(r: _Reader) -> str:
    raise Unreadable("an index filter without parentheses (DacFx always writes them)")


def _alter(r: _Reader, table: Table) -> None:
    r.expect("ALTER")
    r.expect("TABLE")
    _same_table(r.multipart(), table)
    if r.accept("NOCHECK"):
        r.expect("CONSTRAINT")
        name = r.name_part()
        r.accept_punct(";")
        if not r.done():
            raise Unreadable("ALTER TABLE ... NOCHECK CONSTRAINT with more than one name")
        for item in [*table.checks, *table.foreign_keys]:
            if item.name == name:
                item.disabled = True
                item.not_trusted = True  # a disabled constraint is never trusted
                return
        raise Unreadable(f"NOCHECK CONSTRAINT {name}: no such constraint above it")
    untrusted = False
    if r.accept("WITH"):
        untrusted = r.expect("NOCHECK", "CHECK") == "NOCHECK"
    r.expect("ADD")
    added = _constraint(r, table, None)
    r.accept_punct(";")
    if not r.done():
        raise Unreadable("an ALTER TABLE ... ADD with more than one constraint")
    if untrusted:
        if isinstance(added, (CheckConstraint, ForeignKey)):
            added.not_trusted = True
        else:
            raise Unreadable("a key added WITH NOCHECK")


_EXTENDED_PROPERTY = re.compile(r"^\s*(?:EXEC(?:UTE)?\s+)?(?:sys\.)?sp_addextendedproperty\b", re.IGNORECASE)


def extended_property(batch: str) -> bool:
    """A batch that adds an extended property (DacFx writes MS_Description ones into
    the table's file); sqlsc does not track them."""
    return bool(_EXTENDED_PROPERTY.match(batch))


def parse(text: str) -> Table:
    """The table a table file's text describes. Raises Unreadable."""
    parts = [b for b in batches(text) if not extended_property(b)]
    if not parts:
        raise Unreadable("no CREATE TABLE")
    table = _create_table(parts[0])
    for part in parts[1:]:
        r = _Reader(part)
        if r.is_word("CREATE"):
            r.take()
            _index(r, table)
        elif r.is_word("ALTER"):
            _alter(r, table)
        else:
            raise Unreadable(f"a statement sqlsc does not read after the table: {part[:60]}")
    return table


# Any collation a file names is the column's own: render it whatever the database's is.
_ANY_COLLATION = "\0"


def is_sqlsc_shape(text: str) -> bool:
    """The layout sqlsc scripted tables in before 1.0.11: `CREATE TABLE name`, `(`, the
    columns, `);` as the first batch."""
    parts = batches(text)
    if not parts or not parts[0].upper().startswith("CREATE TABLE"):
        return False
    lines = parts[0].split("\n")
    return len(lines) >= 3 and lines[1].strip() == "(" and lines[-1].strip() == ");"


def sqlsc_shape(text: str) -> str:
    """The table text in the sqlsc shape: as it is when it already is in it, else read
    and written back (a DacFx file, a hand-written one). A text this reader cannot read
    comes back unchanged (it then never matches a scripted one, and get-latest holds
    it back as a manual change). Either way whether an enabled constraint is trusted
    is left out (settle_trust), as DacFx leaves it out."""
    if is_sqlsc_shape(text) and not any(extended_property(b) for b in batches(text)):
        return settle_trust(text)
    try:
        table = parse(text)
    except Unreadable:
        return text
    return script_table_sqlsc(table, database_collation=_ANY_COLLATION)


_WITH_NOCHECK_ADD = re.compile(
    r"^(ALTER TABLE .+?) WITH NOCHECK ADD (?:CONSTRAINT (\[(?:[^\]]|\]\])+\]|\S+) )?", re.IGNORECASE | re.MULTILINE
)
_NOCHECK_CONSTRAINT = re.compile(
    r"^ALTER TABLE .+? NOCHECK CONSTRAINT (\[(?:[^\]]|\]\])+\]|[^\s;]+);?[ \t]*$", re.IGNORECASE | re.MULTILINE
)


def settle_trust(text: str) -> str:
    """A sqlsc-shape text with `WITH NOCHECK` taken off every constraint that is not also
    disabled: DacFx scripts an untrusted constraint like a trusted one, so a table SSMS
    wrote and the one sqlsc scripted before 1.0.11 must not differ on it. A disabled
    constraint keeps both statements (DacFx writes its NOCHECK CONSTRAINT)."""
    disabled = {m.group(1) for m in _NOCHECK_CONSTRAINT.finditer(text)}

    def drop(match: re.Match[str]) -> str:
        if match.group(2) is not None and match.group(2) in disabled:
            return match.group(0)
        named = f"CONSTRAINT {match.group(2)} " if match.group(2) is not None else ""
        return f"{match.group(1)} ADD {named}"

    return _WITH_NOCHECK_ADD.sub(drop, text)


def unreadable_reason(text: str) -> str | None:
    """Why the table text cannot be read, or None when it can."""
    if is_sqlsc_shape(text):
        return None
    try:
        parse(text)
    except Unreadable as exc:
        return str(exc)
    return None
