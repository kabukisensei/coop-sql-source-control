# Commit

Commit takes the files script just wrote, plus the new `sqlsc.yml` and its neighbors, and makes one git commit. After that, status is clean.

## Sub-features

- `commit-files` commits the scripted table, the schema, and the setup files.
- `status-clean` reports no differences once those files are committed.

## How to get to it (user POV)

- Run `sqlsc commit -m "Script sales.Customer"`.
- Run `sqlsc status` to see that the repo matches the database.

## Driving it with verify.sh

Preconditions:

- The script-database feature has just written `database/sales/Tables/Customer.sql` and `database/Security/sales.sql` in this temp repo.
- The repo has a local git identity. `--no-pull` is required so a missing upstream is not a fetch.

- **Commit.** Run `sqlsc --json commit --no-pull -m "Script sales.Customer"`. Exit 0, `"ok": true`, `"commit"` is a non-empty short hash, and `setup_files` includes `sqlsc.yml`. `git log -1 --format=%s` is `Script sales.Customer`. `git ls-tree -r --name-only HEAD` includes `sqlsc.yml`, `.gitattributes`, `.gitignore`, `database/.editorconfig`, `database/sales/Tables/Customer.sql`, and `database/Security/sales.sql`.
- **Clean status.** Run `sqlsc --json status --exit-code`. Exit 0, `"ok": true`, and `changes` is empty.

## Gotchas

- The temp repo sets `commit.gpgsign` to false and `core.hooksPath` to `/dev/null`. A signing prompt or a global hook would make the run wait or fail for a reason that is not sqlsc.
- `--no-pull` is part of the command. Without it, a repo that has an upstream will try to fetch.
- Status stays exit 1 after `script` until this commit. A clean status before the commit is a failed proof.
- The harness does not pass `--push`. A commit that pushed somewhere is the wrong command.
