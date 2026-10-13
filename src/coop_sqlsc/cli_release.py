"""Static data, release deploys and deployment history: `sqlsc data`, `sqlsc deploy`, `sqlsc history`."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.markup import escape
from rich.table import Table as RichTable

from . import __version__, git_ops, locks
from . import data as data_mod
from .cli import (
    AS_OPTION,
    Opts,
    _first_error_line,
    _key_json,
    _load_config,
    _read_state,
    _target,
    console,
    emit,
    fail,
    main,
    run_update,
)
from .compare import matches
from .config import ConfigError
from .connect import ConnectError, open_connection
from .deploy import Plan
from .layout import delete_object, read_folder, write_object
from .objects import ObjectKey

# --- static data ---------------------------------------------------------------------------


@main.group()
def data() -> None:
    """Keep the rows of lookup tables in the repo (static data).

    \b
      sqlsc data add sales.Status     start tracking a table's rows
      sqlsc data remove sales.Status  stop tracking them (the rows stay in the database)
    After that, status, diff, commit, get-latest and deploy include the rows.
    """


@data.command("add")
@click.argument("tables", nargs=-1, required=True)
@click.option("--target", "-t", "target_name", help="Database to read the rows from (default from sqlsc.yml).")
@click.pass_obj
def data_add(opts: Opts, tables: tuple[str, ...], target_name: str | None) -> None:
    """Start tracking the rows of these tables; writes Data/<schema>.<table>.sql."""
    cfg = _load_config(opts)
    target = _target(opts, cfg, target_name)
    if not target.caps["tables"]:
        fail(opts, "unsupported", f"{target.label} tables are read-only from SQL, so they cannot hold static data.")
    written: list[dict[str, Any]] = []
    try:
        conn = open_connection(target)
    except (ConfigError, ConnectError) as exc:
        fail(opts, "connect", str(exc))
        raise
    try:
        for name in tables:
            found = locks.lookup(conn, name)
            if found is None or found.type != "table":
                fail(opts, "not_found", f"No table called {name} in '{target.name}'.")
                raise AssertionError  # unreachable; fail exits
            key = ObjectKey("data", found.schema, found.name)
            try:
                text, count = data_mod.script_data(conn, key, target)
            except data_mod.DataError as exc:
                fail(opts, "data", str(exc))
                raise
            except Exception as exc:  # pyodbc.Error reading the rows
                fail(opts, "read", f"Reading the rows of {key.display} failed: {_first_error_line(exc)}")
                raise
            path = write_object(cfg.scripts_dir, key, text)
            written.append({**_key_json(key), "rows": count, "path": str(path)})
    finally:
        conn.close()
    emit(opts, {"command": "data add", "target": target.summary(), "tracked": written})
    if opts.as_json:
        return
    for item in written:
        console.print(
            f"[green]✓[/] Tracking {item['rows']} row(s) of {escape(item['object'])} in {escape(item['path'])}"
        )
        if item["rows"] > data_mod.LARGE_TABLE:
            console.print("  [yellow]![/] That is a lot of rows for static data; it suits small lookup tables.")
    console.print("Commit the file(s) with [bold]sqlsc commit[/] or git.")


@data.command("remove")
@click.argument("tables", nargs=-1, required=True)
@click.pass_obj
def data_remove(opts: Opts, tables: tuple[str, ...]) -> None:
    """Stop tracking these tables' rows (deletes their Data file; the rows stay in the database)."""
    cfg = _load_config(opts)
    folder, _ = read_folder(cfg.scripts_dir)
    tracked = [k for k in folder if k.type == "data"]
    removed = [k for k in tracked if any(matches(k, t) for t in tables)]
    unknown = [t for t in tables if not any(matches(k, t) for k in tracked)]
    for key in removed:
        delete_object(cfg.scripts_dir, key)
    payload = {"command": "data remove", "removed": [_key_json(k) for k in removed], "not_tracked": unknown}
    if opts.as_json:
        sys.stdout.write(json.dumps({"ok": not unknown, **payload}, indent=2) + "\n")
    else:
        for key in removed:
            console.print(f"[green]✓[/] Stopped tracking the rows of {escape(key.display)}")
        for name in unknown:
            console.print(f"[yellow]![/] {escape(name)} has no static data in the repo.")
    if unknown:
        sys.exit(1)


# --- deploy ----------------------------------------------------------------------------------

DEPLOYMENT_TABLE = """
IF OBJECT_ID('sqlsc.Deployment') IS NULL
CREATE TABLE sqlsc.Deployment
(
      DeployedAt datetime2(6) NOT NULL
     ,CommitId varchar(40) NOT NULL
     ,Ref varchar(200) NOT NULL
     ,Repo varchar(400) NOT NULL
     ,PreparedBy varchar(256) NOT NULL
     ,LoginName varchar(256) NOT NULL
     ,Changes int NOT NULL
)
""".strip()
HISTORY_SQL = (
    "SELECT TOP (?) DeployedAt, CommitId, Ref, Repo, PreparedBy, LoginName, Changes "
    "FROM sqlsc.Deployment ORDER BY DeployedAt DESC"
)


