"""The `sqlsc` command line.

Every command prints a readable summary for people, and the same facts as one
JSON document with `--json` for coop-agent, CI and the SSMS add-in. No command
ever prompts, so all of them run unattended.

Exit codes: 0 done, 1 differences found (only with --exit-code), 2 a problem the
user has to fix (config, sign-in, connection).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table as RichTable

from . import __version__, git_ops, locks
from . import config as config_mod
from . import data as data_mod
from . import migrations as migrations_mod
from .catalog import platform_schemas, read_database
from .compare import CHANGED, DELETED, NEW, ORIGIN_WORDS, Change, comparable, compare, matches, summarize
from .config import KINDS, ConfigError
from .connect import ConnectError, open_connection
from .deploy import GUARDED_KINDS, Plan, apply, build_plan, migration_steps
from .layout import delete_object, key_for_path, objects_from_files, read_folder, relative_path, write_object
from .locks import Lock, LockError, LockStore
from .migrations import BEFORE, Applied, Migration
from .objects import OBJECT_TYPES, TYPE_LABELS, ObjectKey
from .sync import BOTH, DATABASE, REPO, UNKNOWN, SyncRecord

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

ACTION_STYLE = {NEW: "green", CHANGED: "yellow", DELETED: "red"}
ACTION_WORDS = {NEW: "New", CHANGED: "Changed", DELETED: "Deleted"}


@dataclass
class Opts:
    as_json: bool
    config_path: Path | None


def emit(opts: Opts, payload: dict[str, Any]) -> None:
    if opts.as_json:
        sys.stdout.write(json.dumps({"ok": True, **payload}, indent=2) + "\n")


def fail(opts: Opts, kind: str, message: str) -> None:
    if opts.as_json:
        sys.stdout.write(json.dumps({"ok": False, "error": {"kind": kind, "message": message}}, indent=2) + "\n")
    else:
        err_console.print(f"[bold red]✗[/] {escape(message)}")
    sys.exit(2)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--json", "as_json", is_flag=True, help="Print one JSON document instead of text (for agents and CI).")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Use this sqlsc.yml instead of searching up from the current folder.",
)
@click.version_option(__version__, prog_name="sqlsc")
@click.pass_context
def main(ctx: click.Context, as_json: bool, config_path: Path | None) -> None:
    """Source control for SQL Server, Azure SQL and Fabric databases.

    \b
    Start here:
      sqlsc init --target dev --kind azure_sql --server myserver.database.windows.net --database Sales
      sqlsc status         what differs between the database and the repo
      sqlsc diff dbo.Foo   the text of one difference
      sqlsc script         write the database's objects into the repo folder
      sqlsc commit -m ...  script the changes and commit just those files
      sqlsc get-latest     update the database to match the repo
      sqlsc checkout X     tell the team you are editing X
      sqlsc checkin        commit what you checked out and release it
    """
    ctx.obj = Opts(as_json=as_json, config_path=config_path)


# --- init / targets --------------------------------------------------------------


@main.command()
@click.option(
    "--folder", default="database", show_default=True, help="Folder for object scripts, relative to the repo."
)
@click.option("--target", "target_name", default="dev", show_default=True, help="Name of the default target.")
@click.option("--kind", type=click.Choice(list(KINDS)), help="Kind of database (skip to use coop's sql_targets).")
@click.option("--server", help="Server host, e.g. myserver.database.windows.net or localhost,1433.")
@click.option("--database", help="Database, warehouse or lakehouse name.")
@click.option("--auth", type=click.Choice(config_mod.AUTH_MODES), help="How to sign in (default depends on kind).")
@click.option("--user", help="User name for SQL authentication.")
@click.option("--trust-server-certificate", is_flag=True, help="Accept a self-signed certificate (local dev only).")
@click.option("--force", is_flag=True, help="Overwrite an existing sqlsc.yml.")
@click.pass_obj
def init(
    opts: Opts,
    folder: str,
    target_name: str,
    kind: str | None,
    server: str | None,
    database: str | None,
    auth: str | None,
    user: str | None,
    trust_server_certificate: bool,
    force: bool,
) -> None:
    """Link this repo to a database by writing sqlsc.yml."""
    path = (opts.config_path or Path.cwd() / config_mod.CONFIG_NAME).resolve()
    if path.exists() and not force:
        fail(opts, "config", f"{path} already exists. Use --force to replace it.")
    entry: dict[str, Any] | None = None
    if kind or server or database:
        entry = {"kind": kind, "server": server, "database": database, "auth": auth, "user": user}
        if trust_server_certificate:
            entry["trust_server_certificate"] = True
        try:
            config_mod.build_target(target_name, entry)
        except ConfigError as exc:
            fail(opts, "config", str(exc))
    else:
        coop = config_mod.find_coop_contract(path.parent)
        if coop is None:
            fail(
                opts,
                "config",
                "Name the database with --kind, --server and --database "
                "(or run this inside a repo that has coop's .coop/project.yml).",
            )
    config_mod.write_new(path, folder=folder, target=target_name, target_entry=entry)
    attributes = path.parent / ".gitattributes"
    if not attributes.exists():
        attributes.write_text("*.sql text diff\n", encoding="utf-8")
    _ignore_sync_dir(path.parent)
    emit(opts, {"command": "init", "config": str(path), "folder": folder, "target": target_name})
    if not opts.as_json:
        console.print(f"[green]✓[/] Wrote {escape(str(path))}")
        console.print(f"  Object scripts will live in [bold]{escape(folder)}/[/]. Next: [bold]sqlsc status[/]")


@main.command()
@click.pass_obj
def targets(opts: Opts) -> None:
    """List the databases this repo can talk to."""
    cfg = _load_config(opts)
    rows: list[dict[str, Any]] = []
    for name in cfg.target_names():
        try:
            rows.append({**cfg.target(name).summary(), "default": name == cfg.default_target, "problem": None})
        except ConfigError as exc:
            rows.append({"name": name, "default": name == cfg.default_target, "problem": str(exc)})
    emit(opts, {"command": "targets", "config": str(cfg.path), "targets": rows})
    if opts.as_json:
        return
    if not rows:
        console.print("No targets yet. Add one under `targets:` in sqlsc.yml, or run `sqlsc init`.")
        return
    table = RichTable(show_header=True, header_style="bold", box=None)
    for col in ("", "Target", "Kind", "Server", "Database", "Sign-in"):
        table.add_column(col)
    for row in rows:
        mark = "*" if row["default"] else ""
        if row.get("problem"):
            table.add_row(mark, row["name"], f"[red]{escape(row['problem'])}[/]", "", "", "")
        else:
            table.add_row(mark, row["name"], KINDS[row["kind"]]["label"], row["server"], row["database"], row["auth"])
    console.print(table)


# --- status / diff / script -------------------------------------------------------


AS_OPTION = click.option(
    "--as",
    "as_user",
    envvar="SQLSC_USER",
    help="Who you are for checkouts (default: git user.email; env SQLSC_USER).",
)


@main.command()
@click.option("--target", "-t", "target_name", help="Target to compare (default from sqlsc.yml).")
@click.option("--exit-code", is_flag=True, help="Exit 1 when anything differs (for CI).")
@AS_OPTION
@click.pass_obj
def status(opts: Opts, target_name: str | None, exit_code: bool, as_user: str | None) -> None:
    """Show which objects differ between the database and the repo, and who has them checked out."""
    state = _read_state(opts, target_name)
    changes = state.changes()
    counts = summarize(changes)
    sides = _sides(changes)
    held, who = _peek_locks(state, as_user)
    pending = state.pending_migrations()
    emit(
        opts,
        {
            "command": "status",
            **state.header(),
            "summary": counts,
            "sides": sides,
            "user": who,
            "changes": [
                {
                    **c.to_json(),
                    "locked_by": _owner(held, c.key),
                    "mine": c.key in held and _same_user(held[c.key].locked_by, who),
                }
                for c in changes
            ],
            "locks": [{**lock.to_json(), "mine": _same_user(lock.locked_by, who)} for lock in held.values()],
            "migrations": {
                "pending": [m.to_json() for m in pending],
                "edited": [m.to_json() for m in migrations_mod.edited(state.migrations, state.applied)],
            },
            "warnings": state.warnings,
        },
    )
    if not opts.as_json:
        _print_header(state)
        if not changes:
            console.print(f"[green]✓[/] The repo matches the database ({len(state.db)} objects).")
        else:
            _print_changes(changes, held, who)
            console.print()
            console.print(
                f"{counts[NEW]} new, {counts[CHANGED]} changed, {counts[DELETED]} deleted: " + _sides_text(sides)
            )
            console.print("Run [bold]sqlsc diff <object>[/] to see one.")
        if pending:
            console.print(
                f"{len(pending)} migration script(s) have not run on '{escape(state.target.name)}' yet; "
                "[bold]sqlsc get-latest[/] runs them."
            )
        mine = [lock for lock in held.values() if _same_user(lock.locked_by, who)]
        if mine:
            console.print(
                f"You have {len(mine)} object(s) checked out; [bold]sqlsc checkin[/] commits and releases them."
            )
        _print_warnings(state.warnings)
    if exit_code and (changes or pending):
        sys.exit(1)


@main.command()
@click.argument("objects", nargs=-1)
@click.option("--target", "-t", "target_name", help="Target to compare (default from sqlsc.yml).")
@click.option("--context", default=3, show_default=True, help="Lines of context around each change.")
@click.pass_obj
def diff(opts: Opts, objects: tuple[str, ...], target_name: str | None, context: int) -> None:
    """Show the text differences, for all objects or the ones named (e.g. dbo.Customer)."""
    state = _read_state(opts, target_name)
    changes = _filter(state.changes(), objects)
    items = [{**c.to_json(), "diff": c.unified_diff(context=context)} for c in changes]
    emit(opts, {"command": "diff", **state.header(), "changes": items, "warnings": state.warnings})
    if opts.as_json:
        return
    if not changes:
        console.print("[green]✓[/] No differences" + (f" for {', '.join(objects)}." if objects else "."))
        return
    for change, item in zip(changes, items, strict=True):
        style = ACTION_STYLE[change.action]
        side = f" [dim]({_side_note(change)})[/]" if change.origin != UNKNOWN else ""
        console.rule(f"[{style}]{ACTION_WORDS[change.action]}[/] {escape(change.key.display)}{side}", align="left")
        console.print(Syntax(item["diff"], "diff", theme="ansi_dark", background_color="default", word_wrap=True))
    _print_warnings(state.warnings)


@main.command()
@click.argument("objects", nargs=-1)
@click.option("--target", "-t", "target_name", help="Target to read (default from sqlsc.yml).")
@click.option("--dry-run", is_flag=True, help="Show what would be written without touching files.")
@click.pass_obj
def script(opts: Opts, objects: tuple[str, ...], target_name: str | None, dry_run: bool) -> None:
    """Write the database's objects into the repo folder (new, changed and deleted files)."""
    state = _read_state(opts, target_name)
    changes = _filter(state.changes(), objects)
    written: list[dict[str, str]] = []
    for change in changes:
        if not dry_run:
            _write_from_database(state, change)
        written.append(change.to_json())
    state.sync.save()
    emit(
        opts,
        {
            "command": "script",
            **state.header(),
            "dry_run": dry_run,
            "summary": summarize(changes),
            "files": written,
            "warnings": state.warnings,
        },
    )
    if opts.as_json:
        return
    _print_header(state)
    if not changes:
        console.print("[green]✓[/] Nothing to write; the repo already matches the database.")
    else:
        verb = "Would write" if dry_run else "Wrote"
        for change in changes:
            style = ACTION_STYLE[change.action]
            what = "delete" if change.action == DELETED else ACTION_WORDS[change.action].lower()
            console.print(f"  [{style}]{what:>7}[/]  {escape(change.path)}")
        console.print()
        console.print(
            f"[green]✓[/] {verb} {len(changes)} file(s) in {escape(str(state.scripts_dir))}."
            + ("" if dry_run else " Review with `git diff`, then commit.")
        )
    _print_warnings(state.warnings)


