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
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table as RichTable

from . import __version__, git_ops
from . import config as config_mod
from .catalog import read_database
from .compare import CHANGED, DELETED, NEW, Change, compare, matches, summarize
from .config import KINDS, ConfigError
from .connect import ConnectError, open_connection
from .deploy import apply, build_plan
from .layout import delete_object, read_folder, write_object
from .objects import TYPE_LABELS, ObjectKey

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


@main.command()
@click.option("--target", "-t", "target_name", help="Target to compare (default from sqlsc.yml).")
@click.option("--exit-code", is_flag=True, help="Exit 1 when anything differs (for CI).")
@click.pass_obj
def status(opts: Opts, target_name: str | None, exit_code: bool) -> None:
    """Show which objects differ between the database and the repo."""
    state = _read_state(opts, target_name)
    changes = compare(state.db, state.folder)
    counts = summarize(changes)
    emit(
        opts,
        {
            "command": "status",
            **state.header(),
            "summary": counts,
            "changes": [c.to_json() for c in changes],
            "warnings": state.warnings,
        },
    )
    if not opts.as_json:
        _print_header(state)
        if not changes:
            console.print(f"[green]✓[/] The repo matches the database ({len(state.db)} objects).")
        else:
            _print_changes(changes)
            console.print()
            console.print(
                f"{counts[NEW]} new, {counts[CHANGED]} changed, {counts[DELETED]} deleted. "
                "Run [bold]sqlsc diff <object>[/] to see one, or [bold]sqlsc script[/] to bring the repo up to date."
            )
        _print_warnings(state.warnings)
    if exit_code and changes:
        sys.exit(1)


