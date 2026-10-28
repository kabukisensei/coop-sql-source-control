"""Table files the way SSMS 22 / DacFx writes them (1.0.11).

tests/dacfx/ holds byte-exact files SSMS 22's own DacFx (170) wrote for the tables in
tests/sql/probe_db.sql on the dev VM (2026-10-28), fake objects only:

- extract/   DacServices.Extract, what "Create Project from Database" writes;
- extract2/  the same after `ADD [Extra]`, a WITH NOCHECK check and a disabled foreign key;
- compare/   Schema Compare from a model built from the script into an empty project.

sqlsc must write the extract files byte for byte (CRLF aside: git stores LF), read all
three, and apply a table change a person made in SSMS through get-latest.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from coop_sqlsc import tablefile
from coop_sqlsc.compare import comparable
from coop_sqlsc.deploy import ManualChange, table_changes
from coop_sqlsc.layout import (
    dacfx_file_text,
    read_folder,
    split_table_file,
    use_layout,
    write_object,
)
from coop_sqlsc.objects import ObjectKey, normalize
from coop_sqlsc.scripter import (
    CheckConstraint,
    Column,
    KeyConstraint,
    Table,
    script_table,
    script_table_dacfx,
    script_table_sqlsc,
)

from .conftest import connect, run_script
from .test_integration import sqlsc

DACFX = Path(__file__).parent / "dacfx"
PROBE = Path(__file__).parent / "sql" / "probe_db.sql"
TABLES = ["Heap", "Order", "Parent", "Plain", "Wide"]


def key(name: str) -> ObjectKey:
    return ObjectKey("table", "probe", name)


def lf(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def without_triggers(text: str) -> str:
    """A DacFx table file up to its first trigger (the trigger text is the database's own)."""
    cut = text.find("\nCREATE TRIGGER")
    return text if cut < 0 else text[:cut].rstrip("\n") + "\n\n"


# --- reading and writing the shape -------------------------------------------------------


@pytest.mark.parametrize("folder", ["extract", "extract2"])
@pytest.mark.parametrize("name", TABLES)
def test_a_dacfx_file_reads_and_writes_back_byte_for_byte(folder, name):
    text = normalize(lf(DACFX / folder / f"{name}.sql"))
    table_text, _triggers = split_table_file(key(name), text)
    table = tablefile.parse(table_text)
    written = script_table_dacfx(table, database_collation="\0")
    assert dacfx_file_text(written) == without_triggers(lf(DACFX / folder / f"{name}.sql"))


@pytest.mark.parametrize("name", TABLES)
def test_schema_compare_files_are_read_too(name):
    # The model route keeps the script's own spacing (DEFAULT (0), [Amount] * [Line]):
    # read, but only the database-normalized text matches the database.
    text = normalize(lf(DACFX / "compare" / f"{name}.sql"))
    table_text, _ = split_table_file(key(name), text)
    assert tablefile.unreadable_reason(table_text) is None
    assert tablefile.is_sqlsc_shape(tablefile.sqlsc_shape(table_text))


def test_nocheck_reads_as_dacfx_writes_it():
    table_text, _ = split_table_file(key("Order"), normalize(lf(DACFX / "extract2" / "Order.sql")))
    table = tablefile.parse(table_text)
    fk = {f.name: f for f in table.foreign_keys}["FK_Order_Parent"]
    assert fk.disabled and fk.not_trusted
    line2 = {c.name: c for c in table.checks}["CK_Order_Line2"]
    assert not line2.not_trusted  # added WITH NOCHECK; DacFx does not keep that
    assert "ALTER TABLE probe.[Order] NOCHECK CONSTRAINT FK_Order_Parent;" in tablefile.sqlsc_shape(table_text)


TABLE = Table(
    "dbo",
    "T",
    columns=[
        Column("Id", "int", nullable=False, identity=("1", "1")),
        Column("Name", "nvarchar", max_length=200, default_name="DF_T_Name", default_definition="('')"),
    ],
    keys=[KeyConstraint("PK_T", True, "CLUSTERED", [("Id", False)])],
    checks=[CheckConstraint("CK_T", "([Id]>(0))", not_trusted=True)],
)


def test_an_older_sqlsc_file_and_a_dacfx_file_compare_alike():
    old = script_table_sqlsc(TABLE, database_collation=None)
    new = script_table(TABLE, database_collation=None)
    assert "WITH NOCHECK" in old and "WITH NOCHECK" not in new
    k = ObjectKey("table", "dbo", "T")
    assert comparable(k, old) == comparable(k, new)
    # An older file keeps comparing on its own text, so sync records stay valid.
    assert tablefile.sqlsc_shape(script_table_sqlsc(Table("dbo", "U", [Column("a", "int")]), database_collation=None))


def test_a_hand_written_create_table_reads_like_the_scripted_one():
    hand = (
        "CREATE TABLE dbo.T (\n  Id int IDENTITY(1,1) NOT NULL CONSTRAINT PK_T PRIMARY KEY,\n"
        "  Name nvarchar(100) NULL CONSTRAINT DF_T_Name DEFAULT (''),\n"
        "  CONSTRAINT CK_T CHECK ([Id]>(0))\n);\n"
    )
    k = ObjectKey("table", "dbo", "T")
    assert comparable(k, hand) == comparable(k, script_table(TABLE, database_collation=None))


def test_a_table_with_options_sqlsc_does_not_script_is_held_back_with_the_reason():
    k = ObjectKey("table", "dbo", "T")
    scripted = script_table(TABLE, database_collation=None)
    compressed = scripted.replace("([Id] ASC)", "([Id] ASC) WITH (DATA_COMPRESSION = PAGE)")
    assert tablefile.sqlsc_shape(compressed) == compressed
    with pytest.raises(ManualChange, match="index options"):
        table_changes(k, scripted, compressed, allow_drops=False)


def test_get_latest_works_out_alters_from_a_dacfx_file():
    k = ObjectKey("table", "dbo", "T")
    db = script_table(TABLE, database_collation=None)
    repo = db.replace(
        "    [Name] NVARCHAR (100)",
        "    [Name] NVARCHAR (200)",
    ).replace("    CONSTRAINT [PK_T]", "    [Note] VARCHAR (10)    NULL,\n    CONSTRAINT [PK_T]")
    steps, _notes = table_changes(k, db, repo, allow_drops=False)
    assert "ALTER TABLE dbo.T ADD Note varchar(10) NULL" in steps
    assert "ALTER TABLE dbo.T ALTER COLUMN Name nvarchar(200) NULL" in steps


EXTENDED = "EXECUTE sp_addextendedproperty @name = N'MS_Description', @value = N'People', @level0type = N'SCHEMA', @level0name = N'dbo', @level1type = N'TABLE', @level1name = N'T';"  # noqa: E501


@pytest.mark.parametrize("layout", ["type", "schema"])
def test_extended_properties_are_read_past_and_kept_on_a_rewrite(tmp_path, layout):
    use_layout(layout)
    k = ObjectKey("table", "dbo", "T")
    scripted = script_table(TABLE, database_collation=None)
    with_description = scripted.rstrip("\n") + f"\n\n{EXTENDED}\n\n\nGO\n"
    assert comparable(k, with_description) == comparable(k, scripted)
    path = write_object(tmp_path, k, with_description)
    wider = scripted.replace("NVARCHAR (100)", "NVARCHAR (200)")
    write_object(tmp_path, k, wider)  # what commit does with the database's script
    text = path.read_text()
    assert "NVARCHAR (200)" in text and EXTENDED in text
    assert comparable(k, read_folder(tmp_path)[0][k]) == comparable(k, wider)


TRIGGER = ObjectKey("trigger", "dbo", "trT")
TRIGGER_TEXT = "CREATE TRIGGER dbo.trT ON dbo.T AFTER UPDATE AS\nBEGIN\n    SET NOCOUNT ON;\nEND\nGO\n"


def test_a_table_file_with_a_trigger_is_written_with_dacfx_spacing(tmp_path):
    use_layout("schema")
    k = ObjectKey("table", "dbo", "T")
    write_object(tmp_path, k, script_table(TABLE, database_collation=None))
    path = write_object(tmp_path, TRIGGER, TRIGGER_TEXT)
    text = path.read_text()
    assert text.endswith(
        ");\n\n\nGO\n\nCREATE TRIGGER dbo.trT ON dbo.T AFTER UPDATE AS\nBEGIN\n    SET NOCOUNT ON;\nEND\n\nGO\n\n"
    )
    # A trigger change leaves the table's part exactly as the file had it, and the other way round.
    hand_spaced = text.replace("CREATE TABLE [dbo].[T] (", "CREATE TABLE [dbo].[T]  (")
    path.write_text(hand_spaced)
    write_object(tmp_path, TRIGGER, TRIGGER_TEXT.replace("SET NOCOUNT ON;", "SET NOCOUNT OFF;"))
    assert path.read_text().split("CREATE TRIGGER")[0] == hand_spaced.split("CREATE TRIGGER")[0]
    objects, _ = read_folder(tmp_path)
    assert set(objects) == {k, TRIGGER}


# --- against a real SQL Server ----------------------------------------------------------


def link_schema(tmp_path: Path, server: str, database: str) -> Path:
    config = tmp_path / "sqlsc.yml"
    config.write_text(
        "folder: database\nlayout: schema\ntarget: dev\ntargets:\n  dev:\n    kind: sql_server\n"
        f"    server: '{server}'\n    database: {database}\n    auth: sql\n    user: sa\n"
        "    trust_server_certificate: true\n",
        encoding="utf-8",
    )
    return config


@pytest.mark.integration
def test_sqlsc_writes_table_files_as_ssms_extracts_them(tmp_path, server, make_db):
    db = make_db(PROBE.read_text())
    config = link_schema(tmp_path, server, db)
    sqlsc(config, "script")
    tables = tmp_path / "database" / "probe" / "Tables"
    for name in TABLES:
        written = (tables / f"{name}.sql").read_text()
        assert without_triggers(written) == without_triggers(lf(DACFX / "extract" / f"{name}.sql")), name
    assert "\nGO\n\nCREATE TRIGGER [probe].[trOrder_Touch]" in (tables / "Order.sql").read_text()
    assert sqlsc(config, "status")["changes"] == []


@pytest.mark.integration
def test_files_ssms_wrote_read_clean_and_its_table_change_is_applied(tmp_path, server, make_db):
    db = make_db(PROBE.read_text())
    config = link_schema(tmp_path, server, db)
    sqlsc(config, "script")
    tables = tmp_path / "database" / "probe" / "Tables"
    for name in TABLES:  # the files as SSMS writes them: CRLF, DacFx's own text
        shutil.copyfile(DACFX / "extract" / f"{name}.sql", tables / f"{name}.sql")
    assert sqlsc(config, "status")["changes"] == []

    # A person's change in SSMS (Schema Compare's Update into the project): a column, a
    # check added WITH NOCHECK, a disabled foreign key.
    shutil.copyfile(DACFX / "extract2" / "Order.sql", tables / "Order.sql")
    status = sqlsc(config, "status")
    assert [(c["action"], c["object"]) for c in status["changes"]] == [("changed", "[probe].[Order]")]
    result = sqlsc(config, "get-latest")
    assert result["ok"] and result["failed"] == [] and result["held_back"] == [], result
    assert sqlsc(config, "status")["changes"] == []
    cur = connect(server, db).cursor()
    assert cur.execute("SELECT COL_LENGTH('probe.[Order]', 'Extra')").fetchone()[0] == 4
    disabled = cur.execute("SELECT is_disabled FROM sys.foreign_keys WHERE name = 'FK_Order_Parent'").fetchone()[0]
    assert disabled

    # The database's script now writes the file SSMS wrote, trigger aside.
    sqlsc(config, "script")
    assert without_triggers((tables / "Order.sql").read_text()) == without_triggers(
        lf(DACFX / "extract2" / "Order.sql")
    )


@pytest.mark.integration
def test_an_untrusted_constraint_does_not_show_as_a_change(tmp_path, server, make_db):
    db = make_db(PROBE.read_text())
    run_script(
        connect(server, db),
        "ALTER TABLE probe.[Order] WITH NOCHECK ADD CONSTRAINT [CK_Order_Line2] CHECK ([Line] < 1000);",
    )
    config = link_schema(tmp_path, server, db)
    sqlsc(config, "script")
    assert sqlsc(config, "status")["changes"] == []
    text = (tmp_path / "database" / "probe" / "Tables" / "Order.sql").read_text()
    assert "    CONSTRAINT [CK_Order_Line2] CHECK ([Line]<(1000)),\n" in text