@main.command()
@click.argument("objects", nargs=-1)
@click.option("--target", "-t", "target_name", help="Target to read (default from sqlsc.yml).")
@click.option("--message", "-m", help="Commit message (default lists the objects).")
@click.option("--push", is_flag=True, help="Push the branch after committing.")
@click.option("--force", is_flag=True, help="Also commit objects someone else has checked out.")
@AS_OPTION
@click.pass_obj
def commit(
    opts: Opts,
    objects: tuple[str, ...],
    target_name: str | None,
    message: str | None,
    push: bool,
    force: bool,
    as_user: str | None,
) -> None:
    """Script the database's changes into the repo and commit just those files.

    Objects someone else has checked out are left for them to check in, unless --force.
    So are objects the repo changed since your last sync (a teammate's commit you have
    not applied yet): committing them would overwrite that work with the database's
    older version. Run `sqlsc get-latest` for those, or --force to commit them anyway.
    """
    state = _read_state(opts, target_name)
    differences = state.changes()
    changes = _filter(differences + _uncommitted(state, {c.key for c in differences}), objects)
    held, who = _peek_locks(state, as_user)
    skipped: list[Change] = []
    held_back: list[Change] = []
    if not force:
        skipped = [c for c in changes if c.key in held and not _same_user(held[c.key].locked_by, who)]
        held_back = [c for c in changes if c not in skipped and not c.from_database]
        changes = [c for c in changes if c not in skipped and c not in held_back]
    scripts = _uncommitted_migrations(state)
    sha, branch = _commit_changes(opts, state, changes, message, push, scripts)
    emit(
        opts,
        {
            "command": "commit",
            "migrations": scripts,
            **state.header(),
            "commit": sha,
            "pushed": branch,
            "push_error": state.push_error,
            "summary": summarize(changes),
            "files": [c.to_json() for c in changes],
            "skipped": [{**c.to_json(), "locked_by": held[c.key].locked_by} for c in skipped],
            "held_back": [{**c.to_json(), "reason": _hold_reason(c)} for c in held_back],
            "warnings": state.warnings,
        },
    )
    if opts.as_json:
        return
    _print_header(state)
    if not sha:
        console.print("[green]✓[/] Nothing to commit; the repo already matches the database.")
    else:
        _print_changes(changes)
        console.print()
        scripted = f" and {len(scripts)} migration script(s)" if scripts else ""
        console.print(
            f"[green]✓[/] Committed {len(changes)} object(s){scripted} as [bold]{sha}[/]"
            + (f" and pushed {escape(branch)}." if branch else ".")
        )
    for change in skipped:
        console.print(
            f"[yellow]![/] Left {escape(change.key.display)}: checked out by {escape(held[change.key].locked_by)}"
            " (--force commits it anyway)."
        )
    for change in held_back:
        console.print(f"[yellow]![/] Left {escape(change.key.display)}: {escape(_hold_reason(change))}")
    _print_warnings(state.warnings)


