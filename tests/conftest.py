from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from coop_sqlsc.objects import batches

SAMPLE = Path(__file__).parent / "sql" / "sample_db.sql"


def run_script(conn, text: str) -> None:
    cur = conn.cursor()
    for batch in batches(text):
        cur.execute(batch)


@pytest.fixture(scope="session")
def server():
    """A live SQL Server: SQLSC_TEST_SERVER=host,port with SQLSC_PASSWORD for `sa`."""
    host = os.environ.get("SQLSC_TEST_SERVER")
    if not host or not os.environ.get("SQLSC_PASSWORD"):
        pytest.skip("set SQLSC_TEST_SERVER and SQLSC_PASSWORD to run integration tests")
    return host


@pytest.fixture
def make_db(server):
    """Creates empty databases for one test and drops them afterwards."""
    import pyodbc

    from coop_sqlsc.connect import pick_driver

    driver = pick_driver(pyodbc.drivers())
    master = pyodbc.connect(
        f"Driver={{{driver}}};Server={server};Database=master;UID=sa;PWD={os.environ['SQLSC_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=yes;",
        autocommit=True,
    )
    created: list[str] = []

    def factory(script: str | None = None) -> str:
        name = f"sqlsc_test_{uuid.uuid4().hex[:8]}"
        master.cursor().execute(f"CREATE DATABASE [{name}]")
        created.append(name)
        if script:
            conn = pyodbc.connect(
                f"Driver={{{driver}}};Server={server};Database={name};UID=sa;"
                f"PWD={os.environ['SQLSC_PASSWORD']};Encrypt=yes;TrustServerCertificate=yes;",
                autocommit=True,
            )
            run_script(conn, script)
            conn.close()
        return name

    yield factory
    for name in created:
        master.cursor().execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        master.cursor().execute(f"DROP DATABASE [{name}]")
    master.close()


def connect(server: str, database: str):
    import pyodbc

    from coop_sqlsc.connect import pick_driver

    return pyodbc.connect(
        f"Driver={{{pick_driver(pyodbc.drivers())}}};Server={server};Database={database};UID=sa;"
        f"PWD={os.environ['SQLSC_PASSWORD']};Encrypt=yes;TrustServerCertificate=yes;",
        autocommit=True,
    )
