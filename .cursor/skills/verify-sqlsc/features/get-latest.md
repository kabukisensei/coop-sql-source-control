# Get latest

Get latest changes a database to match the repo. `--output` writes that script and runs nothing. A following get-latest, aimed at an empty database, creates `sales.Customer`.

## Sub-features

- `output-only` writes the apply script and leaves the empty database untouched.
- `apply` creates `sales.Customer` in the empty database.
- `status-clean` reports that the empty database now matches the repo.

## How to get to it (user POV)

- Run `sqlsc get-latest --output <file>` against the database you have not updated yet.
- Open the file and confirm it is the change you expect.
- Run `sqlsc get-latest` to apply it.
- Run `sqlsc status` to confirm the database matches.

## Driving it with verify.sh

Preconditions:

- The commit feature has committed `sales.Customer` and `sales` in this repo.
- A second, empty database exists on the same server. The config's `dev` target still names the source database. A target named `empty` names the empty database, with the same server, `auth: sql`, `user: sa`, and `trust_server_certificate: true`.
- `OBJECT_ID(N'sales.Customer', N'U')` in the empty database is null.
- Doctor has seen `SELECT 1` succeed on this server.

- **Write the script only.** Run `sqlsc --json get-latest -t empty -o <evidence>/sql/get-latest.sql`. Exit 0, `"ok": true`, `"command"` is `get-latest`. The file contains `CREATE TABLE` and `Customer`. Query the empty database again: `sales.Customer` is still absent.
- **Apply.** Run `sqlsc --json get-latest -t empty`. Exit 0, `"ok": true`, `"applied": true`, `"failed"` is empty, `"transaction": true`, and `"still_different"` is empty. `OBJECT_ID(N'sales.Customer', N'U')` in the empty database is not null.
- **Clean status.** Run `sqlsc --json status -t empty --exit-code`. Exit 0 and `changes` is empty.

## Gotchas

- `--output` and `--dry-run` both skip the apply. Proof that they skipped is the query, not the flag name. The file still has to contain the `CREATE TABLE`.
- The empty database must be a different target name. Reusing `dev` reuses `.sqlsc/sync/dev.json` from the source database and get-latest will not treat the repo as the side to apply.
- `get-latest` on a target named `prod`, `production`, `prd`, or `live` exits 2 and changes nothing. The harness uses `empty`.
- The source database's committed files are the input. Do not edit `Customer.sql` by hand in this proof; the apply has to be the file `script` wrote.