def _commit_changes(
    opts: Opts,
    state: State,
    changes: list[Change],
    message: str | None,
    push: bool,
    scripts: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """Writes the changed object files and commits exactly those, plus the migration
    scripts named in `scripts` (paths relative to the scripts folder). Returns (short sha, pushed branch)."""
    if not changes and not scripts:
        return None, None
    try:
        root = git_ops.repo_root(state.config.root)
    except git_ops.GitError as exc:
        fail(opts, "git", str(exc))
        raise
    paths: list[Path] = []
    for change in changes:
        if change.key in state.uncommitted:
            paths.append(state.scripts_dir / relative_path(change.key))  # already right on disk; commit as it is
        else:
            paths.append(_write_from_database(state, change))
    paths += [state.scripts_dir / rel for rel in scripts or []]
    state.sync.save()
    try:
        sha = git_ops.commit_paths(root, paths, message or _default_message(state, changes, scripts or []))
    except git_ops.GitError as exc:
        fail(opts, "git", str(exc))
        raise
    branch = None
    if sha and push:
        # The commit stands whatever the push does: failing here would report the
        # commit as failed, and checkin would keep the objects checked out.
        try:
            branch = git_ops.push(root)
        except git_ops.GitError as exc:
            state.push_error = f"Committed as {sha}, but the push failed: {exc}. Push with `git push` once fixed."
            state.warnings.append(state.push_error)
    return sha, branch


@main.command("get-latest")
@click.argument("objects", nargs=-1)
@click.option("--target", "-t", "target_name", help="Database to update (default from sqlsc.yml).")
@click.option("--dry-run", is_flag=True, help="Print the script instead of running it.")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Write the script to a file instead of running it.",
)
@click.option(
    "--allow-drops", is_flag=True, help="Also drop objects and columns that are in the database but not the repo."
)
@click.option(
    "--force",
    is_flag=True,
    help="Also overwrite objects changed in the database since your last sync (uncommitted work).",
)
@click.pass_obj
def get_latest(
    opts: Opts,
    objects: tuple[str, ...],
    target_name: str | None,
    dry_run: bool,
    output: Path | None,
    allow_drops: bool,
    force: bool,
) -> None:
    """Update the database to match the repo (like Redgate's Get latest).

    Objects changed in the database since your last sync are left alone, since
    applying the repo's version would wipe uncommitted work: commit them first,
    or --force to overwrite them.
    """
    state = _read_state(opts, target_name)
    if not (dry_run or output is not None) and state.target.protected:
        protected_message = (
            f"'{state.target.name}' is protected, so get-latest will not change it. Use `sqlsc deploy` for a "
            "release, --output to write the script for review, or set `protected: false` on the target."
        )
    else:
        protected_message = None
    run_update(
        opts,
        state,
        objects=objects,
        command="get-latest",
        dry_run=dry_run,
        output=output,
        allow_drops=allow_drops,
        force=force,
        blocked=protected_message,
    )


