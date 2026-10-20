"""--allow-drops against a real SQL Server: a column still read by a plain (not schema-bound)
view or procedure is not dropped (skipped unless SQLSC_TEST_SERVER is set)."""

from __future__ import annotations

import pytest

from .conftest import connect, run_script
from .test_integration import link, sqlsc

pytestmark = pytest.mark.integration

SCHEMA = """
CREATE TABLE dbo.Orders
    (OrderId int NOT NULL PRIMARY KEY, Amount decimal(18,2) NOT NULL, OrderedAt date NULL, Spare int NULL);
GO
INSERT INTO dbo.Orders VALUES (1, 10.00, '2026-10-01', NULL);
GO
CREATE VIEW dbo.vOrders AS SELECT OrderId, Amount, OrderedAt FROM dbo.Orders;
GO
CREATE PROCEDURE dbo.usp_Spare AS SELECT Spare FROM dbo.Orders;
GO
"""


def test_a_column_a_plain_view_reads_is_not_dropped(tmp_path, server, make_db):
    # Found on a Fabric SQL database: sys.sql_expression_dependencies records a plain view
    # against the table only, so the guard let the drop through and broke the view.
    db = make_db(SCHEMA)
    config = link(tmp_path, server, db)
    sqlsc(config, "script")
    table = tmp_path / "database" / "Tables" / "dbo.Orders.sql"
    table.write_text(table.read_text().replace("     ,OrderedAt date NULL\n", ""))

    refused = sqlsc(config, "get-latest", "--allow-drops")
    assert refused["ok"] is False and refused["transaction"] is True
    message = refused["failed"][0]["error"]
    assert "dbo.Orders.OrderedAt is still used by [dbo].[vOrders]" in message
    cur = connect(server, db).cursor()
    assert cur.execute("SELECT COL_LENGTH('dbo.Orders', 'OrderedAt')").fetchone()[0] is not None


def test_a_column_only_a_procedure_reads_is_not_dropped_and_an_unused_one_is(tmp_path, server, make_db):
    db = make_db(SCHEMA)
    config = link(tmp_path, server, db)
    sqlsc(config, "script")
    table = tmp_path / "database" / "Tables" / "dbo.Orders.sql"
    original = table.read_text()

    table.write_text(original.replace("     ,Spare int NULL\n", ""))
    refused = sqlsc(config, "get-latest", "--allow-drops")
    assert "is still used by [dbo].[usp_Spare]" in refused["failed"][0]["error"]

    # Change the procedure in the same update and the drop goes through.
    proc = tmp_path / "database" / "Stored Procedures" / "dbo.usp_Spare.sql"
    proc.write_text("CREATE PROCEDURE dbo.usp_Spare AS SELECT OrderId FROM dbo.Orders;\nGO\n")
    run_script(connect(server, db), "ALTER PROCEDURE dbo.usp_Spare AS SELECT OrderId FROM dbo.Orders;")
    dropped = sqlsc(config, "get-latest", "--allow-drops")
    assert dropped["ok"] is True, dropped
    cur = connect(server, db).cursor()
    assert cur.execute("SELECT COL_LENGTH('dbo.Orders', 'Spare')").fetchone()[0] is None