@main.command()
@click.argument("objects", nargs=-1)
@click.option("--target", "-t", "target_name", help="Target to compare (default from sqlsc.yml).")
@click.option("--context", default=3, show_default=True, help="Lines of context around each change.")
@click.pass_obj
def diff(opts: Opts, objects: tuple[str, ...], target_name: str | None, context: int) -> None:
    """Show the text differences, for all objects or the ones named (e.g. dbo.Customer)."""
    state = _read_state(opts, target_name)
    changes = _filter(compare(state.db, state.folder), objects)
    items = [{**c.to_json(), "diff": c.unified_diff(context=context)} for c in changes]
    emit(opts, {"command": "diff", **state.header(), "changes": items, "warnings": state.warnings})
    if opts.as_json:
        return
    if not changes:
        console.print("[green]✓[/] No differences" + (f" for {', '.join(objects)}." if objects else "."))
        return
    for change, item in zip(changes, items, strict=True):
        style = ACTION_STYLE[change.action]
        console.rule(f"[{style}]{ACTION_WORDS[change.action]}[/] {escape(change.key.display)}", align="left")
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
    changes = _filter(compare(state.db, state.folder), objects)
    written: list[dict[str, str]] = []
    for change in changes:
        if not dry_run:
            if change.action == DELETED:
                delete_object(state.scripts_dir, change.key)
            else:
                write_object(state.scripts_dir, change.key, change.db_text or "")
        written.append(change.to_json())
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
@click.pass_obj
def commit(opts: Opts, objects: tuple[str, ...], target_name: str | None, message: str | None, push: bool) -> None:
    """Script the database's changes into the repo and commit just those files."""
    state = _read_state(opts, target_name)
    changes = _filter(compare(state.db, state.folder), objects)
    try:
        root = git_ops.repo_root(state.config.root)
    except git_ops.GitError as exc:
        fail(opts, "git", str(exc))
        raise
    paths: list[Path] = []
    for change in changes:
        if change.action == DELETED:
            paths.append(delete_object(state.scripts_dir, change.key))
        else:
            paths.append(write_object(state.scripts_dir, change.key, change.db_text or ""))
    sha = None
    branch = None
    if paths:
        text = message or _default_message(state, changes)
        try:
            sha = git_ops.commit_paths(root, paths, text)
            if sha and push:
                branch = git_ops.push(root)
        except git_ops.GitError as exc:
            fail(opts, "git", str(exc))
            raise
    emit(
        opts,
        {
            "command": "commit",
            **state.header(),
            "commit": sha,
            "pushed": branch,
            "summary": summarize(changes),
            "files": [c.to_json() for c in changes],
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
        console.print(
            f"[green]✓[/] Committed {len(changes)} object(s) as [bold]{sha}[/]"
            + (f" and pushed {escape(branch)}." if branch else ".")
        )
    _print_warnings(state.warnings)


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
@click.pass_obj
def get_latest(
    opts: Opts, objects: tuple[str, ...], target_name: str | None, dry_run: bool, output: Path | None, allow_drops: bool
) -> None:
    """Update the database to match the repo (like Redgate's Get latest)."""
    state = _read_state(opts, target_name)
    changes = _filter(compare(state.db, state.folder), objects)
    plan = build_plan(changes, allow_drops=allow_drops, enforced_keys=state.target.caps["enforced_keys"])
    script_text = plan.script(state.target)
    only_script = dry_run or output is not None
    if not only_script and state.target.protected and plan.runnable:
        fail(
            opts,
            "protected",
            f"'{state.target.name}' is protected, so get-latest will not change it. "
            "Use --output to write the script for review, or set `protected: false` on the target.",
        )
    if output is not None:
        output.write_text(script_text, encoding="utf-8")

    result = None
    remaining: list[Change] = []
    if not only_script and plan.runnable:
        spinner = (
            nullcontext()
            if opts.as_json
            else err_console.status(f"Updating {state.target.label} '{state.target.name}'…")
        )
        try:
            with spinner:
                conn = open_connection(state.target)
                try:
                    result = apply(conn, plan, state.target)
                    after = read_database(conn, state.target).objects
                finally:
                    conn.close()
        except (ConfigError, ConnectError) as exc:
            fail(opts, "connect", str(exc))
            raise
        remaining = _filter(compare(after, state.folder), objects)

    payload: dict[str, Any] = {
        "command": "get-latest",
        **state.header(),
        "applied": result is not None and not result.failed,
        "steps": [s.to_json() for s in plan.steps],
        "held_back": [s.to_json() for s in plan.held_back],
        "warnings": state.warnings,
    }
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
    if not plan.steps:
        console.print("[green]✓[/] Nothing to do; the database already matches the repo.")
        return
    if dry_run:
        console.print(Syntax(script_text, "sql", theme="ansi_dark", background_color="default", word_wrap=True))
    for step in sorted(plan.steps, key=lambda s: (s.phase, s.key.sort_key)):
        style = {"create": "green", "alter": "yellow", "drop": "red"}.get(step.action, "dim")
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
        console.print(f"[red]✗[/] {escape(key.display)} failed: {escape(message)}.{undone}")
        sys.exit(1)
    elif result is not None:
        console.print(f"[green]✓[/] Updated {len(result.done)} object(s).")
        if remaining:
            console.print(f"[yellow]![/] {len(remaining)} object(s) still differ; run `sqlsc status` to see them.")
    if plan.held_back:
        console.print(f"[yellow]![/] {len(plan.held_back)} change(s) were held back (see notes above).")
    _print_warnings(state.warnings)


def _default_message(state: State, changes: list[Change]) -> str:
    names = ", ".join(c.key.display for c in changes[:5])
    more = f" and {len(changes) - 5} more" if len(changes) > 5 else ""
    return f"Update {len(changes)} object(s) from {state.target.name}: {names}{more}"


# --- shared plumbing --------------------------------------------------------------


@dataclass
class State:
    config: config_mod.Config
    target: config_mod.Target
    db: dict[ObjectKey, str]
    folder: dict[ObjectKey, str]
    warnings: list[str]

    @property
    def scripts_dir(self) -> Path:
        return self.config.scripts_dir

    def header(self) -> dict[str, Any]:
        return {"target": self.target.summary(), "folder": str(self.scripts_dir)}


def _load_config(opts: Opts) -> config_mod.Config:
    try:
        return config_mod.load(config_path=opts.config_path)
    except ConfigError as exc:
        fail(opts, "config", str(exc))
        raise  # unreachable; fail exits


def _read_state(opts: Opts, target_name: str | None) -> State:
    cfg = _load_config(opts)
    try:
        target = cfg.target(target_name)
    except ConfigError as exc:
        fail(opts, "config", str(exc))
        raise
    spinner = nullcontext() if opts.as_json else err_console.status(f"Reading {target.label} '{target.name}'…")
    try:
        with spinner:
            conn = open_connection(target)
            try:
                result = read_database(conn, target)
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
    folder, unknown = read_folder(cfg.scripts_dir)
    warnings = list(result.warnings)
    warnings += [
        f"{path} is in an object folder but its name is not schema.object.sql; it was ignored." for path in unknown
    ]
    # Files for object types this platform cannot hold are left alone, never
    # reported as deleted (a Lakehouse's tables, a Warehouse's triggers).
    skipped = {t for t, cap in (("table", "tables"), ("trigger", "triggers")) if not target.caps[cap]}
    if "table" in skipped:
        warnings.append(f"{target.label} tables are not scripted (they are read-only from SQL).")
    folder = {k: v for k, v in folder.items() if k.type not in skipped}
    return State(cfg, target, result.objects, folder, warnings)


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


def _print_changes(changes: list[Change]) -> None:
    table = RichTable(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Change")
    table.add_column("Type")
    table.add_column("Object")
    for change in changes:
        style = ACTION_STYLE[change.action]
        table.add_row(
            f"[{style}]{ACTION_WORDS[change.action]}[/]", TYPE_LABELS[change.key.type], escape(change.key.display)
        )
    console.print(table)


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        err_console.print(f"[yellow]![/] {escape(warning)}")


if __name__ == "__main__":  # pragma: no cover
    main()