def run_update(
    opts: Opts,
    state: State,
    *,
    objects: tuple[str, ...],
    command: str,
    dry_run: bool,
    output: Path | None,
    allow_drops: bool,
    force: bool = True,
    blocked: str | None = None,
    header: list[str] | None = None,
    footer: Callable[[Plan], list[str]] | None = None,
    extra: dict[str, Any] | None = None,
    who: str | None = None,
    commit: str | None = None,
) -> None:
    """Plans, writes and (unless only a script was asked for) runs the changes that
    make state.target match state.folder. Shared by get-latest and deploy. Without
    `force`, objects the database changed since the last sync are kept as they are.

    Migration scripts the target has not run yet go with a whole update (no object
    names given): the "before" ones first, after which the database is read again so
    the structural changes build on what they did; the "after" ones at the end."""
    only_script = dry_run or output is not None
    who = who or locks.current_user(state.config.root)
    recorded = False  # a footer run on its own, with no changes (a no-op deploy)
    pending = state.pending_migrations() if not objects else []
    before = migration_steps([m for m in pending if m.when == BEFORE], who, commit)
    after = migration_steps([m for m in pending if m.when != BEFORE], who, commit)
    if blocked:
        preview = build_plan(_filter(state.changes(), objects), allow_drops=allow_drops)
        if before or after or preview.runnable:
            fail(opts, "protected", blocked)

    result = None
    if before and not only_script:
        result = _run_before_migrations(opts, state, Plan(before))
        if result.failed:
            before = []  # nothing else ran; report the failure below
    changes = _filter(state.changes(), objects)
    kept: list[Change] = [] if force else [c for c in changes if not c.from_repo]
    changes = [c for c in changes if c not in kept]
    plan = build_plan(
        changes,
        allow_drops=allow_drops,
        enforced_keys=state.target.caps["enforced_keys"],
        guard_drops=state.target.kind in GUARDED_KINDS,
    )
    if not (result and result.failed):
        plan.steps += (before if only_script else []) + after
    closing = footer(plan) if footer else []  # a deploy records itself even with nothing to change
    structural = [s for s in plan.runnable if s.action != "migrate"]
    if before and only_script and structural:
        header = [
            *(header or plan.script(state.target).splitlines()[:2]),
            f"-- {len(before)} migration script(s) run first. The structural changes below were worked out",
            "-- before they ran; if a script changes structure too, run the update with sqlsc instead,",
            "-- which reads the database again in between.",
        ]
    script_text = plan.script(state.target, header, closing)
    if output is not None:
        output.write_text(script_text, encoding="utf-8")

    remaining: list[Change] = []
    if not only_script and plan.runnable and not (result and result.failed):
        spinner = (
            nullcontext()
            if opts.as_json
            else err_console.status(f"Updating {state.target.label} '{state.target.name}'…")
        )
        try:
            with spinner:
                conn = open_connection(state.target)
                try:
                    done_before = result.done if result else []
                    result = apply(conn, plan, state.target)
                    result.done = done_before + result.done
                    if closing and not result.failed:
                        cur = conn.cursor()
                        for statement in closing:
                            cur.execute(statement)
                    after = read_database(conn, state.target).objects
                    tracked = [k for k in state.folder if k.type == "data"]
                    _read_static_data(conn, state.target, tracked, after)
                finally:
                    conn.close()
        except (ConfigError, ConnectError) as exc:
            fail(opts, "connect", str(exc))
            raise
        remaining = _filter(compare(after, state.folder, state.sync), objects)
        state.sync.save()
    elif not only_script and closing and not blocked and not (result and result.failed):
        # Nothing to change, but a release still goes in the history.
        try:
            conn = open_connection(state.target)
            try:
                cur = conn.cursor()
                for statement in closing:
                    cur.execute(statement)
            finally:
                conn.close()
        except (ConfigError, ConnectError) as exc:
            fail(opts, "connect", str(exc))
            raise
        recorded = True

    payload: dict[str, Any] = {
        "command": command,
        **state.header(),
        **(extra or {}),
        "applied": result is not None and not result.failed,
        "steps": [s.to_json() for s in (before if not only_script else []) + plan.steps],
        "held_back": [s.to_json() for s in plan.held_back],
        "kept": [{**c.to_json(), "reason": _keep_reason(c)} for c in kept],
        "migrations": [m.to_json() for m in pending],
        "warnings": state.warnings,
    }
    if footer:
        payload["recorded"] = recorded or (result is not None and not result.failed and bool(closing))
    if output is not None:
        payload["output"] = str(output)
    if dry_run:
        payload["script"] = script_text
    if result is not None:
        payload["failed"] = [{"object": k.display, "error": m} for k, m in result.failed]
        payload["transaction"] = result.transactional
        payload["still_different"] = [c.to_json() for c in remaining]
    if opts.as_json:
        sys.stdout.write(json.dumps({"ok": not (result and result.failed), **payload}, indent=2) + "\n")
        if result and result.failed:
            sys.exit(1)
        return

    _print_header(state)
    if not plan.steps and not before:
        if kept:
            console.print("[green]✓[/] Nothing to apply from the repo.")
        else:
            console.print("[green]✓[/] Nothing to do; the database already matches the repo.")
        if recorded:
            console.print("[green]✓[/] Recorded the release in sqlsc.Deployment all the same.")
        _print_kept(kept)
        return
    if dry_run:
        console.print(Syntax(script_text, "sql", theme="ansi_dark", background_color="default", word_wrap=True))
    shown = (before if not only_script else []) + plan.steps
    for step in sorted(shown, key=lambda s: (s.phase, s.key.sort_key)):
        style = {"create": "green", "alter": "yellow", "data": "cyan", "drop": "red", "migrate": "magenta"}.get(
            step.action, "dim"
        )
        console.print(
            f"  [{style}]{step.action:>7}[/]  {TYPE_LABELS[step.key.type]} {escape(step.key.display)}"
            + (f"  [dim]{escape('; '.join(step.notes))}[/]" if step.notes else "")
        )
    console.print()
    if output is not None:
        console.print(f"[green]✓[/] Wrote the script to {escape(str(output))}. Nothing was run.")
    elif dry_run:
        console.print("Dry run: nothing was run.")
    elif result is not None and result.failed:
        key, message = result.failed[0]
        undone = " Every change was rolled back." if result.transactional else ""
        # SQL Server's own messages end with a period already.
        console.print(f"[red]✗[/] {escape(key.display)} failed: {escape(message.rstrip().rstrip('.'))}.{undone}")
        sys.exit(1)
    elif result is not None:
        ran = [k for k in result.done if k.type == "migration"]
        updated = len(result.done) - len(ran)
        scripts = f" and ran {len(ran)} migration script(s)" if ran else ""
        console.print(f"[green]✓[/] Updated {updated} object(s){scripts}.")
        if remaining and ran:
            console.print(
                f"[yellow]![/] {len(remaining)} object(s) now differ from the repo, created or changed by the "
                "scripts: `sqlsc commit` puts them in the repo."
            )
        elif remaining:
            console.print(f"[yellow]![/] {len(remaining)} object(s) still differ; run `sqlsc status` to see them.")
    if plan.held_back:
        console.print(f"[yellow]![/] {len(plan.held_back)} change(s) were held back (see notes above).")
    _print_kept(kept)
    _print_warnings(state.warnings)


def _run_before_migrations(opts: Opts, state: State, plan: Plan):
    """Runs the "before" migration scripts on their own, then reads the database again
    so the structural changes are planned against what they left behind."""
    spinner = (
        nullcontext()
        if opts.as_json
        else err_console.status(f"Running {len(plan.steps)} migration script(s) on '{state.target.name}'…")
    )
    try:
        with spinner:
            conn = open_connection(state.target)
            try:
                result = apply(conn, plan, state.target)
                if not result.failed:
                    state.db = read_database(conn, state.target).objects
                    _read_static_data(conn, state.target, [k for k in state.folder if k.type == "data"], state.db)
                    for key in result.done:
                        state.applied[key.name] = Applied(key.name, datetime.now(timezone.utc), "", "")
            finally:
                conn.close()
    except (ConfigError, ConnectError) as exc:
        fail(opts, "connect", str(exc))
        raise
    return result


def _print_kept(kept: list[Change]) -> None:
    for change in kept:
        console.print(f"[yellow]![/] Kept {escape(change.key.display)}: {escape(_keep_reason(change))}")


def _default_message(state: State, changes: list[Change], scripts: list[str] | None = None) -> str:
    names = ", ".join(c.key.display for c in changes[:5])
    more = f" and {len(changes) - 5} more" if len(changes) > 5 else ""
    if not changes:
        return f"Add {len(scripts or [])} migration script(s): " + ", ".join(scripts or [])
    scripted = f" and {len(scripts)} migration script(s)" if scripts else ""
    return f"Update {len(changes)} object(s){scripted} from {state.target.name}: {names}{more}"


# --- checkout / checkin / locks / unlock / guard ------------------------------------


