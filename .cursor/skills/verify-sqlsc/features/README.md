# sqlsc verification map

This directory is the maintained source for verifying the user-facing behavior of the `sqlsc` CLI. Read the index before driving the app, then use the matching feature file as the recipe. The harness is `scripts/verify.sh`.

## Baseline preconditions

- Python 3.10 to 3.13, with the `venv` module, and git are on `PATH`. Debian and Ubuntu ship that module in `python3-venv` (`python3.12-venv`). GitHub Actions `setup-python` already includes it.
- The checkout is the repo under test. The harness installs it with `pip install` into a fresh virtualenv.
- Offline features need no SQL Server and no ODBC driver.
- SQL features need ODBC Driver 18 for SQL Server, and either `SQLSC_TEST_SERVER` plus `SQLSC_PASSWORD` or Docker.
- The harness creates its own temp git repos. Do not point it at a user's checkout or a shared dev database.
- Run `./scripts/verify.sh` (offline) or `./scripts/verify.sh --sql` (offline plus SQL Server). One `--sql` Docker launch owns host port 1433. A second launch must set `SQLSC_TEST_SERVER` or wait.

## Driving conventions

- Start from the harness, not from a `sqlsc` already on `PATH`.
- Treat every command in a feature file as literal. Keep the flags.
- JSON goes to stdout. Assert `"ok"` and the exit code together.
- A command that writes a file is proved by the file. A command that changes a database is proved by a query afterward.
- `script --dry-run` connects and lists the files it would write. It does not write those object files, and on a fresh repo it does not write `.sqlsc/sync/dev.json`. The real `script` writes both. Check both.
- Leave `/tmp/sqlsc-verify-evidence/<run-id>` in place. Cleanup removes temp repos, databases this run created, and a container this run started.

## Proof and skip reporting

- Capture the command, the exit code, and the resulting file or database state.
- Record which feature file the command came from.
- `sql: not-run` means the SQL features were not exercised. Do not report them as verified.
- An unreachable SQL path is `VERIFY FAIL` at `sql-odbc` or `sql-docker` with the unmet prerequisite. Do not switch the workflow to an in-process `CliRunner` call to make it pass.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then uses exactly four H2 sections in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with verify.sh` starts with `Preconditions:` and uses labeled bullets that pair each user action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

Keep implementation details out of the map. Name only user paths, stable handles, required state, commands, and observable proof.

## Features

- [CLI surface](./cli-surface.md) covers `--version`, `--help`, and an unknown command. No SQL Server.
- [Initialize a repo](./init-repo.md) covers `init`, `targets`, `migrations add`, and `sqlproj`. No SQL Server.
- [Script a database](./script-database.md) covers `status`, `diff`, and `script` against a live database.
- [Commit](./commit-changes.md) covers committing the scripted files and the clean status that follows.
- [Get latest](./get-latest.md) covers writing the apply script without running it, then applying it to an empty database.
