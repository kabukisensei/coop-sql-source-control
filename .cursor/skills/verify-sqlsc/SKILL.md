---
name: verify-sqlsc
description: "Drive the sqlsc CLI from coop-sql-source-control the way a person does: install from the checkout, check --version and --help, and replay init, targets, migrations add, sqlproj, status, diff, script, commit, and get-latest. Use before merging, or whenever a green build is not enough to prove the CLI. Offline checks need no SQL Server; --sql runs the core workflows against a real one."
---

# Verify sqlsc

`sqlsc` (and the same program as `coop-sqlsc`) is the product. This skill installs it from the checkout and replays real commands, then checks exit codes, JSON, and files. It does not call pytest and it does not import `coop_sqlsc.cli` in-process. The SSMS add-in shells out to this CLI; it is a Windows surface and this skill does not drive it.

Two runs, and only these two:

| Run | Command | SQL Server |
| --- | --- | --- |
| Offline | `./scripts/verify.sh` | Not used. Safe on a laptop with no database. |
| SQL Server | `./scripts/verify.sh --sql` | Required. Uses `SQLSC_TEST_SERVER` and `SQLSC_PASSWORD` when both are set (what CI sets). Otherwise starts Docker. |

Run both from the repo root. The offline run is the one to use when a review only has a minute. `--sql` is the one that proves scripting and applying. Do not treat a skipped SQL section as a pass of those workflows.

## Launch

`./scripts/verify.sh` creates a throwaway virtualenv (`python3 -m venv`) and runs `pip install <checkout>` into it, so the `sqlsc` and `coop-sqlsc` it drives are the console scripts from that install, not `python -m` and not an editable checkout on `PYTHONPATH`. On Debian and Ubuntu the venv module is the `python3-venv` package. Without it the run stops at `step: launch`.

There is no server to keep alive for the offline run. Each drive is a new git repo under `/tmp/sqlsc-verify-work-<run-id>/`. The script sets that repo's local `user.name`, `user.email`, `commit.gpgsign false`, and `core.hooksPath /dev/null` so the machine's git identity and hooks cannot change the result. It does not change global git config.

`--sql` reaches a usable database before any workflow command:

- When `SQLSC_TEST_SERVER` and `SQLSC_PASSWORD` are both set, those are the server. CI's `.github/workflows/ci.yml` sets `SQLSC_TEST_SERVER=localhost,1433` and `SQLSC_PASSWORD` to the `sa` password of its `mssql` service container. The script does not start a second server in that case.
- Otherwise it runs `docker run` of `mcr.microsoft.com/mssql/server:2022-latest` with `ACCEPT_EULA=Y`, `MSSQL_SA_PASSWORD=Sqlsc_Ci_Pw1!` (the CI password), port `1433:1433`, and the same `sqlcmd -C ... SELECT 1` health command as that workflow. It then exports `SQLSC_TEST_SERVER=localhost,1433` and `SQLSC_PASSWORD` for the rest of the process only.

Ready means the installed CLI's own driver can `SELECT 1` on `master` with `Encrypt=yes` and `TrustServerCertificate=yes`. A container that is merely up is not ready. The script waits up to about four minutes, and gives up if the container exits.

Offline init is pointed at `203.0.113.10,1433` (the documentation range). Nothing is listening there. That is deliberate: `targets`, `migrations add`, and `sqlproj` must succeed without a database.

## Doctor

The script runs this itself, before the workflows. If doctor fails it stops and the summary is `VERIFY FAIL`.

Offline, immediately after install:

- `sqlsc --version` and `coop-sqlsc --version` exit 0 and print `sqlsc, version <version>`, where `<version>` is `__version__` in `src/coop_sqlsc/__init__.py`. Both entry points print the `sqlsc` program name.
- `sqlsc --help`, `sqlsc -h`, and `coop-sqlsc --help` exit 0. Help names `sqlsc init`, `sqlsc status`, `sqlsc commit`, and `sqlsc get-latest`.
- `sqlsc not-a-command` exits 2 and its stderr contains `No such command`.

`--sql` also requires ODBC Driver 18 for SQL Server (`coop_sqlsc.connect.pick_driver` on `pyodbc.drivers()`). CI installs it in the `ODBC Driver 18` step of `.github/workflows/ci.yml`. Then `SELECT 1` against `master` as `sa` must return 1. The password is never written into `sqlsc.yml` and the script does not print it.

An agent who is about to drive one feature by hand, instead of the whole script, runs doctor the same way: version must match the checkout, and for a SQL feature `SELECT 1` must have succeeded since that server last restarted. Do not drive a server this run did not health-check.

## Drive

The harness is `scripts/verify.sh`. Prefer one full run over typing the commands yourself. The feature map is the list of what that run covers; a later pass can drive a single file when only that behavior changed.