@main.command()
@click.argument("objects", nargs=-1, required=True)
@click.option("--target", "-t", "target_name", help="Database being worked on (default from sqlsc.yml).")
@click.option("--message", "-m", "note", help="A note the team sees next to the lock (why, or until when).")
@click.option(
    "--type",
    "object_type",
    type=click.Choice(list(OBJECT_TYPES)),
    help="Check out an object that does not exist yet, as this type.",
)
@AS_OPTION
@click.pass_obj
def checkout(
    opts: Opts,
    objects: tuple[str, ...],
    target_name: str | None,
    note: str | None,
    object_type: str | None,
    as_user: str | None,
) -> None:
    """Check objects out so the team knows you are editing them (e.g. sqlsc checkout dbo.Customer)."""
    cfg = _load_config(opts)
    target = _target(opts, cfg, target_name)
    keys = _resolve(opts, cfg, target, objects, object_type)
    with _lock_session(opts, cfg, target, as_user) as session:
        created = _guarded(opts, session.store.ensure)
        granted, conflicts = _guarded(
            opts, lambda: session.store.acquire(keys, repo=session.repo, who=session.who, note=note)
        )
    payload = {
        "command": "checkout",
        **session.header(),
        "created_lock_table": created,
        "checked_out": [_key_json(k) for k in granted],
        "conflicts": [lock.to_json() for lock in conflicts],
    }
    if opts.as_json:
        sys.stdout.write(json.dumps({"ok": not conflicts, **payload}, indent=2) + "\n")
    else:
        if created:
            console.print(f"[dim]Created the lock table sqlsc.ObjectLock in {escape(session.where)}.[/]")
        for key in granted:
            console.print(f"[green]✓[/] Checked out {escape(key.display)} to {escape(session.who)}")
        for lock in conflicts:
            console.print(f"[red]✗[/] {escape(lock.key.display)} is checked out by {_lock_owner_text(lock)}")
    if conflicts:
        sys.exit(1)


@main.command()
@click.argument("objects", nargs=-1)
@click.option("--target", "-t", "target_name", help="Database to commit from (default from sqlsc.yml).")
@click.option("--message", "-m", help="Commit message (default lists the objects).")
@click.option("--push", is_flag=True, help="Push the branch after committing.")
@click.option("--no-commit", is_flag=True, help="Only release the checkouts (undo checkout); commit nothing.")
@AS_OPTION
@click.pass_obj
def checkin(
    opts: Opts,
    objects: tuple[str, ...],
    target_name: str | None,
    message: str | None,
    push: bool,
    no_commit: bool,
    as_user: str | None,
) -> None:
    """Commit the objects you checked out and release them (all of yours when none are named)."""
    cfg = _load_config(opts)
    target = _target(opts, cfg, target_name)
    with _lock_session(opts, cfg, target, as_user) as session:
        mine = [lock for lock in _guarded(opts, lambda: session.store.list(session.repo)) if session.owns(lock)]
        chosen = [lock for lock in mine if not objects or any(matches(lock.key, o) for o in objects)]
        unknown = [o for o in objects if not any(matches(lock.key, o) for lock in mine)]
        keys = [lock.key for lock in chosen]
        sha = branch = None
        changes: list[Change] = []
        state = None
        if keys and not no_commit:
            state = _read_state(opts, target.name)
            wanted = set(keys)
            differences = state.changes()
            pending = _uncommitted(state, {c.key for c in differences})
            changes = [c for c in differences + pending if c.key in wanted]
            held_back = [c for c in changes if not c.from_database]
            changes = [c for c in changes if c.from_database]
            for change in held_back:
                state.warnings.append(f"Left {change.key.display} out of the commit: {_hold_reason(change)}")
            sha, branch = _commit_changes(opts, state, changes, message, push)
        released, _ = _guarded(opts, lambda: session.store.release(keys, repo=session.repo, who=session.who))
    payload = {
        "command": "checkin",
        **session.header(),
        "commit": sha,
        "pushed": branch,
        "push_error": state.push_error if state is not None else None,
        "files": [c.to_json() for c in changes],
        "released": [_key_json(k) for k in released],
        "not_checked_out": list(unknown),
    }
    if opts.as_json:
        sys.stdout.write(json.dumps({"ok": not unknown, **payload}, indent=2) + "\n")
    else:
        if not keys and not unknown:
            console.print(f"Nothing to check in: {escape(session.who)} has nothing checked out.")
        if changes:
            _print_changes(changes)
        if sha:
            console.print(
                f"[green]✓[/] Committed {len(changes)} object(s) as [bold]{sha}[/]"
                + (f" and pushed {escape(branch)}." if branch else ".")
            )
        elif keys and not no_commit:
            console.print("[dim]The checked-out objects match the repo; nothing to commit.[/]")
        for key in released:
            console.print(f"[green]✓[/] Released {escape(key.display)}")
        for name in unknown:
            console.print(f"[yellow]![/] {escape(name)} is not checked out by {escape(session.who)}.")
        if state is not None:
            _print_warnings(state.warnings)
    if unknown:
        sys.exit(1)


@main.command("locks")
@click.option("--target", "-t", "target_name", help="Database being worked on (default from sqlsc.yml).")
@click.option("--mine", is_flag=True, help="Only your own checkouts.")
@click.option("--all-repos", is_flag=True, help="Checkouts for every repo that shares this lock table.")
@AS_OPTION
@click.pass_obj
def list_locks(opts: Opts, target_name: str | None, mine: bool, all_repos: bool, as_user: str | None) -> None:
    """List who has which objects checked out."""
    cfg = _load_config(opts)
    target = _target(opts, cfg, target_name)
    with _lock_session(opts, cfg, target, as_user) as session:
        rows = _guarded(opts, lambda: session.store.list(None if all_repos else session.repo))
    if mine:
        rows = [lock for lock in rows if session.owns(lock)]
    emit(opts, {"command": "locks", **session.header(), "locks": [lock.to_json() for lock in rows]})
    if opts.as_json:
        return
    if not rows:
        console.print("Nothing is checked out" + (f" by {escape(session.who)}." if mine else "."))
        return
    table = RichTable(show_header=True, header_style="bold", box=None, pad_edge=False)
    for col in ("Object", "Type", "Checked out by", "Since (UTC)", "Note") + (("Repo",) if all_repos else ()):
        table.add_column(col)
    for lock in rows:
        who = escape(lock.locked_by) + (" [dim](you)[/]" if session.owns(lock) else "")
        cells = [
            escape(lock.key.display),
            TYPE_LABELS.get(lock.key.type, lock.key.type),
            who,
            lock.locked_at.strftime("%Y-%m-%d %H:%M"),
            escape(lock.note or ""),
        ]
        table.add_row(*cells, *((escape(lock.repo),) if all_repos else ()))
    console.print(table)


