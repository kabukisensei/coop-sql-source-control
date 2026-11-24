# Script a database

Status and diff show what the live database has that the repo does not. Script writes those objects into files. Dry-run shows the same plan and leaves the object files untouched.

## Sub-features

- `status-new` reports the table that exists only in the database.
- `diff-table` shows the text of that one object.
- `script-dry-run` plans the write and does not create the table file.
- `script-write` writes the table file and the schema file.
- `status-uncommitted` still reports the new files until they are committed.

## How to get to it (user POV)

- Run `sqlsc status`.
- Run `sqlsc diff sales.Customer`.
- Run `sqlsc script --dry-run`.
- Run `sqlsc script`.

## Driving it with verify.sh

Preconditions:

- `./scripts/verify.sh --sql` reached `select1: ok`.
- A database this run created contains `CREATE SCHEMA sales` and `sales.Customer (CustomerId int NOT NULL CONSTRAINT PK_Customer PRIMARY KEY, Name nvarchar(100) NOT NULL)`.
- `sqlsc init` in a fresh git repo points the `dev` target at that database, layout `schema`, auth `sql`, user `sa`, `--trust-server-certificate`. The password is only in `SQLSC_PASSWORD`.

- **Status.** Run `sqlsc --json status --exit-code`. Exit 1, `"ok": true`, and the changes include schema `sales`, name `Customer`, action `new`. `summary.new` is at least 1.
- **Diff.** Run `sqlsc --json diff sales.Customer`. Exit 0. One change, name `Customer`, and its `diff` text contains `CustomerId`.
- **Dry-run.** Run `sqlsc --json script --dry-run`. Exit 0, `"dry_run": true`, and `files` is non-empty. `database/sales/Tables/Customer.sql` does not exist. `.sqlsc/sync/dev.json` does not exist either: nothing has matched, so there is no sync to record.
- **Script.** Run `sqlsc --json script`. Exit 0, `"dry_run": false`. `database/sales/Tables/Customer.sql` exists and contains `CREATE TABLE`, `CustomerId`, and `PK_Customer`. `database/Security/sales.sql` exists and contains `CREATE SCHEMA`. `.sqlsc/sync/dev.json` now exists.
- **Uncommitted status.** Run `sqlsc --json status --exit-code` again. Exit 1, and the `Customer` change has `"uncommitted": true`.

## Gotchas

- Exit 1 from `status --exit-code` with `"ok": true` means the repo and the database differ. It is not a crashed command.
- `--dry-run` still connects. The JSON lists the files it would write, and those files must be absent afterward. The sync record appears only after the real `script`, once the file matches the database.
- Layout `schema` puts the table at `database/sales/Tables/Customer.sql` and the schema at `database/Security/sales.sql`. A file at `database/Tables/sales.Customer.sql` is the other layout.
- Do not point this target at a database people are using. The harness creates and later drops its own databases.