- [CLI surface](features/cli-surface.md) — `--version`, `--help`, unknown command. Offline.
- [Initialize a repo](features/init-repo.md) — `init`, `targets`, `migrations add`, `sqlproj`. Offline.
- [Script a database](features/script-database.md) — `status`, `diff`, `script`, including `--dry-run`. Needs SQL Server.
- [Commit](features/commit-changes.md) — `commit` and the git result. Needs SQL Server.
- [Get latest](features/get-latest.md) — `--output` writes a script and does not apply it; a second run applies it to an empty database. Needs SQL Server.

Commands use `--json`. Assert the document and the exit code. `--json` still exits 1 for `status --exit-code` when something differs, and exits 2 for a problem the user has to fix (`"ok": false` and `error.kind`). Human text on stderr is not the contract, except the unknown-command check.

The SQL drive uses one source database that already has `sales.Customer`, scripts it into a `schema`-layout repo, commits, then applies that repo to a second empty database under a target named `empty`. The empty database is a different target so it does not share `.sqlsc/sync/dev.json`.

## Evidence

The script writes `/tmp/sqlsc-verify-evidence/<run-id>/` (override with `SQLSC_VERIFY_EVIDENCE`):

- `transcript.txt` — the run's stdout and stderr.
- `summary.txt` — the PASS or FAIL block.
- `pip.log` — the install.
- `offline/` — `sqlsc.yml`, `gitattributes`, `gitignore`, `editorconfig`, the migration file, `VerifyDb.sqlproj`.
- `sql/Customer.sql` and `sql/sales.sql` — files `script` wrote.
- `sql/get-latest.sql` — the script `get-latest --output` wrote, which must exist even though it was not executed.
- `sql/commit-subject.txt` and `sql/commit-files.txt` — what git recorded.
- `last-stdout.txt` and `last-stderr.txt` — the command that failed, when one did.

A proof is the command, its exit code, and the side effect. For `script`, the side effect is the `.sql` file. For `get-latest --output`, the side effect is the file on disk and `sales.Customer` still missing in the empty database. For `get-latest` itself, the side effect is `OBJECT_ID(N'sales.Customer', N'U')` becoming non-null. For `commit`, the side effect is `git log -1` and `git ls-tree`. Matching JSON alone is not enough when the command claims to write a file or change a database.

`script --dry-run` must not write `database/sales/Tables/Customer.sql`. On a fresh repo, where nothing matches yet, it also must not write `.sqlsc/sync/dev.json`: there is no agreed text to record. The real `script` writes the object files and then that sync record. Do not treat a dry-run's JSON `files` list as proof that anything was written.

## Cleanup

The script's exit trap, and only that trap, removes what the run started:

- Drops `sqlsc_verify_src_<id>` and `sqlsc_verify_empty_<id>` if they were created. It will not drop any other database.
- `docker rm -f` the container id this run's `docker run` printed. It does not stop a container it did not start, and it does not remove the image.
- Deletes `/tmp/sqlsc-verify-work-<run-id>/`, including the virtualenv and both temp git repos.

It does not delete the evidence directory. `--keep` leaves the work directory and the container in place for debugging; evidence is still kept. Do not `pkill` `sqlservr` or `docker` by name.

After a run, confirm the work directory is gone (`work_removed: yes` in the summary, and the path is absent) and that `summary.txt` plus, for `--sql`, `sql/get-latest.sql` are still under the evidence path.

## Helpers

The only helper is the script. It is executable.

```bash
./scripts/verify.sh
./scripts/verify.sh --sql
./scripts/verify.sh --help
```

`./scripts/verify.sh --sql` is the CI-shaped invocation: in GitHub Actions the service container is already healthy and the two `SQLSC_*` variables are set, so the script does not call Docker. Locally, with those variables unset, the same command starts the container.

ODBC Driver 18 is a machine prerequisite for `--sql`, not something the script installs. Without it the SQL section fails at doctor and names the CI step that installs the driver.

When commands or their files change, run `/maintain-verification-skill` so this map stays aligned with the CLI. Do not edit the product to make a verification run pass.

## Result

End every run by reading the script's last lines. They are the result. Do not summarize them into a softer sentence.

Success:

```
VERIFY PASS
offline: pass
sql: not-run
step: -
detail: -
evidence: /tmp/sqlsc-verify-evidence/<run-id>
work: /tmp/sqlsc-verify-work-<run-id>
work_removed: yes
seconds: <n>
```

`--sql` success is the same with `sql: pass`. Anything else is a failure, including `VERIFY FAIL`, a missing summary, or `sql: not-run` when the task was to prove the SQL workflows. `step` and `detail` name the check that stopped the run.