@main.command()
@click.argument("objects", nargs=-1, required=True)
@click.option("--target", "-t", "target_name", help="Database being worked on (default from sqlsc.yml).")
@click.option("--force", is_flag=True, help="Release checkouts that belong to someone else.")
@AS_OPTION
@click.pass_obj
def unlock(opts: Opts, objects: tuple[str, ...], target_name: str | None, force: bool, as_user: str | None) -> None:
    """Release checkouts without committing; someone else's need --force."""
    cfg = _load_config(opts)
    target = _target(opts, cfg, target_name)
    with _lock_session(opts, cfg, target, as_user) as session:
        held = _guarded(opts, lambda: session.store.list(session.repo))
        keys = [lock.key for lock in held if any(matches(lock.key, o) for o in objects)]
        released, refused = _guarded(
            opts, lambda: session.store.release(keys, repo=session.repo, who=session.who, force=force)
        )
    unknown = [o for o in objects if not any(matches(lock.key, o) for lock in held)]
    payload = {
        "command": "unlock",
        **session.header(),
        "released": [_key_json(k) for k in released],
        "refused": [lock.to_json() for lock in refused],
        "not_checked_out": unknown,
    }
    if opts.as_json:
        sys.stdout.write(json.dumps({"ok": not refused, **payload}, indent=2) + "\n")
    else:
        for key in released:
            console.print(f"[green]✓[/] Released {escape(key.display)}")
        for lock in refused:
            console.print(
                f"[red]✗[/] {escape(lock.key.display)} is checked out by {_lock_owner_text(lock)}. "
                "Use --force to release it anyway."
            )
        for name in unknown:
            console.print(f"[dim]{escape(name)} is not checked out.[/]")
    if refused:
        sys.exit(1)


@main.command()
@click.argument("action", type=click.Choice(["install", "remove", "status"]))
@click.option("--target", "-t", "target_name", help="Database to guard (default from sqlsc.yml).")
@click.pass_obj
def guard(opts: Opts, action: str, target_name: str | None) -> None:
    """Install, remove or check the edit guard: a database trigger that stops anyone
    changing an object someone else has checked out."""
    cfg = _load_config(opts)
    target = _target(opts, cfg, target_name)
    if target.kind not in locks.GUARD_KINDS:
        fail(
            opts,
            "unsupported",
            f"The edit guard needs SQL Server, Azure SQL or a Fabric SQL database, not {target.label}.",
        )
    if cfg.lock_target and cfg.lock_target != target.name:
        fail(
            opts,
            "unsupported",
            f"The edit guard reads the lock table in the database it guards, but this repo keeps its locks in "
            f"'{cfg.lock_target}'. It only works when locks live in '{target.name}' itself.",
        )
    try:
        conn = open_connection(target)
    except (ConfigError, ConnectError) as exc:
        fail(opts, "connect", str(exc))
        raise
    try:
        cur = conn.cursor()
        if action == "install":
            _guarded(opts, LockStore(conn, transactional=True).ensure)
            try:
                cur.execute(locks.GUARD_SQL)
            except Exception as exc:  # pyodbc.Error
                fail(opts, "permission", f"Could not create the guard trigger: {_first_error_line(exc)}")
        elif action == "remove":
            cur.execute(locks.GUARD_DROP)
        installed = bool(cur.execute(locks.GUARD_EXISTS).fetchone()[0])
    finally:
        conn.close()
    emit(opts, {"command": "guard", "action": action, "target": target.summary(), "installed": installed})
    if opts.as_json:
        return
    if installed:
        console.print(
            f"[green]✓[/] The edit guard is on in {escape(target.database)}: changing an object someone else has "
            "checked out fails with their name."
        )
    else:
        console.print(f"The edit guard is off in {escape(target.database)}.")


# --- lock plumbing -------------------------------------------------------------------


@dataclass
class LockSession:
    store: LockStore
    target: config_mod.Target
    repo: str
    who: str

    @property
    def where(self) -> str:
        return f"{self.target.name} ({self.target.database})"

    def owns(self, lock: Lock) -> bool:
        return _same_user(lock.locked_by, self.who)

    def header(self) -> dict[str, Any]:
        return {"lock_target": self.target.summary(), "repo": self.repo, "user": self.who}


class _LockSessionContext:
    def __init__(self, opts: Opts, cfg: config_mod.Config, working: config_mod.Target, as_user: str | None):
        self.opts, self.cfg, self.working, self.as_user = opts, cfg, working, as_user
        self.conn: Any = None

    def __enter__(self) -> LockSession:
        opts, cfg = self.opts, self.cfg
        try:
            target = cfg.target(cfg.lock_target) if cfg.lock_target else self.working
            locks.check_lock_target(target, configured=bool(cfg.lock_target))
        except ConfigError as exc:
            fail(opts, "config", str(exc))
            raise
        except LockError as exc:
            fail(opts, "locks", str(exc))
            raise
        try:
            self.conn = open_connection(target)
        except (ConfigError, ConnectError) as exc:
            fail(opts, "connect", str(exc))
            raise
        store = LockStore(self.conn, transactional=target.kind in locks.TRANSACTIONAL_KINDS)
        return LockSession(
            store,
            target,
            locks.repo_id(cfg.root, cfg.lock_repo),
            locks.current_user(cfg.root, self.as_user),
        )

    def __exit__(self, *exc: object) -> None:
        if self.conn is not None:
            self.conn.close()


def _lock_session(
    opts: Opts, cfg: config_mod.Config, working: config_mod.Target, as_user: str | None
) -> _LockSessionContext:
    return _LockSessionContext(opts, cfg, working, as_user)


def _guarded(opts: Opts, action: Any) -> Any:
    """Runs a lock-table call, turning its failures into a clean error."""
    try:
        return action()
    except LockError as exc:
        fail(opts, "locks", str(exc))
        raise
    except Exception as exc:  # pyodbc.Error from the lock table
        fail(opts, "locks", f"The lock table could not be read or changed: {_first_error_line(exc)}")
        raise


def _peek_locks(state: State, as_user: str | None) -> tuple[dict[ObjectKey, Lock], str]:
    """Current checkouts for this repo, for status and commit. Never fails: a lock
    database that cannot be reached becomes a warning."""
    cfg = state.config
    who = locks.current_user(cfg.root, as_user)
    try:
        target = cfg.target(cfg.lock_target) if cfg.lock_target else state.target
        locks.check_lock_target(target, configured=bool(cfg.lock_target))
    except (ConfigError, LockError):
        return {}, who
    try:
        conn = open_connection(target)
        try:
            rows = LockStore(conn, transactional=False).list(locks.repo_id(cfg.root, cfg.lock_repo))
        finally:
            conn.close()
    except Exception as exc:  # connect or query failure
        state.warnings.append(f"Checkouts could not be read from '{target.name}': {_first_error_line(exc)}")
        return {}, who
    return {lock.key: lock for lock in rows}, who


