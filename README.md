# coop-sql-source-control

Source control for SQL Server, Azure SQL and Microsoft Fabric databases, in the
spirit of Redgate SQL Source Control: every object lives in the repo as its own
`.sql` file, and one command tells you what differs between a database and the
repo. `sqlsc commit` puts database changes into git, and `sqlsc get-latest`
brings a database up to date with the repo. `sqlsc checkout` tells the team
you are editing an object and `sqlsc checkin` commits it and lets it go.
`sqlsc data add` keeps a lookup table's rows in the repo too, and `sqlsc deploy`
releases a tagged commit to test or prod. An SSMS add-in follows (see the
[roadmap](#roadmap)).

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
sqlsc commit -m "Script Sales"   # write the changed objects and commit just those files
git pull                          # a teammate's changes arrive as files
sqlsc get-latest                  # apply them to your dev database
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
| `sqlsc commit [objects…] -m "…"` | Does what `script` does, then commits exactly those files (nothing else you have staged), plus object files that already match the database but are not committed yet (from `script`, `data add`, or an edit applied with `get-latest`); `--push` pushes the branch |
| `sqlsc get-latest [objects…]` | Changes the database to match the repo; `--dry-run` prints the script, `--output FILE` saves it without running it, `--allow-drops` also drops objects and columns the repo no longer has |
| `sqlsc checkout objects… [-m note]` | Checks objects out to you; `--type view` checks out one you are about to create |
| `sqlsc checkin [objects…] [-m "…"]` | Commits your checked-out objects (all of them when none are named) and releases them; `--no-commit` only releases them |
| `sqlsc locks` | Lists who has what checked out; `--mine` for yours, `--all-repos` for every repo sharing the lock table |
| `sqlsc unlock objects…` | Releases checkouts without committing; someone else's need `--force` |
| `sqlsc guard install\|remove\|status` | The optional edit guard (see below) |
| `sqlsc data add tables…` | Starts keeping these tables' rows in the repo as static data |
| `sqlsc data remove tables…` | Stops keeping them (the rows stay in the database) |
| `sqlsc deploy` | Releases a commit (`--ref`, default `HEAD`) to a database; `--output FILE` writes the script for review, `--confirm NAME` is needed for a protected target |
| `sqlsc history` | Lists the releases `deploy` has made to a database |

Global options: `--json` prints one JSON document (`{"ok": true, …}`, or
`{"ok": false, "error": {"kind", "message"}}` with exit code 2), `--config PATH`
uses a specific `sqlsc.yml`, and `-t/--target NAME` picks a target per command.
No command ever prompts.

You are your git `user.email` for checkouts. `--as NAME` (or the `SQLSC_USER`
environment variable) overrides it, for an agent or a shared machine.

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
field by field. A Fabric Warehouse or Lakehouse entry with `workspace_id` and
`item_id` but no `server` (how coop names them) gets its SQL host from the
Fabric API, signed in the same way, with the contract's `fabric.tenant_id`;
add `server:` in `sqlsc.yml` to skip the lookup. The Fabric system schemas
`queryinsights` and `_rsc` are never scripted.

## How get-latest changes a database

- Objects only in the repo are created; foreign keys are added after every new table exists.
- Views, functions, procedures and triggers that differ are changed with `ALTER`, so their permissions stay.
  Ones that read each other are retried until they succeed, so file order never matters.
- Tables are altered, never rebuilt: columns are added, changed (`ALTER COLUMN`) or, with
  `--allow-drops`, dropped, and constraints and indexes are dropped and added by name. Rows are kept.
- Changes `ALTER` cannot make safely (a new `IDENTITY`, a changed computed column, removing a
  constraint SQL Server named itself) are listed as held back, with the reason; everything else runs.
- On SQL Server, Azure SQL and Fabric SQL database the whole update is one transaction: if any
  statement fails, nothing changes. Fabric Warehouse and Lakehouse run the statements one by one.
- Targets named `prod`, `production`, `prd` or `live`, or marked `protected: true`, are never
  changed by `get-latest`; `--output` still writes the script for review.
- Column order is not compared: `ALTER TABLE` can only add a column at the end, so a column placed
  mid-table in the repo lands last in the database and both sides still count as the same.

## Checkout and check-in

Checkouts are rows in a small table, `sqlsc.ObjectLock`, which the first
`sqlsc checkout` creates. By default it lives in the database being worked on,
which suits a team sharing one dev database. Teams where everyone has their own
dev database point every clone at one shared database instead:

```yaml
locks:
  target: shared   # any target in this file: Azure SQL, SQL Server or Fabric Warehouse
  repo: sales-db   # optional; locks are scoped by the git origin URL by default
```

A Fabric Lakehouse or Synapse serverless pool cannot hold the table, so those
need `locks: target:`. The `sqlsc` schema is never scripted or compared.

- `status` shows who has each changed object checked out.
- `commit` leaves objects someone else has checked out for them to check in
  (`--force` commits them anyway); `checkin` commits only your own.
- Checking out the same object twice is fine; checking out someone else's fails
  with their name, when they took it and their note, and exits 1.

**The edit guard** (`sqlsc guard install`) goes further on SQL Server, Azure SQL
and Fabric SQL database: a database-level DDL trigger rolls back an `ALTER`,
`DROP` or index change to an object that a different login has checked out, and
says who has it. It reads the lock table in the same database, so it only works
when locks are not kept elsewhere, and it tells people apart by database login:
two people sharing one SQL login are the same person to it. Remove it with
`sqlsc guard remove`. Note that `get-latest` run by someone else is stopped by
it too, and rolled back as a whole.

## Static data

Lookup tables (statuses, countries, settings) often need their rows in source
control as well as their structure. `sqlsc data add sales.Status` writes
`Data/sales.Status.sql`; the file being there is what tracks the table, and
`sqlsc data remove` deletes it. From then on the rows are one more object:
`status` and `diff` show changed rows, `commit` writes them, and `get-latest` and
`deploy` apply them.

The file is a single `MERGE` that makes the table hold exactly its rows: missing
rows are inserted, differing ones updated, extra ones deleted, matched on the
primary key (or the first unique constraint; a table with neither cannot be
tracked). Identity values are kept with `IDENTITY_INSERT`. Values are written so
that the same rows always give the same file: dates in ISO 8601, floats with all
17 digits, line breaks inside a value as `NCHAR(13)`/`NCHAR(10)`. Comparisons
are case-sensitive, so `'Ok'` changed to `'OK'` is a change. Computed and
`rowversion` columns are left out, and `geography`, `geometry` and `sql_variant`
columns cannot be tracked. Static data suits small tables; `sqlsc` warns above
1,000 rows. When one tracked table refers to another, `get-latest` retries the
`MERGE` until the order works out, inside the same transaction.

## Releasing to test and prod

`sqlsc deploy` is `get-latest` for a release: it compares a database with a
commit of the repo rather than your working folder, so only what has been
committed (and tagged, if you release by tag) goes out.

```powershell
sqlsc deploy -t test --ref v1.4                     # straight to test
sqlsc deploy -t prod --ref v1.4 -o release-v1.4.sql # the script, for review
sqlsc deploy -t prod --ref v1.4 --confirm prod      # run it
sqlsc history -t prod                               # what has been released there
```

The script says which commit it releases and when it was written, against the
database as it was at that moment, so write it again if the database changes
before it runs. Protected targets (named `prod`, `production`, `prd` or `live`,
or `protected: true`) need `--confirm` with the target's name. Every release, run
by `sqlsc` or by hand from the script in SSMS or sqlcmd, ends by adding a row to
`sqlsc.Deployment` with the commit, the ref, who prepared it and the login that
ran it; `sqlsc history` reads that table. Drops still need `--allow-drops`, and
changes `ALTER` cannot make safely are still held back with the reason.

## Repo layout

```
database/
  Security/Schemas/sales.sql
  Tables/sales.Customer.sql
  Views/sales.vCustomerTotals.sql
  Functions/dbo.fn_Double.sql
  Stored Procedures/sales.usp_GetCustomer.sql
  Triggers/sales.trCustomer_Touch.sql
  Data/sales.Status.sql
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
from the files with `get-latest` and check that it scripts back identically, then
exercise table alters with data in place, held-back drops, rollback on failure,
protected targets, `commit`, checkouts between two users, a separate lock
database, the edit guard against a second login, static data across every
column type, and releases by tag to a protected target.

## Roadmap

1. **Script and diff**: `init`, `targets`, `status`, `diff`, `script`.
2. **Commit and get latest**: `commit` scripts and commits in one step; `get-latest` applies repo changes to a dev database.
3. **Checkout and check-in**: a shared lock table, `checkout`, `checkin`, `locks`, `unlock`, and an optional edit guard on SQL Server and Azure SQL.
4. **Deploy and static data** (this release): `deploy` and `history` for test and prod releases, lookup-table rows as `MERGE` scripts.
5. **SSMS add-in**: a C# shell over the same CLI.

Design notes: [SQL Source Control design proposal](https://claude.ai/code/artifact/b6a4d639-6a01-40a7-a64f-cb97231ce159).
