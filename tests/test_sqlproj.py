"""`sqlsc sqlproj` / `init --sqlproj`: a SQL project over the scripts folder for a compile check."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from coop_sqlsc import cli as cli_mod
from coop_sqlsc import sqlproj


def run(root: Path, *args: str):
    return CliRunner().invoke(cli_mod.main, ["--json", "--config", str(root / "sqlsc.yml"), *args])


def test_project_name_comes_from_the_database_name():
    assert sqlproj.project_name("WH Gold", "database") == "WHGold"
    assert sqlproj.project_name("Sales-2026.v2", "database") == "Sales-2026.v2"
    assert sqlproj.project_name("   ", "scripts/db") == "db"


def test_every_kind_has_a_platform():
    from coop_sqlsc.config import KINDS

    assert set(KINDS) == set(sqlproj.PLATFORMS)


def test_init_with_sqlproj_writes_the_project_and_ignores_the_build_output(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = run(tmp_path, "init", "--kind", "fabric_warehouse", "--server", "x", "--database", "WH Gold", "--sqlproj")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["sqlproj"] == "database/WHGold.sqlproj"
    text = (tmp_path / "database" / "WHGold.sqlproj").read_text(encoding="utf-8")
    assert 'Sdk="Microsoft.Build.Sql/' in text
    assert "<DSP>Microsoft.Data.Tools.Schema.Sql.SqlDwUnifiedDatabaseSchemaProvider</DSP>" in text
    assert '<Build Remove="Data/**" />' in text and '<Build Remove="Migrations/**" />' in text
    ignored = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".sqlsc/" in ignored and "bin/" in ignored and "obj/" in ignored
    # Run again: the project is left as it is (people edit it), the ignore lines are not repeated.
    again = json.loads(run(tmp_path, "sqlproj").output)
    assert again["written"] is False and again["path"] == "database/WHGold.sqlproj" and again["ignored"] == []
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8").count("bin/") == 1
    # A hand-renamed project is still the folder's project.
    (tmp_path / "database" / "WHGold.sqlproj").rename(tmp_path / "database" / "Gold.sqlproj")
    assert json.loads(run(tmp_path, "sqlproj").output)["path"] == "database/Gold.sqlproj"


def test_sqlproj_without_git_still_writes_next_to_the_config(tmp_path):
    result = run(tmp_path, "init", "--kind", "azure_sql", "--server", "x", "--database", "Sales")
    assert result.exit_code == 0, result.output
    done = json.loads(run(tmp_path, "sqlproj", "--name", "sales").output)
    assert done["ok"] and done["written"] and done["path"] == "database/sales.sqlproj"
    assert "SqlAzureV12DatabaseSchemaProvider" in (tmp_path / "database" / "sales.sqlproj").read_text(encoding="utf-8")
