"""The few git operations `sqlsc commit` needs, through the user's own git.

Only the object files the command wrote are staged and committed, so anything
else the developer has staged or changed stays exactly as it was.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    """git is missing, the folder is not a repo, or a git command failed."""


def _git(cwd: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH.") from exc
    if done.returncode != 0:
        raise GitError(f"git {args[0]} failed: {_reason(done.stderr or done.stdout)}")
    return done.stdout.strip()


def _reason(output: str) -> str:
    """git's own fatal:/error: line: the last line is often the tail of a hint
    ("and the repository exists.") rather than what went wrong."""
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    for prefix in ("fatal:", "error:"):
        found = [line for line in lines if line.lower().startswith(prefix)]
        if found:
            return found[0][len(prefix) :].strip().rstrip(".")
    return lines[-1].rstrip(".") if lines else "no output"


def repo_root(path: Path) -> Path:
    try:
        return Path(_git(path, "rev-parse", "--show-toplevel"))
    except GitError as exc:
        raise GitError(f"{path} is not inside a git repository. Run `git init` first.") from exc


def commit_paths(root: Path, paths: list[Path], message: str) -> str | None:
    """Stage and commit exactly these paths. Returns the new commit's short hash, or None if nothing changed."""
    rel = [str(p.resolve().relative_to(root.resolve())) for p in paths]
    if not rel:
        return None
    _git(root, "add", "-A", "--", *rel)
    staged = _git(root, "diff", "--cached", "--name-only", "--", *rel)
    if not staged:
        return None
    _git(root, "commit", "-m", message, "--only", "--", *rel)
    return _git(root, "rev-parse", "--short", "HEAD")


def uncommitted(root: Path, folder: Path) -> dict[str, str]:
    """Files under folder that differ from HEAD (staged, unstaged or untracked):
    {path relative to folder: 'added' | 'modified' | 'deleted'}."""
    prefix = folder.resolve().relative_to(root.resolve()).as_posix()
    prefix = "" if prefix == "." else prefix + "/"
    try:
        done = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--", prefix or "."],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH.") from exc
    if done.returncode != 0:
        raise GitError(f"git status failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    files: dict[str, str] = {}
    for entry in done.stdout.decode("utf-8", "replace").split("\0"):
        if len(entry) < 4 or not entry[3:].startswith(prefix):
            continue
        xy, path = entry[:2], entry[3:]
        if "D" in xy:
            state = "deleted"
        elif xy == "??" or "A" in xy:
            state = "added"
        else:
            state = "modified"
        files[path[len(prefix) :]] = state
    return files


def push(root: Path) -> str:
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    _git(root, "push", "-u", "origin", branch)
    return branch


def resolve(root: Path, ref: str) -> str:
    """The full commit hash a branch, tag or hash names."""
    try:
        return _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except GitError as exc:
        raise GitError(f"'{ref}' is not a commit, branch or tag in this repository.") from exc


def read_tree(root: Path, commit: str, folder: Path) -> dict[str, str]:
    """Every .sql file under folder as it was in commit: {path relative to folder: text}."""
    prefix = folder.resolve().relative_to(root.resolve()).as_posix()
    prefix = "" if prefix == "." else prefix + "/"
    listing = _git(root, "ls-tree", "-r", "-z", "--name-only", commit, "--", prefix or ".")
    names = [n for n in listing.split("\0") if n.lower().endswith(".sql") and n.startswith(prefix)]
    if not names:
        return {}
    request = "".join(f"{commit}:{name}\n" for name in names).encode("utf-8")
    try:
        done = subprocess.run(["git", "cat-file", "--batch"], cwd=root, input=request, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH.") from exc
    if done.returncode != 0:
        raise GitError(f"git cat-file failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    files: dict[str, str] = {}
    out, pos = done.stdout, 0
    for name in names:
        end = out.index(b"\n", pos)
        header = out[pos:end].decode("utf-8", "replace").split()
        pos = end + 1
        if len(header) < 3 or header[1] != "blob":
            continue
        size = int(header[2])
        files[name[len(prefix) :]] = out[pos : pos + size].decode("utf-8-sig", "replace")
        pos += size + 1
    return files
