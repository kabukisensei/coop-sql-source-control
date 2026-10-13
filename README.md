# coop-sql-source-control

Source control for SQL Server, Azure SQL and Microsoft Fabric databases, in the
spirit of Redgate SQL Source Control: every object lives in the repo as its own
`.sql` file, and one command tells you what differs between a database and the
repo. Object checkout and check-in, get latest, deploy and an SSMS add-in follow
in later phases (see the [roadmap](#roadmap)).

It is part of the coop suite: a Python CLI installed with pipx like
`coop-data-doc`, readable for people, and `--json` on every command for
coop-agent, CI and the SSMS add-in.

## Install

```powershell
pipx install git+https://github.com/kabukisensei/coop-sql-source-control
```

Needs Python 3.10 to 3.13 and the ODBC Driver 18 for SQL Server
(`winget install --id Microsoft.msodbcsql.18 -e`). The tool installs as both
`sqlsc` and `coop-sqlsc`.

## Quick start

```powershell
cd my-sql-repo
sqlsc init --kind azure_sql --server myserver.database.windows.net --database Sales
sqlsc status          # what differs between the database and the repo
sqlsc diff dbo.Foo    # the text of one difference
sqlsc script          # write the database's objects into the repo folder
git add database; git commit -m "Script Sales"
```

`init` writes `sqlsc.yml` at the repo root and a `.gitattributes` for the
scripts. Inside a repo that has coop's `.coop/project.yml`, `sqlsc init` with no
options uses the contract's `sql_targets` (dev, test, prod) as they are.

## Commands

| Command | What it does |
| --- | --- |
| `sqlsc init` | Links the repo to a database by writing `sqlsc.yml` |
| `sqlsc targets` | Lists the databases the repo can talk to |
| `sqlsc status` | Lists objects that are new, changed or deleted in the database compared with the repo; `--exit-code` exits 1 when anything differs |
| `sqlsc diff [objects…]` | Shows the text differences, for everything or the objects named (`dbo.Customer`, `Customer`) |
| `sqlsc script [objects…]` | Writes new and changed objects into the repo and removes files for dropped ones; `--dry-run` shows what it would do |

Global options: `--json` prints one JSON document (`{"ok": true, …}`, or
`{"ok": false, "error": {"kind", "message"}}` with exit code 2), `--config PATH`
uses a specific `sqlsc.yml`, and `-t/--target NAME` picks a target per command.
No command ever prompts.

## Targets

| Kind | Platform | What is scripted |
| --- | --- | --- |
| `sql_server` | SQL Server on-premises or in a VM | Schemas, tables (columns, keys, checks, foreign keys, indexes), views, functions, procedures, triggers |
| `azure_sql` | Azure SQL Database, including serverless | Same as SQL Server |
| `fabric_sql_database` | Fabric SQL database | Same as SQL Server |
| `fabric_warehouse` | Fabric Warehouse | Schemas, tables (keys as `NOT ENFORCED`), views, functions, procedures |
| `fabric_lakehouse` | Fabric Lakehouse SQL analytics endpoint | Schemas, views, functions, procedures (Delta tables are read-only) |
| `synapse_serverless` | Synapse serverless SQL pool | Schemas, views, functions, procedures |

`sqlsc.yml`:

```yaml
folder: database          # object scripts, relative to this file
target: dev               # used when a command names none
targets:
  dev:
    kind: azure_sql
    server: myserver.database.windows.net
    database: Sales       # auth defaults to entra for every kind but sql_server
  local:
    kind: sql_server
    server: localhost,1433
    database: Sales
    auth: sql             # sql | windows | entra | entra-interactive
    user: sa
    password_env: SQLSC_PASSWORD   # the password is read from this variable, never stored
    trust_server_certificate: true # local dev only
  gold:
    kind: fabric_warehouse
    server: abc123.datawarehouse.fabric.microsoft.com  # the item's SQL connection string
    database: WH_Gold
```

Entra sign-in uses `azure-identity`: `az login`, a service principal in the
environment (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET`) or a
managed identity. Set `tenant_id` on a target to pin a tenant.

When the repo (or a parent folder) has coop's `.coop/project.yml`, its
`sql_targets` entries are targets too, and its `default_environment` is the
default. An entry in `sqlsc.yml` with the same name is laid over the coop entry
field by field; Fabric Warehouse and Lakehouse entries need that to add the
`server`, which coop itself discovers through the Fabric API.

## Repo layout

```
database/
  Security/Schemas/sales.sql
  Tables/sales.Customer.sql
  Views/sales.vCustomerTotals.sql
  Functions/dbo.fn_Double.sql
  Stored Procedures/sales.usp_GetCustomer.sql
  Triggers/sales.trCustomer_Touch.sql
```

Scripts are deterministic: the same object always produces the same bytes, names
are bracketed only where needed, column lists use Cooptimize's leading-comma
layout, and constraint names SQL Server generated itself (`PK__Tag__3214EC07…`)
are left out because they differ in every database. Views, procedures, functions
and triggers are stored exactly as written, with line endings and trailing
spaces normalized.

## Use from coop-agent

See [docs/coop-agent.md](docs/coop-agent.md).

## Develop

```bash
pip install -e ".[dev]" ruff
ruff check src tests && ruff format --check src tests
pytest                                    # unit tests
docker run -d -e ACCEPT_EULA=Y -e 'MSSQL_SA_PASSWORD=Sqlsc_Dev_Pw1!' -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest
SQLSC_TEST_SERVER=localhost,1433 SQLSC_PASSWORD='Sqlsc_Dev_Pw1!' pytest   # plus the live tests
```

The live tests build a sample database, script it, rebuild a second database
from the files and check that it scripts back identically.

## Roadmap

1. **Script and diff** (this release): `init`, `targets`, `status`, `diff`, `script`.
2. **Commit and get latest**: `commit` scripts and commits in one step; `get-latest` applies repo changes to a dev database in dependency order.
3. **Checkout and check-in**: a shared lock table, `checkout`, `checkin`, `locks`, and an optional edit guard on SQL Server and Azure SQL.
4. **Deploy and static data**: release scripts for test and prod, lookup-table data as `MERGE` scripts.
5. **SSMS add-in**: a C# shell over the same CLI.

Design notes: [SQL Source Control design proposal](https://claude.ai/code/artifact/b6a4d639-6a01-40a7-a64f-cb97231ce159).
