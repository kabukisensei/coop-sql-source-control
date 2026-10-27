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
    assert relative_path(ObjectKey("procedure", "sales", "usp Load")) == "sales/Stored Procedures/usp Load.sql"
    assert relative_path(SCHEMA) == "Security/sales.sql"
    assert relative_path(ObjectKey("data", "dbo", "Status")) == "Data/dbo.Status.sql"  # sqlsc's own, unchanged
    # A dot in a schema name is a folder name now, so it stays a dot.
    assert relative_path(ObjectKey("view", "a.b", "v")) == "a.b/Views/v.sql"
    for key in (TABLE, SCHEMA, ObjectKey("procedure", "sales", "usp Load"), ObjectKey("data", "dbo", "Status")):
        assert key_for_path(relative_path(key)) == key
    # Both spellings DacFx has used for procedures read back; sqlsc's own `type` files do not.
    assert key_for_path("dbo/StoredProcedures/p.sql") == ObjectKey("procedure", "dbo", "p")
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
