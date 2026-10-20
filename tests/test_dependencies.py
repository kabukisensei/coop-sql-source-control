"""Dependencies.json: the database's own dependency list, for lineage tools."""

from __future__ import annotations

import json

import pytest

from coop_sqlsc import dependencies as deps

from .conftest import SAMPLE, connect, run_script
from .test_integration import link, sqlsc
from .test_phase2 import git

# --- units ----------------------------------------------------------------------------------


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql):
        assert "sys.sql_expression_dependencies" in sql

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


def test_rows_become_a_sorted_stable_file(tmp_path):
    found = deps.read(
        _Conn(
            [
                ("sales", "vTotals", "V", "sales", "Customer", "U", None, None),
                ("dbo", "usp_Load", "P", None, "Staging", None, None, None),  # unqualified, unresolved
                ("dbo", "usp_Load", "P", "dbo", "Orders", "U", "OtherDb", None),  # another database
                ("sales", "trAudit", "TR", "sales", "Customer", "U", None, None),
                ("queryinsights", "x", "V", "sys", "y", "V", None, None),  # a platform schema
                ("sales", "vTotals", "V", "sales", "Customer", "U", None, None),  # a duplicate
            ]
        ),
        {"queryinsights"},
    )
    text = deps.render(found)
    doc = json.loads(text)
    assert doc["version"] == 1 and doc["source"] == "sys.sql_expression_dependencies"
    assert doc["dependencies"] == [
        {
            "object": {"schema": "dbo", "name": "usp_Load", "type": "procedure"},
            "references": {"schema": None, "name": "Staging", "type": None},
        },
        {
            "object": {"schema": "dbo", "name": "usp_Load", "type": "procedure"},
            "references": {"schema": "dbo", "name": "Orders", "type": "table", "database": "OtherDb"},
        },
        {
            "object": {"schema": "sales", "name": "trAudit", "type": "trigger"},
            "references": {"schema": "sales", "name": "Customer", "type": "table"},
        },
        {
            "object": {"schema": "sales", "name": "vTotals", "type": "view"},
            "references": {"schema": "sales", "name": "Customer", "type": "table"},
        },
    ]
    assert deps.write(tmp_path, text) is True
    assert deps.write(tmp_path, text) is False  # unchanged: not rewritten
    assert (tmp_path / "Dependencies.json").read_bytes().endswith(b"]\n}\n")


# --- against a live SQL Server ----------------------------------------------------------------


@pytest.mark.integration
def test_dependencies_are_written_then_kept_current_by_commit(tmp_path, server, make_db):
    db = make_db(SAMPLE.read_text())
    config = link(tmp_path, server, db)
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "dev@example.com")
    git(tmp_path, "config", "user.name", "Dev")
    sqlsc(config, "commit", "-m", "Schema")
    path = tmp_path / "database" / "Dependencies.json"
    assert not path.exists()  # opt-in: no file until asked

    written = sqlsc(config, "dependencies")
    assert written["changed"] is True and written["dependencies"] > 0
    pairs = {
        (d["object"]["schema"], d["object"]["name"], d["references"]["schema"], d["references"]["name"])
        for d in json.loads(path.read_text())["dependencies"]
    }
    assert ("sales", "vAllTotals", "sales", "vCustomerTotals") in pairs
    assert ("sales", "usp_GetCustomer", "sales", "Customer") in pairs

    # The new file alone is something to commit.
    committed = sqlsc(config, "commit", "-m", "Track dependencies")
    assert committed["commit"]
    assert "database/Dependencies.json" in git(tmp_path, "show", "--name-only", "--format=", "HEAD")

    # A view changed in the database: its commit carries the refreshed list.
    run_script(
        connect(server, db),
        "ALTER VIEW sales.vAllTotals AS SELECT SUM(t.Total) AS GrandTotal, "
        "(SELECT COUNT(*) FROM sales.Customer) AS Customers FROM sales.vCustomerTotals AS t",
    )
    sqlsc(config, "commit", "-m", "Count customers")
    files = git(tmp_path, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert "database/Dependencies.json" in files
    assert sqlsc(config, "commit", "-m", "Nothing")["commit"] is None