@main.command()
@click.option("--target", "-t", "target_name", help="Database to release to (default from sqlsc.yml).")
@click.option("--ref", default="HEAD", show_default=True, help="The commit, branch or tag to release.")
@click.option("--dry-run", is_flag=True, help="Print the release script instead of running it.")
@click.option(
    "--output", "-o", type=click.Path(path_type=Path, dir_okay=False), help="Write the release script to a file."
)
@click.option("--allow-drops", is_flag=True, help="Also drop objects and columns the release no longer has.")
@click.option("--confirm", metavar="TARGET", help="The target's name again; required to change a protected target.")
@AS_OPTION
@click.pass_obj
def deploy(
    opts: Opts,
    target_name: str | None,
    ref: str,
    dry_run: bool,
    output: Path | None,
    allow_drops: bool,
    confirm: str | None,
    as_user: str | None,
) -> None:
    """Release a commit of the repo to a database (test, prod): only committed
    files count, and the run is recorded in sqlsc.Deployment.

    \b
      sqlsc deploy -t test                        release HEAD to test
      sqlsc deploy -t prod --ref v1.4 -o v1.4.sql write the script for review
      sqlsc deploy -t prod --ref v1.4 --confirm prod
    """
    cfg = _load_config(opts)
    try:
        root = git_ops.repo_root(cfg.root)
        commit = git_ops.resolve(root, ref)
        files = git_ops.read_tree(root, commit, cfg.scripts_dir)
    except git_ops.GitError as exc:
        fail(opts, "git", str(exc))
        raise
    state = _read_state(opts, target_name, files=files)
    target = state.target
    blocked = None
    if target.protected and not (dry_run or output is not None) and confirm != target.name:
        blocked = (
            f"'{target.name}' is protected. Review the script first (--output release.sql), then run the "
            f"release with --confirm {target.name}."
        )
    repo = locks.repo_id(cfg.root, cfg.lock_repo)
    who = locks.current_user(cfg.root, as_user)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    header = [
        f"-- Release of {repo} at {ref} ({commit[:12]}) to {target.label} '{target.name}' ({target.database}).",
        f"-- Written by sqlsc {__version__} for {who} at {now} UTC, against the database as it was then;",
        "-- if the database has changed since, write the script again. GO separates the batches (sqlcmd, SSMS).",
    ]

    def record(plan: Plan) -> list[str]:
        values = ", ".join(
            [
                "SYSUTCDATETIME()",
                _text(commit),
                _text(ref[:200]),
                _text(repo[:400]),
                _text(who[:256]),
                "SUSER_SNAME()",
                str(len(plan.runnable)),
            ]
        )
        return [
            locks.CREATE_SCHEMA,
            DEPLOYMENT_TABLE,
            "INSERT INTO sqlsc.Deployment (DeployedAt, CommitId, Ref, Repo, PreparedBy, LoginName, Changes)\n"
            f"VALUES ({values})",
        ]

    run_update(
        opts,
        state,
        objects=(),
        command="deploy",
        dry_run=dry_run,
        output=output,
        allow_drops=allow_drops,
        blocked=blocked,
        header=header,
        footer=record,
        extra={"ref": ref, "commit": commit, "repo": repo},
    )


@main.command()
@click.option("--target", "-t", "target_name", help="Database to read (default from sqlsc.yml).")
@click.option("--limit", default=20, show_default=True, help="How many releases to list, newest first.")
@click.pass_obj
def history(opts: Opts, target_name: str | None, limit: int) -> None:
    """List the releases `sqlsc deploy` has made to a database."""
    cfg = _load_config(opts)
    target = _target(opts, cfg, target_name)
    try:
        conn = open_connection(target)
    except (ConfigError, ConnectError) as exc:
        fail(opts, "connect", str(exc))
        raise
    try:
        cur = conn.cursor()
        exists = cur.execute("SELECT CASE WHEN OBJECT_ID('sqlsc.Deployment') IS NULL THEN 0 ELSE 1 END").fetchone()[0]
        rows = cur.execute(HISTORY_SQL, max(1, limit)).fetchall() if exists else []
    except Exception as exc:  # pyodbc.Error
        fail(opts, "read", f"Reading sqlsc.Deployment failed: {_first_error_line(exc)}")
        raise
    finally:
        conn.close()
    releases = [
        {
            "deployed_at": r[0].replace(tzinfo=timezone.utc).isoformat(),
            "commit": r[1],
            "ref": r[2],
            "repo": r[3],
            "prepared_by": r[4],
            "login": r[5],
            "changes": r[6],
        }
        for r in rows
    ]
    emit(opts, {"command": "history", "target": target.summary(), "releases": releases})
    if opts.as_json:
        return
    if not releases:
        console.print(f"No releases recorded in '{escape(target.name)}' yet (sqlsc deploy records them).")
        return
    table = RichTable(show_header=True, header_style="bold", box=None, pad_edge=False)
    for col in ("Deployed (UTC)", "Ref", "Commit", "Prepared by", "Login", "Changes"):
        table.add_column(col)
    for r in releases:
        table.add_row(
            r["deployed_at"][:16].replace("T", " "),
            escape(r["ref"]),
            r["commit"][:12],
            escape(r["prepared_by"]),
            escape(r["login"]),
            str(r["changes"]),
        )
    console.print(table)


def _text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
