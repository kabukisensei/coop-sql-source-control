# Using coop-sql-source-control from coop-agent

coop-agent calls standalone tools by shelling out and reading JSON, the way its
`data_doc` tool drives `coop-data-doc`. This tool is built for the same pattern.

## What the tool already guarantees

- `--json` on every command prints exactly one JSON document on stdout.
  Success is `{"ok": true, "command": …}`; a problem the user must fix is
  `{"ok": false, "error": {"kind": "config" | "connect" | "read" | "locks" | …, "message": …}}`
  with exit code 2. The message is written for a person and says what to do.
- No command prompts, so nothing hangs under the agent.
- No password, token or connection string appears in any output. Target
  summaries carry only name, kind, server, database, sign-in mode and source.
- Targets come from the same `.coop/project.yml` `sql_targets` the agent
  already uses, so `dev`, `test` and `prod` mean the same thing in both.
- `status`, `diff` and `targets` only read the catalog. `script` writes files in
  the repo's scripts folder and nothing else; it never runs DDL. `commit` adds a
  git commit of exactly those files. `get-latest` and `deploy` are the only commands
  that change a database's objects or rows. `get-latest` never touches a
  protected target (prod); `deploy` changes one only with `--confirm <target>`,
  which the agent passes only after the user approves that release. `--dry-run`
  and `--output` on either show the script without running it.
- `commit` also commits `sqlsc.yml`, `.gitattributes` and `.gitignore` while they
  are new or changed (listed as `setup_files`), so the first baseline commit links
  the repo without a shell `git commit`.
- `schemas` / `exclude_schemas` in `sqlsc.yml` (written by `init --schema` /
  `--exclude-schema`) narrow every command to part of the database.
- `layout` in `sqlsc.yml` (written by `init --layout`) is `type` (sqlsc's own
  `Tables/dbo.Customer.sql`) or `schema` (`dbo/Tables/Customer.sql`, what SSMS's
  Create Project from Database writes); with no key the files decide. Every answer's
  header carries `layout`. Files in the other layout make `status` warn and every
  writing command refuse with `"error": {"kind": "layout"}` before touching anything,
  so an agent should pass the warning on rather than script around it.
- `data add` and `data remove` only write or delete files under `Data/`.
- `commit` and `checkin` fast-forward the branch onto its upstream before they read
  anything (`--no-pull` skips it) and refuse, with `"error": {"kind": "git"}`, on a
  detached HEAD, a merge in progress, unresolved conflicts, a missing git identity or
  a branch that cannot fast-forward; the answer's `git` block says what was pulled.
  coop-agent's own fetch-and-fast-forward before `sql_source commit` stays: the two
  agree, and sqlsc's check also covers the SSMS window and the shell.
- `commit --prepared-by "coop-agent <version>"` (or the `<agent>:<person>` form of
  `--as` alone) puts a `Prepared-by:` trailer on the commit, so git history says which
  commits the agent prepared while the person stays the author; the answer lists it
  under `prepared_by`. A person's later `checkin` of the agent's checkout adds the
  same trailer on its own.
- `checkout`, `checkin`, `unlock` and `guard` write only to the `sqlsc` schema
  (the lock table and the guard trigger); `checkin` also makes a git commit.
  A checkout that clashes returns `"ok": false` with the owner in `conflicts`
  and exit code 1.

## Wiring it into coop-agent (changes in the coop-agent repo, not done yet)

1. Pin it in `config/release-manifest.json` under `python_tools` so
   `coop install` and `coop update` install it with pipx, and add the matching
   check to `scripts/doctor.ps1`.
2. Add a `sql_source` tool to `extensions/coop-tools/index.ts` that runs
   `coop-sqlsc --json <status|diff> [--target <env>] [objects…]` and renders
   the changes into the tool's `content` (object, action, and for `diff` the
   unified diff, within the 12,000-character cap).
3. Treat `script` as a write, `commit` and `checkin` as source commits (which coop
   leaves to a person), and `get-latest` as a database change:
   under coop's guardrails each runs only after the user approves, and the agent
   shows `get-latest --dry-run` before running it.
4. Before the agent edits an object it runs
   `coop-sqlsc --json checkout <object> --as coop-agent:<user> -m "<task>"`, and
   stops to tell the user if `conflicts` is not empty. After an approved dev change
   it runs `script`, shows the diff, then `commit --as coop-agent:<user> -m "…"`
   (one approval per commit, never `--push` or `--force`) and `unlock --as
   coop-agent:<user>` for what it committed; pushing stays a person's step. A
   checkout held by `coop-agent:<user>` counts as that user's own (`mine` is true
   for them), so the person can also check it in themselves (from the SSMS add-in
   or `sqlsc checkin`). Naming the agent in `--as` keeps it from taking the
   person's own checkouts, and keeps its checkouts apart from everyone else's. Pass
   `--prepared-by "coop-agent <coop version>"` on `commit` so the history names the
   agent and its version.

## Example

```
$ coop-sqlsc --json status --target dev
{
  "ok": true,
  "command": "status",
  "target": {"name": "dev", "kind": "azure_sql", "server": "…", "database": "Sales", "auth": "entra", "source": "coop project.yml"},
  "folder": "C:\\work\\sales\\database",
  "summary": {"new": 1, "changed": 0, "deleted": 0},
  "sides": {"to_commit": 1, "to_get": 0, "conflicts": 0, "unknown_side": 0},
  "user": "ada@example.com",
  "changes": [{"action": "new", "origin": "database", "type": "view", "schema": "dbo", "name": "vSales", "object": "[dbo].[vSales]", "path": "Views/dbo.vSales.sql", "locked_by": null, "mine": false}],
  "locks": [],
  "warnings": []
}
```

`origin` says which side moved since the repo and this database last agreed
(sqlsc keeps that record itself, per target, under `.sqlsc/`): `database`
(commit it), `repo` (apply it with `get-latest`), `both` (changed on both
sides: show the diff and ask), or `unknown` (never seen in sync). `commit`
leaves `repo` and `both` objects alone and lists them under `held_back`;
`get-latest` leaves `database` and `both` objects alone and lists them under
`kept`. Each entry carries a `reason`. An object dropped on one side and
edited on the other is `both`, never a plain deletion. `checkin` keeps its
`held_back` objects checked out and answers `ok: false`. `--force` on either command overrides
that, so only pass it when the person has chosen a side.

`user` is who sqlsc ran as (git `user.email`, or `--as`); `locked_by` names
who has an object checked out and `mine` is true when that is this user, so
"checked out by you" needs no name comparison.

`status` also lists `migrations.pending`: hand-written scripts under
`Migrations/` that this database has not run yet (see the README). A whole
`get-latest` or `deploy` runs them and lists them under `migrations`; a
`get-latest` of named objects does not. `sqlsc --json migrations` gives every
script with its `state` (`pending`, `applied`, `edited`, or `unknown` when
`sqlsc.Migration` could not be read: then `status` says
`migrations.history_unknown: true` and a whole update fails with error kind
`history` before running anything), and
`sqlsc --json migrations add <name>` creates one and returns its `path`; the
agent then writes the SQL into that file and commits it with `commit`.
