"""A SQL project over the scripts folder, so a build can compile the repo before a deploy.

`sqlsc sqlproj` writes `<folder>/<name>.sqlproj`, an SDK-style project (Microsoft.Build.Sql)
that takes every `.sql` file under the scripts folder except the static data `MERGE`
files and the migration scripts. `dotnet build` on it resolves every reference and
fails on bad syntax or a missing object, producing a dacpac nobody has to publish: the
build is the check, `get-latest` and `deploy` stay the way changes reach a database. SSMS
(Database DevOps workload) and Visual Studio open the same file as a database project.
"""

from __future__ import annotations

from pathlib import Path

# The SDK version the project pins. SSMS's Database DevOps workload opens SDK-style projects
# of Microsoft.Build.Sql 2.1.0 or newer only, so the pin stays at or above that.
SDK_VERSION = "2.3.0"

# DacFx's target platform per kind of database (the project's DSP). Fabric SQL database
# is Azure SQL under the hood; a Lakehouse SQL analytics endpoint and Synapse serverless
# share the serverless provider, which knows views, functions and procedures only.
PLATFORMS = {
    "sql_server": "Microsoft.Data.Tools.Schema.Sql.Sql160DatabaseSchemaProvider",
    "azure_sql": "Microsoft.Data.Tools.Schema.Sql.SqlAzureV12DatabaseSchemaProvider",
    "fabric_sql_database": "Microsoft.Data.Tools.Schema.Sql.SqlAzureV12DatabaseSchemaProvider",
    "fabric_warehouse": "Microsoft.Data.Tools.Schema.Sql.SqlDwUnifiedDatabaseSchemaProvider",
    "fabric_lakehouse": "Microsoft.Data.Tools.Schema.Sql.SqlServerlessDatabaseSchemaProvider",
    "synapse_serverless": "Microsoft.Data.Tools.Schema.Sql.SqlServerlessDatabaseSchemaProvider",
}

IGNORE_LINES = ("bin/", "obj/")


def project_name(database: str, folder: str) -> str:
    """A project file name from the database's name (letters, digits, `_`, `-`, `.`), else the folder's."""
    keep = "".join(ch for ch in database.strip() if ch.isalnum() or ch in "_-.")
    return keep.strip(".") or Path(folder).name or "database"


def render(name: str, kind: str) -> str:
    platform = PLATFORMS[kind]
    return (
        f'<Project Sdk="Microsoft.Build.Sql/{SDK_VERSION}">\n'
        "  <!-- Written by sqlsc. `dotnet build` compiles every object script into a dacpac so a\n"
        "       pull request fails on bad syntax or a missing reference. Nothing publishes the\n"
        "       dacpac: sqlsc get-latest and deploy apply the repo. Edit freely; sqlsc never rewrites it. -->\n"
        "  <PropertyGroup>\n"
        f"    <Name>{name}</Name>\n"
        f"    <DSP>{platform}</DSP>\n"
        "    <ModelCollation>1033, CI</ModelCollation>\n"
        "    <TreatWarningsAsErrors>false</TreatWarningsAsErrors>\n"
        "  </PropertyGroup>\n"
        "  <ItemGroup>\n"
        "    <!-- Static data (MERGE scripts) and run-once migration scripts are not schema. Only the\n"
        "         files directly in Data/ and Migrations/: a schema named Data or Migrations keeps its\n"
        "         objects in Data/Tables/ and the like in the schema layout. -->\n"
        '    <Build Remove="Data/*.sql" />\n'
        '    <Build Remove="Migrations/*.sql" />\n'
        "  </ItemGroup>\n"
        "</Project>\n"
    )


def write(scripts_dir: Path, name: str, kind: str, *, force: bool = False) -> tuple[Path, bool]:
    """Writes `<scripts_dir>/<name>.sqlproj`. Returns (path, written): an existing file is
    left alone unless `force`, since people edit it."""
    if kind not in PLATFORMS:
        raise ValueError(f"no SQL project platform for kind '{kind}'")
    path = scripts_dir / f"{name}.sqlproj"
    # One project per folder (dotnet build refuses a folder with two): an existing one,
    # whatever its name, is the project unless --force names a new one.
    existing = sorted(scripts_dir.glob("*.sqlproj")) if scripts_dir.is_dir() else []
    if existing and not force:
        return existing[0], False
    scripts_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(render(name, kind), encoding="utf-8", newline="\n")
    return path, True


def ignore_build_output(root: Path) -> list[str]:
    """Adds the build folders to .gitignore; returns the lines it added."""
    ignore = root / ".gitignore"
    existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    present = set(existing.splitlines())
    missing = [line for line in IGNORE_LINES if line not in present]
    if not missing:
        return []
    with ignore.open("a", encoding="utf-8", newline="\n") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write("".join(f"{line}\n" for line in missing))
    return missing