def _resolve(
    opts: Opts, cfg: config_mod.Config, target: config_mod.Target, names: tuple[str, ...], object_type: str | None
) -> list[ObjectKey]:
    """The objects a user's names refer to: files in the repo first, then the live database."""
    folder, _ = read_folder(cfg.scripts_dir)
    keys: list[ObjectKey] = []
    missing: list[str] = []
    for name in names:
        found = sorted(k for k in folder if matches(k, name) and (object_type is None or k.type == object_type))
        if found:
            keys += found
        else:
            missing.append(name)
    if missing:
        try:
            conn = open_connection(target)
        except (ConfigError, ConnectError) as exc:
            fail(opts, "connect", str(exc))
            raise
        try:
            for name in missing:
                key = locks.lookup(conn, name)
                if key is not None and (object_type is None or key.type == object_type):
                    keys.append(key)
                elif object_type is not None:
                    keys.append(locks.key_from_name(object_type, name))
                else:
                    fail(
                        opts,
                        "not_found",
                        f"No object called {name} in the repo or in '{target.name}'. To check out an object "
                        "you are about to create, add --type (e.g. --type view).",
                    )
        finally:
            conn.close()
    return list(dict.fromkeys(keys))


def _target(opts: Opts, cfg: config_mod.Config, name: str | None) -> config_mod.Target:
    try:
        return cfg.target(name)
    except ConfigError as exc:
        fail(opts, "config", str(exc))
        raise


