"""End to end against a real SQL Server (skipped unless SQLSC_TEST_SERVER is set)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from coop_sqlsc.cli import main
from coop_sqlsc.layout import read_folder
from coop_sqlsc.objects import OBJECT_TYPES

from .conftest import SAMPLE, batches, run_script

pytestmark = pytest.mark.integration


def link(tmp_path: Path, server: str, database: str) -> Path:
    config = tmp_path / "sqlsc.yml"
    config.write_text(
        "folder: database\n"
        "target: dev\n"
        "targets:\n"
        "  dev:\n"
        "    kind: sql_server\n"
        f"    server: '{server}'\n"
        f"    database: {database}\n"
        "    auth: sql\n"
        "    user: sa\n"
        "    trust_server_certificate: true\n",
        encoding="utf-8",
    )
    return config


def sqlsc(config: Path, *args: str) -> dict:
    result = CliRunner().invoke(main, ["--json", "--config", str(config), *args])
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


def test_script_then_status_is_clean(tmp_path, server, make_db):
    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db)

    first = sqlsc(config, "status")
    assert first["summary"]["new"] == 8, first["changes"]
    assert {c["object"] for c in first["changes"]} >= {
        "[sales]",
        "[sales].[Customer]",
        "[sales].[Order]",
        "[sales].[vCustomerTotals]",
        "[dbo].[fn_Double]",
        "[sales].[usp_GetCustomer]",
        "[sales].[trCustomer_Touch]",
    }

    written = sqlsc(config, "script")
    assert len(written["files"]) == 8
    assert (tmp_path / "database" / "Tables" / "sales.Order.sql").is_file()

    again = sqlsc(config, "status")
    assert again["changes"] == []


def test_scripts_rebuild_an_identical_database(tmp_path, server, make_db):
    """The files are complete: running them into an empty database scripts back the same."""
    source = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, source)
    sqlsc(config, "script")

    objects, _ = read_folder(tmp_path / "database")
    order = list(OBJECT_TYPES)
    # Type order plus foreign keys after every table is enough for the sample;
    # real dependency ordering arrives with `get-latest` in phase 2.
    staged: list[tuple[int, int, str]] = []
    for key, text in objects.items():
        for batch in batches(text):
            staged.append((order.index(key.type), 1 if "FOREIGN KEY" in batch else 0, batch))
    staged.sort(key=lambda s: (s[1] if s[0] == order.index("table") else 0, s[0]))
    deploy = "\nGO\n".join(batch for _, _, batch in staged) + "\nGO\n"
    copy = make_db(deploy)

    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    config_copy = link(copy_dir, server, copy)
    (copy_dir / "database").symlink_to(tmp_path / "database", target_is_directory=True)
    assert sqlsc(config_copy, "status")["changes"] == []


def test_changed_and_deleted_objects_show_up(tmp_path, server, make_db):
    import pyodbc

    from coop_sqlsc.connect import pick_driver

    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db)
    sqlsc(config, "script")

    import os

    conn = pyodbc.connect(
        f"Driver={{{pick_driver(pyodbc.drivers())}}};Server={server};Database={db};UID=sa;"
        f"PWD={os.environ['SQLSC_PASSWORD']};Encrypt=yes;TrustServerCertificate=yes;",
        autocommit=True,
    )
    run_script(
        conn,
        "ALTER PROCEDURE sales.usp_GetCustomer @CustomerId int AS SELECT 1;\nGO\n"
        "DROP FUNCTION dbo.fn_Double;\nGO\nALTER TABLE sales.Customer ADD Phone varchar(20) NULL;\nGO",
    )
    conn.close()

    status = sqlsc(config, "status")
    got = {(c["action"], c["object"]) for c in status["changes"]}
    assert got == {
        ("changed", "[sales].[usp_GetCustomer]"),
        ("deleted", "[dbo].[fn_Double]"),
        ("changed", "[sales].[Customer]"),
    }

    diff = sqlsc(config, "diff", "sales.Customer")
    assert len(diff["changes"]) == 1
    assert "+     ,Phone varchar(20) NULL" in diff["changes"][0]["diff"]

    sqlsc(config, "script")
    assert not (tmp_path / "database" / "Functions" / "dbo.fn_Double.sql").exists()
    assert sqlsc(config, "status")["changes"] == []
