"""Living next to SSMS's Database DevOps workload and Git windows (2026-10-27).

SSMS's Create Project from Database (and SqlPackage extract) write a schema-first layout,
`dbo/Tables/Customer.sql`, that sqlsc does not read; SSMS saves .sql files with a BOM; and
SSMS opens SDK-style SQL projects of Microsoft.Build.Sql 2.1.0 or newer only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from coop_sqlsc import cli as cli_mod
from coop_sqlsc import sqlproj
from coop_sqlsc.layout import (
    delete_object,
    key_for_path,
    other_layout,
    read_folder,
    relative_path,
    sql_files,
    use_layout,
    write_object,
)
from coop_sqlsc.objects import ObjectKey

from .test_units import fake_db  # noqa: F401  (the faked database fixture)

VIEW = ObjectKey("view", "dbo", "vSales")
TABLE = ObjectKey("table", "dbo", "Customer")
TABLE_TEXT = "CREATE TABLE [dbo].[Customer]\n(\n      [Id] int NOT NULL\n);\nGO\n"
SCHEMA = ObjectKey("schema", "", "sales")


@pytest.fixture
def layout_reset():
    yield
    use_layout("type")


def run(tmp_path: Path, *args: str):
    return CliRunner().invoke(cli_mod.main, ["--json", "--config", str(tmp_path / "sqlsc.yml"), *args])


def _init(tmp_path: Path, *extra: str) -> dict:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "dev@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Dev"], check=True)
    result = run(
        tmp_path, "init", "--kind", "azure_sql", "--server", "s.database.windows.net", "--database", "Sales", *extra
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _git(tmp_path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout


def test_other_layout_names_schema_first_files_and_nothing_of_ours():
    paths = [
        "Tables/dbo.Customer.sql",  # sqlsc's own
        "Security/Schemas/sales.sql",  # sqlsc's own
        "Migrations/0001_backfill.sql",
        "README.md",
        "dbo/Tables/Customer.sql",  # SSMS / SqlPackage
        "sales\\Stored Procedures\\usp_Load.sql",
        "sales/StoredProcedures/usp_Other.sql",
        "Security/sales.sql",  # DacFx's schema file
        "dbo/Tables/Archive/Old.sql",  # too deep: not a layout we know
        "Sales.sqlproj",
    ]
    assert other_layout(paths) == [
        "Security/sales.sql",
        "dbo/Tables/Customer.sql",
        "sales/Stored Procedures/usp_Load.sql",
        "sales/StoredProcedures/usp_Other.sql",
    ]


def test_sql_files_lists_the_tree_and_read_folder_still_ignores_the_other_shape(tmp_path):
    write_object(tmp_path, VIEW, "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n")
    (tmp_path / "dbo" / "Tables").mkdir(parents=True)
    (tmp_path / "dbo" / "Tables" / "Customer.sql").write_text("CREATE TABLE [dbo].[Customer] ([Id] INT NOT NULL);\n")
    (tmp_path / ".sqlsc").mkdir()
    (tmp_path / ".sqlsc" / "x.sql").write_text("not scanned")
    assert sql_files(tmp_path) == ["Views/dbo.vSales.sql", "dbo/Tables/Customer.sql"]
    objects, unknown = read_folder(tmp_path)
    assert set(objects) == {VIEW} and unknown == []


def test_status_warns_and_script_commit_get_latest_refuse_on_a_mixed_folder(tmp_path, fake_db):  # noqa: F811
    # A `type` repo (it already holds Tables/dbo.Customer.sql) into which someone saved an
    # SSMS-shaped file: that file is not read, status says so, and nothing writes.
    _init(tmp_path)
    write_object(tmp_path / "database", TABLE, TABLE_TEXT)
    fake_db[TABLE] = TABLE_TEXT
    fake_db[VIEW] = "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n"
    ssms = tmp_path / "database" / "dbo" / "Views"
    ssms.mkdir(parents=True)
    (ssms / "vSales.sql").write_text("\ufeffCREATE VIEW [dbo].[vSales] AS SELECT 1;\r\n", encoding="utf-8")

    status = json.loads(run(tmp_path, "status").output)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "files")  # committed, so status has nothing to say about them
    status = json.loads(run(tmp_path, "status").output)
    assert status["ok"] and status["layout"] == "type"
    assert status["summary"] == {"new": 1, "changed": 0, "deleted": 0}
    assert any("dbo/Views/vSales.sql" in w and "`schema` layout" in w for w in status["warnings"])

    # A dry run only looks; everything that writes stops before touching anything.
    assert json.loads(run(tmp_path, "script", "--dry-run").output)["ok"]
    for args in (("script",), ("commit", "-m", "x", "--no-pull"), ("get-latest",)):
        result = run(tmp_path, *args)
        out = json.loads(result.output)
        assert result.exit_code == 2 and out["ok"] is False, (args, result.output)
        assert out["error"]["kind"] == "layout" and "Nothing was written" in out["error"]["message"], args
    assert not (tmp_path / "database" / "Views").exists()
    assert _git(tmp_path, "log", "--oneline").count("\n") == 1  # only the test's own commit


def test_write_object_keeps_a_bom_the_file_already_has_and_adds_none(tmp_path):
    path = write_object(tmp_path, VIEW, "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n")
    assert path.read_bytes()[:3] != b"\xef\xbb\xbf"
    # SSMS saved it with a BOM (and CRLF); sqlsc rewrites the text and leaves the BOM alone.
    path.write_bytes(b"\xef\xbb\xbfCREATE VIEW dbo.vSales AS SELECT 1;\r\nGO\r\n")
    write_object(tmp_path, VIEW, "CREATE VIEW dbo.vSales AS SELECT 2;\nGO\n")
    assert path.read_bytes() == b"\xef\xbb\xbfCREATE VIEW dbo.vSales AS SELECT 2;\nGO\n"
    objects, _ = read_folder(tmp_path)
    assert objects[VIEW] == "CREATE VIEW dbo.vSales AS SELECT 2;\nGO\n"


def test_sqlproj_sdk_is_one_ssms_opens():
    major, minor, *_ = (int(part) for part in sqlproj.SDK_VERSION.split("."))
    assert (major, minor) >= (2, 1), "SSMS's Database DevOps workload needs Microsoft.Build.Sql 2.1.0 or newer"
    assert f'Sdk="Microsoft.Build.Sql/{sqlproj.SDK_VERSION}"' in sqlproj.render("Sales", "azure_sql")


# --- the `schema` layout itself -----------------------------------------------------------


def test_paths_in_the_schema_layout_match_ssms_and_sqlpackage(layout_reset):
    use_layout("schema")
    assert relative_path(TABLE) == "dbo/Tables/Customer.sql"
    assert relative_path(ObjectKey("procedure", "sales", "usp Load")) == "sales/StoredProcedures/usp Load.sql"
    assert relative_path(SCHEMA) == "Security/sales.sql"
    assert relative_path(ObjectKey("data", "dbo", "Status")) == "Data/dbo.Status.sql"  # sqlsc's own, unchanged
    # A dot in a schema name is a folder name now, so it stays a dot.
    assert relative_path(ObjectKey("view", "a.b", "v")) == "a.b/Views/v.sql"
    for key in (TABLE, SCHEMA, ObjectKey("procedure", "sales", "usp Load"), ObjectKey("data", "dbo", "Status")):
        assert key_for_path(relative_path(key)) == key
    # Both spellings DacFx has used for procedures read back; sqlsc's own `type` files do not.
    assert key_for_path("dbo/Stored Procedures/p.sql") == ObjectKey("procedure", "dbo", "p")
    assert key_for_path("dbo/DatabaseTriggers/ddl.sql") is None and key_for_path("DatabaseTriggers/ddl.sql") is None
    assert key_for_path("dbo/UserDefinedTypes/Money.sql") is None
    assert key_for_path("Tables/dbo.Customer.sql") is None
    assert key_for_path("Security/Schemas/sales.sql") is None
    assert key_for_path("bin/Debug/x.sql") is None


def test_init_layout_schema_writes_the_key_and_the_files_follow(tmp_path, fake_db):  # noqa: F811
    done = _init(tmp_path, "--layout", "schema", "--sqlproj")
    assert done["layout"] == "schema"
    assert "layout: schema\n" in (tmp_path / "sqlsc.yml").read_text(encoding="utf-8")
    fake_db[VIEW] = "CREATE VIEW dbo.vSales AS SELECT 1;\nGO\n"
    fake_db[TABLE] = TABLE_TEXT
    fake_db[SCHEMA] = "CREATE SCHEMA [sales];\nGO\n"
    status = json.loads(run(tmp_path, "status").output)
    assert status["layout"] == "schema" and status["warnings"] == []
    assert sorted(c["path"] for c in status["changes"]) == [
        "Security/sales.sql",
        "dbo/Tables/Customer.sql",
        "dbo/Views/vSales.sql",
    ]

    first = json.loads(run(tmp_path, "commit", "-m", "baseline", "--no-pull").output)
    assert first["ok"] and first["summary"] == {"new": 3, "changed": 0, "deleted": 0}
    assert sorted(_git(tmp_path, "show", "--name-only", "--format=", "HEAD").split()) == [
        ".gitattributes",
        ".gitignore",
        "database/Security/sales.sql",
        "database/dbo/Tables/Customer.sql",
        "database/dbo/Views/vSales.sql",
        "sqlsc.yml",
    ]
    assert json.loads(run(tmp_path, "status").output)["summary"] == {"new": 0, "changed": 0, "deleted": 0}

    # The repo moves (a teammate's edit through SSMS, saved with a BOM and CRLF): get-latest sees it.
    path = tmp_path / "database" / "dbo" / "Views" / "vSales.sql"
    path.write_bytes(b"\xef\xbb\xbfCREATE VIEW dbo.vSales AS SELECT 2;\r\nGO\r\n")
    status = json.loads(run(tmp_path, "status").output)
    assert [(c["path"], c["origin"]) for c in status["changes"]] == [("dbo/Views/vSales.sql", "repo")]
    # Nothing of the `type` shape sneaks in: sqlsc's own old-style file is now the odd one out.
    (tmp_path / "database" / "Views").mkdir()
    (tmp_path / "database" / "Views" / "dbo.vOld.sql").write_text("CREATE VIEW dbo.vOld AS SELECT 1;\n")
    refused = json.loads(run(tmp_path, "script").output)
    assert refused["ok"] is False and refused["error"]["kind"] == "layout"
    assert "Views/dbo.vOld.sql" in refused["error"]["message"] and "`type` layout" in refused["error"]["message"]


def test_a_folder_ssms_filled_is_recognised_without_the_key(tmp_path, fake_db):  # noqa: F811
    # `sqlsc init` run at the repo root over a project SSMS created: no --layout needed.
    project = tmp_path / "Sales"
    (project / "dbo" / "Tables").mkdir(parents=True)
    (project / "dbo" / "Tables" / "Customer.sql").write_text(
        "\ufeff" + TABLE_TEXT.replace("\n", "\r\n"), encoding="utf-8"
    )
    (project / "Sales.sqlproj").write_text('<Project Sdk="Microsoft.Build.Sql/2.3.0" />\n')
    done = _init(tmp_path, "--folder", "Sales")
    assert done["layout"] == "schema"
    assert "layout:" not in (tmp_path / "sqlsc.yml").read_text(encoding="utf-8")  # the files say it
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "SSMS project")
    fake_db[TABLE] = TABLE_TEXT
    status = json.loads(run(tmp_path, "status").output)
    assert status["layout"] == "schema", status
    assert status["summary"] == {"new": 0, "changed": 0, "deleted": 0}, status
    assert status["warnings"] == []
    # And asking for the other layout over those files is refused rather than ignoring them.
    other = run(
        tmp_path,
        "init",
        "--force",
        "--folder",
        "Sales",
        "--layout",
        "type",
        "--kind",
        "azure_sql",
        "--server",
        "s",
        "--database",
        "Sales",
    )
    assert other.exit_code == 2 and json.loads(other.output)["error"]["kind"] == "layout"
    # sqlsc sqlproj leaves SSMS's project alone.
    assert json.loads(run(tmp_path, "sqlproj").output)["written"] is False


def test_a_bad_layout_key_is_a_config_error(tmp_path, fake_db):  # noqa: F811
    _init(tmp_path)
    (tmp_path / "sqlsc.yml").write_text((tmp_path / "sqlsc.yml").read_text() + "layout: redgate\n")
    out = json.loads(run(tmp_path, "status").output)
    assert out["ok"] is False and out["error"]["kind"] == "config" and "`layout`" in out["error"]["message"]


# --- DML triggers live in their table's file, as DacFx writes them ----------------------------

TRIGGER = ObjectKey("trigger", "dbo", "trCustomer")
TRIGGER_TEXT = "CREATE TRIGGER [dbo].[trCustomer] ON [dbo].[Customer] AFTER INSERT AS\nSELECT 1;\nGO\n"


def test_schema_layout_keeps_a_trigger_in_its_tables_file(tmp_path):
    use_layout("schema")
    write_object(tmp_path, TABLE, TABLE_TEXT)
    path = write_object(tmp_path, TRIGGER, TRIGGER_TEXT)
    assert path == tmp_path / "dbo" / "Tables" / "Customer.sql" and relative_path(TRIGGER) == "dbo/Tables/Customer.sql"
    assert path.read_text() == TABLE_TEXT + TRIGGER_TEXT
    assert not (tmp_path / "dbo" / "Triggers").exists()
    objects, unknown = read_folder(tmp_path)
    assert objects == {TABLE: TABLE_TEXT, TRIGGER: TRIGGER_TEXT} and unknown == []
    # The table is rewritten: its triggers stay. The trigger is rewritten: the table stays.
    wider = TABLE_TEXT.replace("[Id] int NOT NULL", "[Id] int NOT NULL\n     ,[Name] nvarchar(50) NULL")
    write_object(tmp_path, TABLE, wider)
    changed = TRIGGER_TEXT.replace("SELECT 1", "SELECT 2")
    write_object(tmp_path, TRIGGER, changed)
    assert read_folder(tmp_path)[0] == {TABLE: wider, TRIGGER: changed}
    # Dropping the trigger takes its batch out and leaves the table; dropping the table takes the file.
    delete_object(tmp_path, TRIGGER)
    assert read_folder(tmp_path)[0] == {TABLE: wider}
    delete_object(tmp_path, TABLE)
    assert not path.exists()


def test_a_dacfx_table_file_with_triggers_reads_as_table_plus_triggers(tmp_path):
    use_layout("schema")
    second = ObjectKey("trigger", "dbo", "trCustomer_Touch")
    dacfx = (
        "\ufeffCREATE TABLE [dbo].[Customer] (\n    [Id] INT NOT NULL\n);\r\n\r\n\r\nGO\r\n"
        "SET QUOTED_IDENTIFIER OFF\r\nGO\r\n"
        "CREATE TRIGGER [dbo].[trCustomer_Touch]\r\n    ON [dbo].[Customer]\r\n    FOR UPDATE\r\n    AS\r\n"
        "    BEGIN\r\n        SET NoCount ON\r\n    END\r\n\r\nGO\r\n" + TRIGGER_TEXT
    )
    (tmp_path / "dbo" / "Tables").mkdir(parents=True)
    (tmp_path / "dbo" / "Tables" / "Customer.sql").write_text(dacfx, encoding="utf-8")
    objects, _ = read_folder(tmp_path)
    assert set(objects) == {TABLE, TRIGGER, second}
    assert objects[TABLE] == "CREATE TABLE [dbo].[Customer] (\n    [Id] INT NOT NULL\n);\nGO\n"
    assert objects[second].startswith("SET QUOTED_IDENTIFIER OFF\nGO\nCREATE TRIGGER [dbo].[trCustomer_Touch]")
    assert objects[TRIGGER] == TRIGGER_TEXT
    assert relative_path(second) == "dbo/Tables/Customer.sql"


def test_a_trigger_file_from_1_0_9_is_read_and_moves_into_the_table_file_on_write(tmp_path):
    use_layout("schema")
    write_object(tmp_path, TABLE, TABLE_TEXT)
    old = tmp_path / "dbo" / "Triggers" / "trCustomer.sql"
    old.parent.mkdir(parents=True)
    old.write_text(TRIGGER_TEXT)
    objects, _ = read_folder(tmp_path)
    assert objects == {TABLE: TABLE_TEXT, TRIGGER: TRIGGER_TEXT}
    write_object(tmp_path, TRIGGER, TRIGGER_TEXT.replace("SELECT 1", "SELECT 2"))
    assert not old.exists()
    assert (tmp_path / "dbo" / "Tables" / "Customer.sql").read_text().count("CREATE TRIGGER") == 1


def test_status_and_commit_place_a_new_trigger_in_the_table_file(tmp_path, fake_db):  # noqa: F811
    _init(tmp_path, "--layout", "schema")
    fake_db[TABLE] = TABLE_TEXT
    fake_db[TRIGGER] = TRIGGER_TEXT
    status = json.loads(run(tmp_path, "status").output)
    assert {(c["object"], c["path"]) for c in status["changes"]} == {
        ("[dbo].[Customer]", "dbo/Tables/Customer.sql"),
        ("[dbo].[trCustomer]", "dbo/Tables/Customer.sql"),
    }
    done = json.loads(run(tmp_path, "commit", "-m", "table and trigger", "--no-pull").output)
    assert done["ok"] and done["summary"]["new"] == 2
    assert _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split() == [
        ".gitattributes",
        ".gitignore",
        "database/dbo/Tables/Customer.sql",
        "sqlsc.yml",
    ]
    assert json.loads(run(tmp_path, "status").output)["summary"] == {"new": 0, "changed": 0, "deleted": 0}
    # The trigger goes in the database: commit removes its batch and keeps the table's file.
    del fake_db[TRIGGER]
    status = json.loads(run(tmp_path, "status").output)
    assert [(c["object"], c["action"]) for c in status["changes"]] == [("[dbo].[trCustomer]", "deleted")]
    assert json.loads(run(tmp_path, "commit", "-m", "drop trigger", "--no-pull").output)["ok"]
    assert (tmp_path / "database" / "dbo" / "Tables" / "Customer.sql").read_text() == TABLE_TEXT
    assert json.loads(run(tmp_path, "status").output)["summary"] == {"new": 0, "changed": 0, "deleted": 0}


# --- the 2026-10-28 audit: #63 reserved-name schemas, #65 legacy trigger files, #66 shared files


def test_a_schema_named_like_a_folder_keeps_its_objects(tmp_path, layout_reset):  # 63
    from coop_sqlsc.compare import compare
    from coop_sqlsc.deploy import build_plan
    from coop_sqlsc.layout import detect_layout, objects_from_files

    use_layout("schema")
    objects = {
        ObjectKey("table", "Data", "T"): "CREATE TABLE [Data].[T]\n(\n    [Id] int NOT NULL\n);\nGO\n",
        ObjectKey("view", "Security", "v"): "CREATE VIEW [Security].[v] AS SELECT 1;\nGO\n",
        ObjectKey("procedure", "Tables", "p"): "CREATE PROCEDURE [Tables].[p] AS SELECT 1;\nGO\n",
        ObjectKey("function", "bin", "f"): "CREATE FUNCTION [bin].[f]() RETURNS int AS BEGIN RETURN 1 END;\nGO\n",
        ObjectKey("table", "MIGRATIONS", "m"): "CREATE TABLE [MIGRATIONS].[m]\n(\n    [Id] int NOT NULL\n);\nGO\n",
        ObjectKey("schema", "", "Data"): "CREATE SCHEMA [Data];\nGO\n",
        ObjectKey("data", "Data", "Status"): "INSERT INTO [Data].[Status] ([Id]) VALUES (1);\nGO\n",
    }
    for key, text in objects.items():
        write_object(tmp_path, key, text)
    files = sql_files(tmp_path)
    assert files == [
        "Data/Data.Status.sql",
        "Data/Tables/T.sql",
        "MIGRATIONS/Tables/m.sql",
        "Security/Data.sql",
        "Security/Views/v.sql",
        "Tables/StoredProcedures/p.sql",
        "bin/Functions/f.sql",
    ]
    assert detect_layout(files) == "schema" and other_layout(files) == []
    folder, unknown = read_folder(tmp_path)
    assert folder == objects and unknown == []
    assert objects_from_files({rel: (tmp_path / rel).read_text() for rel in files})[0] == objects
    # The committed tree reads the same, so a release never plans a DROP for them.
    plan = build_plan(compare(objects, folder), allow_drops=True)
    assert plan.runnable == [] and plan.steps == []
    # The 2-part shapes keep their meanings beside the 3-part ones.
    assert key_for_path("Data/dbo.T.sql") == ObjectKey("data", "dbo", "T")
    assert key_for_path("Security/sales.sql") == ObjectKey("schema", "", "sales")
    assert key_for_path("bin/Debug/x.sql") is None and key_for_path("obj/Release/Model.sql") is None
    # And in a `type` repo such files are still the other layout's.
    use_layout("type")
    assert other_layout(["Data/Tables/T.sql", "Data/dbo.T.sql", "Tables/dbo.T.sql"]) == ["Data/Tables/T.sql"]


def test_status_and_commit_see_a_table_in_a_schema_named_data(tmp_path, fake_db):  # noqa: F811  # 63
    _init(tmp_path, "--layout", "schema")
    key = ObjectKey("table", "Data", "T")
    text = "CREATE TABLE [Data].[T]\n(\n    [Id] int NOT NULL\n);\nGO\n"
    fake_db[key] = text
    status = json.loads(run(tmp_path, "status").output)
    assert [(c["object"], c["path"], c["action"]) for c in status["changes"]] == [
        ("[Data].[T]", "Data/Tables/T.sql", "new")
    ]
    assert json.loads(run(tmp_path, "commit", "-m", "data schema", "--no-pull").output)["ok"]
    assert json.loads(run(tmp_path, "status").output)["summary"] == {"new": 0, "changed": 0, "deleted": 0}
    assert (tmp_path / "database" / "Data" / "Tables" / "T.sql").read_text() == text


def _legacy_repo(tmp_path, fake_db, trigger_text=TRIGGER_TEXT):  # noqa: F811
    """A `schema` repo as 1.0.9 left it: the table's file and the trigger's own file, committed."""
    _init(tmp_path, "--layout", "schema")
    scripts = tmp_path / "database"
    write_object(scripts, TABLE, TABLE_TEXT)
    legacy = scripts / "dbo" / "Triggers" / "trCustomer.sql"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(trigger_text)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "as 1.0.9 wrote it")
    fake_db[TABLE] = TABLE_TEXT
    fake_db[TRIGGER] = trigger_text
    assert json.loads(run(tmp_path, "status").output)["summary"] == {"new": 0, "changed": 0, "deleted": 0}
    return legacy


def _head_objects(tmp_path):
    from coop_sqlsc import git_ops
    from coop_sqlsc.layout import objects_from_files

    return objects_from_files(git_ops.read_tree(tmp_path, "HEAD", tmp_path / "database"))[0]


def test_a_trigger_moving_out_of_its_1_0_9_file_commits_the_removal_too(tmp_path, fake_db):  # noqa: F811  # 65
    legacy = _legacy_repo(tmp_path, fake_db)
    changed = TRIGGER_TEXT.replace("SELECT 1", "SELECT 2")
    fake_db[TRIGGER] = changed
    done = json.loads(run(tmp_path, "commit", "-m", "trigger v2", "--no-pull").output)
    assert done["ok"] and done["summary"] == {"new": 0, "changed": 1, "deleted": 0}
    assert not legacy.exists()
    assert _git(tmp_path, "status", "--porcelain").strip() == ""
    assert _git(tmp_path, "show", "--name-status", "--format=", "HEAD").split() == [
        "M",
        "database/dbo/Tables/Customer.sql",
        "D",
        "database/dbo/Triggers/trCustomer.sql",
    ]
    assert _head_objects(tmp_path) == {TABLE: TABLE_TEXT, TRIGGER: changed}
    assert json.loads(run(tmp_path, "status").output)["summary"] == {"new": 0, "changed": 0, "deleted": 0}


def test_dropping_a_trigger_still_in_its_1_0_9_file_removes_that_file(tmp_path, fake_db):  # noqa: F811  # 65
    legacy = _legacy_repo(tmp_path, fake_db)
    del fake_db[TRIGGER]
    status = json.loads(run(tmp_path, "status").output)
    assert [(c["object"], c["action"]) for c in status["changes"]] == [("[dbo].[trCustomer]", "deleted")]
    done = json.loads(run(tmp_path, "commit", "-m", "drop trigger", "--no-pull").output)
    assert done["ok"] and done["summary"]["deleted"] == 1
    assert not legacy.exists()
    assert _git(tmp_path, "status", "--porcelain").strip() == ""
    assert _head_objects(tmp_path) == {TABLE: TABLE_TEXT}
    assert json.loads(run(tmp_path, "status").output)["summary"] == {"new": 0, "changed": 0, "deleted": 0}
    # delete_object alone does the same for a trigger that sits in both places.
    scripts = tmp_path / "database"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(TRIGGER_TEXT)
    write_object(scripts, TRIGGER, TRIGGER_TEXT)
    legacy.write_text(TRIGGER_TEXT)
    read_folder(scripts)
    delete_object(scripts, TRIGGER)
    assert not legacy.exists() and read_folder(scripts)[0] == {TABLE: TABLE_TEXT}


def _store(monkeypatch, *keys):
    """A lock store with these objects checked out by dev@example.com; records releases."""
    from datetime import datetime

    from coop_sqlsc.locks import Lock

    locks = {k: Lock("repo", k, "dev@example.com", "dev", datetime(2026, 10, 28, 12, 0), None) for k in keys}
    released: list = []

    class FakeStore:
        def __init__(self, conn, transactional):
            pass

        def list(self, repo):
            return list(locks.values())

        def release(self, wanted, *, repo, who, force=False):
            done = [k for k in wanted if k in locks]
            for k in done:
                locks.pop(k)
                released.append(k)
            return done, []

    monkeypatch.setattr(cli_mod, "LockStore", FakeStore)
    return released


def test_checkin_commits_a_scripted_trigger_from_its_tables_file(tmp_path, fake_db, monkeypatch):  # noqa: F811  # 66
    _init(tmp_path, "--layout", "schema")
    fake_db[TABLE] = TABLE_TEXT
    fake_db[TRIGGER] = TRIGGER_TEXT
    assert json.loads(run(tmp_path, "commit", "-m", "baseline", "--no-pull").output)["ok"]
    changed = TRIGGER_TEXT.replace("SELECT 1", "SELECT 2")
    fake_db[TRIGGER] = changed
    assert json.loads(run(tmp_path, "script", "dbo.trCustomer").output)["ok"]  # the file matches the database now
    status = json.loads(run(tmp_path, "status").output)
    # status counts the file owed to git as the trigger's change, not the table's
    assert [(c["object"], c["action"], c["path"]) for c in status["changes"]] == [
        ("[dbo].[trCustomer]", "changed", "dbo/Tables/Customer.sql")
    ]
    released = _store(monkeypatch, TRIGGER)
    done = json.loads(run(tmp_path, "checkin", "dbo.trCustomer", "--no-pull", "--as", "dev@example.com").output)
    assert done["ok"], done
    assert [(f["object"], f["action"]) for f in done["files"]] == [("[dbo].[trCustomer]", "changed")]
    assert done["commit"] and done["held_back"] == [] and released == [TRIGGER]
    assert _head_objects(tmp_path) == {TABLE: TABLE_TEXT, TRIGGER: changed}
    assert _git(tmp_path, "status", "--porcelain").strip() == ""


def test_commit_and_checkin_name_the_trigger_not_the_table_and_hold_a_shared_file(tmp_path, fake_db, monkeypatch):  # noqa: F811  # 66
    _init(tmp_path, "--layout", "schema")
    second = ObjectKey("trigger", "dbo", "trCustomer_Touch")
    second_text = "CREATE TRIGGER [dbo].[trCustomer_Touch] ON [dbo].[Customer] AFTER UPDATE AS\nSELECT 10;\nGO\n"
    fake_db[TABLE] = TABLE_TEXT
    fake_db[TRIGGER] = TRIGGER_TEXT
    fake_db[second] = second_text
    assert json.loads(run(tmp_path, "commit", "-m", "baseline", "--no-pull").output)["ok"]
    # Both triggers change; the person applies both with script, then checks in only one.
    fake_db[TRIGGER] = TRIGGER_TEXT.replace("SELECT 1", "SELECT 2")
    fake_db[second] = second_text.replace("SELECT 10", "SELECT 20")
    assert json.loads(run(tmp_path, "script").output)["ok"]
    status = json.loads(run(tmp_path, "status").output)
    assert [(c["object"], c["action"]) for c in status["changes"]] == [
        ("[dbo].[trCustomer]", "changed"),
        ("[dbo].[trCustomer_Touch]", "changed"),
    ]
    # commit of one named trigger: its file also carries the other's edit, so it waits.
    one = json.loads(run(tmp_path, "commit", "dbo.trCustomer", "-m", "one", "--no-pull").output)
    assert one["commit"] is None and one["files"] == []
    assert [h["object"] for h in one["held_back"]] == ["[dbo].[trCustomer]"]
    assert one["held_back"][0]["reason"].startswith("shares dbo/Tables/Customer.sql with an uncommitted change to ")
    assert "[dbo].[trCustomer_Touch]" in one["held_back"][0]["reason"]
    # checkin of one: kept checked out, not released, nothing committed.
    released = _store(monkeypatch, TRIGGER, second)
    kept = json.loads(run(tmp_path, "checkin", "dbo.trCustomer", "--no-pull", "--as", "dev@example.com").output)
    assert kept["ok"] is False and kept["commit"] is None and released == []
    assert [h["object"] for h in kept["held_back"]] == ["[dbo].[trCustomer]"]
    assert _git(tmp_path, "log", "--oneline").count("\n") == 1
    # Both named together: committed as one, both released, the table untouched in HEAD.
    both = json.loads(run(tmp_path, "checkin", "--no-pull", "--as", "dev@example.com").output)
    assert both["ok"] and both["commit"] and both["held_back"] == []
    assert sorted(f["object"] for f in both["files"]) == ["[dbo].[trCustomer]", "[dbo].[trCustomer_Touch]"]
    assert sorted(released, key=lambda k: k.sort_key) == [TRIGGER, second]
    head = _head_objects(tmp_path)
    assert head[TABLE] == TABLE_TEXT and head[TRIGGER].count("SELECT 2") == 1 and head[second].count("SELECT 20") == 1
    assert _git(tmp_path, "status", "--porcelain").strip() == ""