def _same_user(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _owner(held: dict[ObjectKey, Lock], key: ObjectKey) -> str | None:
    lock = held.get(key)
    return lock.locked_by if lock else None


def _lock_owner_text(lock: Lock) -> str:
    since = lock.locked_at.strftime("%Y-%m-%d %H:%M")
    note = f" ({escape(lock.note)})" if lock.note else ""
    return f"[bold]{escape(lock.locked_by)}[/] since {since} UTC{note}"


def _key_json(key: ObjectKey) -> dict[str, str]:
    return {"object": key.display, "type": key.type, "schema": key.schema, "name": key.name}


def _first_error_line(exc: Exception) -> str:
    text = str(exc.args[1] if len(exc.args) > 1 else exc)
    return (
        text.split("(SQLExecDirectW)")[0].replace("[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]", "").strip()
    )


# --- shared plumbing --------------------------------------------------------------


@dataclass
class State:
    config: config_mod.Config
    target: config_mod.Target
    db: dict[ObjectKey, str]
    folder: dict[ObjectKey, str]
    warnings: list[str]
    # Objects whose file matches the database but not HEAD (see _uncommitted).
    uncommitted: set[ObjectKey] = field(default_factory=set)
    push_error: str | None = None
    # What this working copy and the target last agreed on (see sync.py).
    sync: SyncRecord = field(default_factory=lambda: SyncRecord(None))
    # Migrations/ scripts in the repo, and the ones sqlsc.Migration says the target has run.
    migrations: list[Migration] = field(default_factory=list)
    applied: dict[str, Applied] = field(default_factory=dict)

    def pending_migrations(self) -> list[Migration]:
        if not self.target.caps["tables"]:
            return []  # nowhere to record them (a Lakehouse); the warning is on state
        return migrations_mod.pending(self.migrations, self.applied)

    @property
    def scripts_dir(self) -> Path:
        return self.config.scripts_dir

    def header(self) -> dict[str, Any]:
        return {"target": self.target.summary(), "folder": str(self.scripts_dir)}

    def changes(self) -> list[Change]:
        """The differences, with the side that moved; also brings the sync record up to date."""
        changes = compare(self.db, self.folder, self.sync)
        self.sync.save()
        return changes


def _load_config(opts: Opts) -> config_mod.Config:
    try:
        return config_mod.load(config_path=opts.config_path)
    except ConfigError as exc:
        fail(opts, "config", str(exc))
        raise  # unreachable; fail exits


def _read_state(opts: Opts, target_name: str | None, *, files: dict[str, str] | None = None) -> State:
    """The database's objects next to the repo's: the working folder, or `files` read from a commit."""
    cfg = _load_config(opts)
    try:
        target = cfg.target(target_name)
    except ConfigError as exc:
        fail(opts, "config", str(exc))
        raise
    folder, unknown = read_folder(cfg.scripts_dir) if files is None else objects_from_files(files)
    scripts = migrations_mod.read_folder(cfg.scripts_dir) if files is None else migrations_mod.from_files(files)
    warnings: list[str] = []
    applied: dict[str, Applied] = {}
    # Files for object types this platform cannot hold are left alone, never
    # reported as deleted (a Lakehouse's tables, a Warehouse's triggers).
    skipped = {t for t, cap in (("table", "tables"), ("trigger", "triggers")) if not target.caps[cap]}
    if "table" in skipped:
        skipped.add("data")
        warnings.append(f"{target.label} tables are not scripted (they are read-only from SQL).")
    platform = platform_schemas(target)
    folder = {
        k: v
        for k, v in folder.items()
        if k.type not in skipped and (k.name if k.type == "schema" else k.schema) not in platform
    }
    spinner = nullcontext() if opts.as_json else err_console.status(f"Reading {target.label} '{target.name}'…")
    try:
        with spinner:
            conn = open_connection(target)
            try:
                result = read_database(conn, target)
                result.warnings += _read_static_data(
                    conn, target, [k for k in folder if k.type == "data"], result.objects
                )
                if scripts and target.caps["tables"]:
                    try:
                        applied = migrations_mod.read_applied(conn)
                    except Exception as exc:  # pyodbc.Error: say so, and treat every script as pending
                        warnings.append(f"sqlsc.Migration could not be read: {_first_error_line(exc)}")
                elif scripts:
                    warnings.append(f"Migration scripts are skipped: {target.label} cannot hold sqlsc.Migration.")
            finally:
                conn.close()
    except ConfigError as exc:
        fail(opts, "config", str(exc))
        raise
    except ConnectError as exc:
        fail(opts, "connect", str(exc))
        raise
    except Exception as exc:  # a catalog query the platform refused outright
        fail(opts, "read", f"Reading {target.label} '{target.name}' failed: {exc}")
        raise
    warnings = list(result.warnings) + warnings
    warnings += [
        f"{path} is in an object folder but its name is not schema.object.sql; it was ignored." for path in unknown
    ]
    # A deploy reads the repo at a commit, not this working copy: no sync record applies.
    sync = SyncRecord.load(cfg.root, target.name) if files is None else SyncRecord(None)
    if files is None:
        _ignore_sync_dir(cfg.root)  # repos linked before the sync record existed
    return State(cfg, target, result.objects, folder, warnings, sync=sync, migrations=scripts, applied=applied)


def _write_from_database(state: State, change: Change) -> Path:
    """Makes the object's file match the database, and notes the two sides as synced."""
    if change.action == DELETED:
        path = delete_object(state.scripts_dir, change.key)
        state.sync.forget(change.key)
    else:
        path = write_object(state.scripts_dir, change.key, change.db_text or "")
        state.sync.record(change.key, comparable(change.key, change.db_text or ""))
    return path


def _sides(changes: list[Change]) -> dict[str, int]:
    return {
        "to_commit": sum(1 for c in changes if c.origin == DATABASE),
        "to_get": sum(1 for c in changes if c.origin == REPO),
        "conflicts": sum(1 for c in changes if c.origin == BOTH),
        "unknown_side": sum(1 for c in changes if c.origin == UNKNOWN),
    }


def _sides_text(sides: dict[str, int]) -> str:
    parts = []
    if sides["to_commit"]:
        parts.append(f"{sides['to_commit']} to commit ([bold]sqlsc commit[/])")
    if sides["to_get"]:
        parts.append(f"{sides['to_get']} to get ([bold]sqlsc get-latest[/])")
    if sides["conflicts"]:
        parts.append(f"{sides['conflicts']} changed on both sides")
    if sides["unknown_side"]:
        parts.append(f"{sides['unknown_side']} not seen in sync before")
    return ", ".join(parts) + "."


def _side_note(change: Change) -> str:
    if change.origin == DATABASE:
        return "dropped from the database" if change.action == DELETED else "changed in the database"
    if change.origin == REPO:
        return "in the repo, not applied here yet" if change.action == DELETED else "changed in the repo"
    if change.origin == BOTH:
        return "changed in the database and in the repo"
    return "not seen in sync before"


def _hold_reason(change: Change) -> str:
    """Why commit leaves an object alone."""
    if change.origin == BOTH:
        return (
            "changed in the database and in the repo since your last sync; "
            "diff it, then --force to commit the database's version"
        )
    if change.action == DELETED:
        return "in the repo but never applied here; `sqlsc get-latest` creates it (--force deletes the file instead)"
    if change.action == NEW:
        return (
            "the repo dropped it since your last sync; "
            "`sqlsc get-latest --allow-drops` drops it here (--force re-adds the file)"
        )
    return "the repo changed it since your last sync; `sqlsc get-latest` applies it (--force commits the database's)"


def _keep_reason(change: Change) -> str:
    """Why get-latest leaves an object alone."""
    if change.origin == BOTH:
        return (
            "changed in the database and in the repo since your last sync; "
            "diff it, then --force to apply the repo's version"
        )
    if change.action == NEW:
        return "new in the database since your last sync; commit it (--force with --allow-drops drops it)"
    if change.action == DELETED:
        return "dropped from the database since your last sync; commit removes its file (--force recreates it)"
    return "changed in the database since your last sync; commit it (--force overwrites it)"


def _ignore_sync_dir(root: Path) -> None:
    """Keeps the sync record out of git: it describes one working copy and one database."""
    ignore = root / ".gitignore"
    line = ".sqlsc/"
    existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if line in existing.splitlines():
        return
    with ignore.open("a", encoding="utf-8", newline="\n") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(f"{line}\n")


def _read_static_data(
    conn: Any, target: config_mod.Target, keys: list[ObjectKey], into: dict[ObjectKey, str]
) -> list[str]:
    """Scripts the rows of every table the repo tracks static data for. A table
    missing from the database is simply absent, so get-latest creates and fills it."""
    warnings: list[str] = []
    for key in sorted(keys):
        try:
            text, count = data_mod.script_data(conn, key, target)
        except data_mod.TableMissing:
            continue
        except data_mod.DataError as exc:
            warnings.append(f"Static data for {key.display} was not read: {exc}")
            continue
        into[key] = text
        if count > data_mod.LARGE_TABLE:
            warnings.append(
                f"{key.display} holds {count} rows; static data suits small lookup tables "
                "(sqlsc data remove stops tracking it)."
            )
    return warnings


def _uncommitted_migrations(state: State) -> list[str]:
    """Migrations/ files that are new, edited or deleted since HEAD: commit takes them along."""
    try:
        root = git_ops.repo_root(state.config.root)
        dirty = git_ops.uncommitted(root, state.scripts_dir)
    except git_ops.GitError:
        return []
    prefix = migrations_mod.FOLDER.lower() + "/"
    return sorted(rel for rel in dirty if rel.lower().startswith(prefix) and rel.lower().endswith(".sql"))


def _uncommitted(state: State, differing: set[ObjectKey]) -> list[Change]:
    """Object files that already match the database but are not committed yet: written
    by `script` or `data add`, or edited and applied with get-latest. The database shows
    no difference for them, yet commit and checkin still owe git these files.
    Records their keys on state so _commit_changes commits them as they are."""
    try:
        root = git_ops.repo_root(state.config.root)
        dirty = git_ops.uncommitted(root, state.scripts_dir)
    except git_ops.GitError:
        return []  # not a repo yet; _commit_changes says so if it gets that far
    found: list[Change] = []
    for rel, how in dirty.items():
        key = key_for_path(rel)
        if key is None or key in differing:
            continue
        if how == "deleted":
            found.append(Change(key, DELETED, None, None, DATABASE))
        elif key in state.folder:
            text = state.folder[key]
            found.append(Change(key, NEW if how == "added" else CHANGED, text, text, DATABASE))
        else:
            continue
        state.uncommitted.add(key)
    found.sort(key=lambda c: c.key.sort_key)
    return found


def _filter(changes: list[Change], objects: tuple[str, ...]) -> list[Change]:
    if not objects:
        return changes
    return [c for c in changes if any(matches(c.key, o) for o in objects)]


def _print_header(state: State) -> None:
    t = state.target
    console.print(
        f"[bold]{escape(t.label)}[/] {escape(t.server)} / {escape(t.database)}  "
        f"[dim]({escape(t.name)}) ↔ {escape(str(state.scripts_dir))}[/]"
    )


def _print_changes(changes: list[Change], held: dict[ObjectKey, Lock] | None = None, who: str = "") -> None:
    held = held or {}
    show_locks = any(c.key in held for c in changes)
    show_sides = any(c.origin != UNKNOWN for c in changes)
    table = RichTable(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Change")
    table.add_column("Type")
    table.add_column("Object")
    if show_sides:
        table.add_column("Changed in")
    if show_locks:
        table.add_column("Checked out by")
    for change in changes:
        style = ACTION_STYLE[change.action]
        cells = [f"[{style}]{ACTION_WORDS[change.action]}[/]", TYPE_LABELS[change.key.type], escape(change.key.display)]
        if show_sides:
            word = ORIGIN_WORDS[change.origin]
            cells.append(f"[red]{word}[/]" if change.origin == BOTH else word)
        if show_locks:
            lock = held.get(change.key)
            cells.append(
                ""
                if lock is None
                else ("[green]you[/]" if _same_user(lock.locked_by, who) else f"[red]{escape(lock.locked_by)}[/]")
            )
        table.add_row(*cells)
    console.print(table)


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        err_console.print(f"[yellow]![/] {escape(warning)}")


from . import cli_release  # noqa: E402,F401  (registers data, deploy, history and migrations)

if __name__ == "__main__":  # pragma: no cover
    main()
