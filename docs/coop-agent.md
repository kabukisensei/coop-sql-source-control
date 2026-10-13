# Using coop-sql-source-control from coop-agent

coop-agent calls standalone tools by shelling out and reading JSON, the way its
`data_doc` tool drives `coop-data-doc`. This tool is built for the same pattern.

## What the tool already guarantees

- `--json` on every command prints exactly one JSON document on stdout.
  Success is `{"ok": true, "command": …}`; a problem the user must fix is
  `{"ok": false, "error": {"kind": "config" | "connect" | "read", "message": …}}`
  with exit code 2. The message is written for a person and says what to do.
- No command prompts, so nothing hangs under the agent.
- No password, token or connection string appears in any output. Target
  summaries carry only name, kind, server, database, sign-in mode and source.
- Targets come from the same `.coop/project.yml` `sql_targets` the agent
  already uses, so `dev`, `test` and `prod` mean the same thing in both.
- `status`, `diff` and `targets` only read the catalog. `script` writes files in
  the repo's scripts folder and nothing else; it never runs DDL.

## Wiring it into coop-agent (changes in the coop-agent repo, not done yet)

1. Pin it in `config/release-manifest.json` under `python_tools` so
   `coop install` and `coop update` install it with pipx, and add the matching
   check to `scripts/doctor.ps1`.
2. Add a `sql_source` tool to `extensions/coop-tools/index.ts` that runs
   `coop-sqlsc --json <status|diff> [--target <env>] [objects…]` and renders
   the changes into the tool's `content` (object, action, and for `diff` the
   unified diff, within the 12,000-character cap).
3. Treat `script` as a write: under coop's guardrails it runs only after the
   user approves, and the agent shows `git diff` before anything is committed.
4. Later phases add `checkout` / `checkin`; the agent should check out an
   object before editing it, so two people (or a person and the agent) never
   change the same object at once.

## Example

```
$ coop-sqlsc --json status --target dev
{
  "ok": true,
  "command": "status",
  "target": {"name": "dev", "kind": "azure_sql", "server": "…", "database": "Sales", "auth": "entra", "source": "coop project.yml"},
  "folder": "C:\\work\\sales\\database",
  "summary": {"new": 1, "changed": 0, "deleted": 0},
  "changes": [{"action": "new", "type": "view", "schema": "dbo", "name": "vSales", "object": "[dbo].[vSales]", "path": "Views/dbo.vSales.sql"}],
  "warnings": []
}
```
