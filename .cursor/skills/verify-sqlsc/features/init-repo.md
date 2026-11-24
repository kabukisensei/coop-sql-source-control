# Initialize a repo

Init links a git repo to a database by writing `sqlsc.yml` and the surrounding files. Targets, a new migration file, and a SQL project are written from that config. None of these four commands connects.

## Sub-features

- `init-write` creates the config and the repo files.
- `targets-list` lists the database named in the config.
- `init-twice` refuses to overwrite the config.
- `migration-add` creates a timestamped migration file.
- `sqlproj-write` creates the SQL project and ignores build output.

## How to get to it (user POV)

- In an empty git repo, run `sqlsc init` with `--kind`, `--server`, and `--database`.
- Run `sqlsc targets` or `coop-sqlsc targets`.
- Run `sqlsc init` again.
- Run `sqlsc migrations add verify-seed`.
- Run `sqlsc sqlproj`.

## Driving it with verify.sh

Preconditions:

- Doctor's version check has passed.
- The harness made a new git repo with a local identity. No SQL Server is running for this feature.
- The server in the init command is `203.0.113.10,1433` and the database name is `VerifyDb`.

- **Write the link.** Run `sqlsc --json init --kind sql_server --server 203.0.113.10,1433 --database VerifyDb --auth sql --user sa --trust-server-certificate --layout schema --folder database`. Exit 0, `"ok": true`, `"layout": "schema"`. The repo now has `sqlsc.yml`, `.gitattributes` containing `*.sql text diff`, `.gitignore` containing the line `.sqlsc/`, and `database/.editorconfig` containing `charset = utf-8`. `sqlsc.yml` names `VerifyDb`, `sa`, and `trust_server_certificate: true`, and it has no password.
- **List targets.** Run `sqlsc --json targets` and `coop-sqlsc --json targets`. Each exits 0 within a few seconds. The one target is `dev`, kind `sql_server`, auth `sql`, server `203.0.113.10,1433`, database `VerifyDb`. The JSON has no password.
- **Refuse a second init.** Run the same `sqlsc --json init` again without `--force`. Exit 2, `"ok": false`, `error.kind` is `config`, and the message says the file already exists. `sqlsc.yml` is unchanged.
- **Add a migration.** Run `sqlsc --json migrations add verify-seed`. Exit 0, `"command"` is `migrations add`, `"when"` is `after`. One file `database/Migrations/*_verify-seed.sql` exists and its first line is `-- sqlsc: after`.
- **Write the project.** Run `sqlsc --json sqlproj`. Exit 0, `"written": true`, `"path"` is `database/VerifyDb.sqlproj`. The file pins `Microsoft.Build.Sql/2.3.0` and the SQL Server platform. `.gitignore` gains `bin/`, `obj/`, and `.vs/`.

## Gotchas

- `sqlsc migrations` with no subcommand reads `sqlsc.Migration` and needs a database. The offline command is `migrations add`.
- `targets` on this config must return without a server. If it hangs, it tried to connect and the check failed.
- The password is not a flag and must not appear in `sqlsc.yml`. `--user sa` only records the login name.
- `init` from a directory that already has `sqlsc.yml` exits 2 unless `--force`. Do not pass `--force` to make a second init succeed.
