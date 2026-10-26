"""The few git operations `sqlsc commit` needs, through the user's own git.

Only the object files the command wrote are staged and committed, so anything
else the developer has staged or changed stays exactly as it was.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


class GitError(Exception):
    """git is missing, the folder is not a repo, or a git command failed."""


def _git(cwd: Path, *args: str) -> str:
    # Two sqlsc runs at once in one working copy (the SSMS window and coop) meet on
    # git's index.lock; the loser waits a moment and tries again instead of failing.
    for attempt in range(5):
        try:
            return _git_once(cwd, *args)
        except GitError as exc:
            if "index.lock" not in str(exc) or attempt == 4:
                raise
            time.sleep(0.3 * (attempt + 1))
    raise AssertionError("unreachable")


def _git_once(cwd: Path, *args: str) -> str:
    try:
        # git writes paths and messages as UTF-8 whatever the console's code page (a
        # Windows cp1252 locale would read "Café" wrong), so never decode by locale.
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
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


def commit_paths(root: Path, paths: list[Path], message: str, trailers: list[str] | None = None) -> str | None:
    """Stage and commit exactly these paths. Returns the new commit's short hash, or None if nothing changed.

    `trailers` are git trailer lines ("Prepared-by: coop-agent 0.38.6") added as the
    message's last paragraph, so `git log` and `git interpret-trailers` read them."""
    rel = [str(p.resolve().relative_to(root.resolve())) for p in paths]
    if not rel:
        return None
    _git(root, "add", "-A", "--", *rel)
    staged = _git(root, "diff", "--cached", "--name-only", "--", *rel)
    if not staged:
        return None
    extra = ["-m", "\n".join(trailers)] if trailers else []
    _git(root, "commit", "-m", message, *extra, "--only", "--", *rel)
    return _git(root, "rev-parse", "--short", "HEAD")


PREPARED_BY = "Prepared-by"


def prepared_by_trailers(names: list[str]) -> list[str]:
    """One `Prepared-by:` trailer per agent that prepared the change, in order, no repeats."""
    seen: list[str] = []
    for name in names:
        text = " ".join(str(name or "").split())
        if text and text not in seen:
            seen.append(text)
    return [f"{PREPARED_BY}: {name}" for name in seen]


def agent_of(user: str | None) -> str | None:
    """The agent in an `<agent>:<person>` checkout name (`coop-agent:ada@example.com`), else None."""
    text = (user or "").strip()
    if ":" not in text:
        return None
    agent = text.split(":", 1)[0].strip()
    return agent or None


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
    if branch == "HEAD":
        raise GitError("HEAD is detached (no branch is checked out), so there is nothing to push.")
    done = _git_quiet(root, "push", "-u", "origin", branch)
    if done.returncode != 0:
        reason = _reason(done.stderr or done.stdout)
        if "non-fast-forward" in (done.stderr or "") or "fetch first" in (done.stderr or "") or "rejected" in reason:
            raise GitError(
                f"git push was rejected: someone pushed to {branch} since this copy last pulled. "
                "Run `git pull` then `git push`."
            )
        raise GitError(f"git push failed: {reason}")
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
    try:
        listed = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-tree", "-r", "-z", "--name-only", commit, "--", prefix or "."],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH.") from exc
    if listed.returncode != 0:
        raise GitError(f"git ls-tree failed: {_reason(listed.stderr.decode('utf-8', 'replace'))}")
    try:
        listing = listed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitError(f"a file name in {commit[:12]} is not UTF-8; rename it and commit again.") from exc
    names = [n for n in listing.split("\0") if n.lower().endswith(".sql") and n.startswith(prefix)]
    if not names:
        return {}
    odd = [n for n in names if "\n" in n]
    if odd:
        raise GitError(f"{odd[0]!r} has a line break in its name; rename it and commit again.")
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
            # A release read without one of its files would plan that object as gone
            # (a DROP with --allow-drops): stop instead of reading on.
            raise GitError(f"git could not read {name} in {commit[:12]} ({' '.join(header) or 'no answer'}).")
        size = int(header[2])
        files[name[len(prefix) :]] = out[pos : pos + size].decode("utf-8-sig", "replace")
        pos += size + 1
    return files


# --- is the working copy ready for a commit? -----------------------------------------------

# git must never stop to ask for a password: the SSMS add-in and coop run it without a
# console, so a prompt would hang until the add-in's timeout kills it. These are
# defaults; anything the user already set in the environment stays.
_NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"}


def _git_quiet(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**_NO_PROMPT, **os.environ}  # a setting the user made wins, but nothing is left to prompt
    try:
        return subprocess.run(
            ["git", "-c", "credential.interactive=never", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH.") from exc


def _ok(cwd: Path, *args: str) -> str | None:
    done = _git_quiet(cwd, *args)
    return done.stdout.strip() if done.returncode == 0 else None


class RepoState:
    """What `git` says about the working copy before sqlsc commits into it."""

    def __init__(self, root: Path):
        self.root = root
        self.branch: str | None = _ok(root, "symbolic-ref", "--quiet", "--short", "HEAD")  # None: detached HEAD
        self.upstream: str | None = _ok(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        self.user_name = _ok(root, "config", "--get", "user.name") or ""
        self.user_email = _ok(root, "config", "--get", "user.email") or ""
        self.in_progress = self._in_progress()
        self.unmerged = self._unmerged()
        self.ahead, self.behind = self._counts()

    def _in_progress(self) -> str | None:
        git_dir = _ok(self.root, "rev-parse", "--git-dir")
        if not git_dir:
            return None
        base = Path(git_dir) if Path(git_dir).is_absolute() else self.root / git_dir
        for marker, what in (
            ("MERGE_HEAD", "merge"),
            ("rebase-merge", "rebase"),
            ("rebase-apply", "rebase"),
            ("CHERRY_PICK_HEAD", "cherry-pick"),
            ("REVERT_HEAD", "revert"),
            ("BISECT_LOG", "bisect"),
        ):
            if (base / marker).exists():
                return what
        return None

    def _unmerged(self) -> list[str]:
        """Paths git still has as conflicted (relative to the repo root)."""
        done = _git_quiet(self.root, "diff", "--name-only", "--diff-filter=U", "-z")
        if done.returncode != 0:
            return []
        return sorted({p for p in done.stdout.split("\0") if p})

    def _counts(self) -> tuple[int, int]:
        """Commits ahead of and behind the upstream as last fetched (no network)."""
        if not self.upstream:
            return 0, 0
        text = _ok(self.root, "rev-list", "--left-right", "--count", "HEAD...@{u}")
        if not text:
            return 0, 0
        parts = text.split()
        try:
            return int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            return 0, 0

    @property
    def has_identity(self) -> bool:
        return bool(self.user_name and self.user_email)

    def to_json(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "detached": self.branch is None,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "in_progress": self.in_progress,
            "unmerged": self.unmerged,
            "identity": f"{self.user_name} <{self.user_email}>" if self.has_identity else None,
        }

    def commit_problem(self) -> str | None:
        """Why a commit must not be made now, or None. Each reason names the way out."""
        if self.branch is None:
            return (
                "HEAD is detached (no branch is checked out), so a commit here would be lost. "
                "Check out a branch first: `git switch <branch>`."
            )
        if self.in_progress:
            return (
                f"a git {self.in_progress} is in progress in this repository. Finish it or abort it "
                f"(`git {self.in_progress} --abort`) before committing."
            )
        if self.unmerged:
            shown = ", ".join(self.unmerged[:3]) + (", …" if len(self.unmerged) > 3 else "")
            return (
                f"{len(self.unmerged)} file(s) still have merge conflicts: {shown}. Resolve them "
                "(`git add` each resolved file, then `git commit`) before committing."
            )
        if not self.has_identity:
            return (
                "git does not know who you are in this repository (user.name and user.email are not set), "
                'so it cannot record the commit. Set them: `git config user.name "Ada Lovelace"` and '
                "`git config user.email ada@example.com`."
            )
        return None

    def warnings(self) -> list[str]:
        """What status tells a person about the working copy."""
        notes: list[str] = []
        problem = self.commit_problem()
        if problem:
            notes.append("The repository is not ready for a commit: " + problem)
        if self.behind:
            notes.append(
                f"This working copy is {self.behind} commit(s) behind {self.upstream} (as last fetched); "
                "commit pulls first, or run `git pull`."
            )
        return notes


def pull_fast_forward(root: Path, upstream: str) -> tuple[int, list[str]]:
    """Fetches the upstream and fast-forwards the branch onto it. Returns the number of
    commits pulled and the paths (relative to the repo root) the pull changed. Raises
    GitError, naming the way out, when it cannot fast-forward."""
    remote = upstream.split("/", 1)[0] or "origin"
    fetched = _git_quiet(root, "fetch", "--quiet", remote)
    if fetched.returncode != 0:
        raise GitError(
            f"could not fetch {remote} ({_reason(fetched.stderr or fetched.stdout)}). Check the network and "
            "your sign-in to the remote, pull by hand, then commit again."
        )
    count = _ok(root, "rev-list", "--count", "HEAD..@{u}")
    behind = int(count) if count and count.isdigit() else 0
    if not behind:
        return 0, []
    before = _ok(root, "rev-parse", "HEAD") or "HEAD"
    ancestor = _git_quiet(root, "merge-base", "--is-ancestor", "HEAD", "@{u}")
    if ancestor.returncode != 0:
        raise GitError(
            f"this branch is {behind} commit(s) behind {upstream} and has its own commits too, so it cannot "
            "fast-forward. Run `git pull` (merge or rebase) by hand, resolve anything it reports, then commit again."
        )
    pulled = _git_quiet(root, "pull", "--ff-only", "--quiet")
    if pulled.returncode != 0:
        raise GitError(
            f"{behind} commit(s) behind {upstream} and the pull did not fast-forward "
            f"({_reason(pulled.stderr or pulled.stdout)}). Commit or stash your other edits, run `git pull`, "
            "then commit again."
        )
    changed = _git_quiet(root, "diff", "--name-only", "-z", before, "HEAD")
    paths = sorted({p for p in changed.stdout.split("\0") if p}) if changed.returncode == 0 else []
    return behind, paths


def head_text(root: Path, rel: str) -> str | None:
    """A file's text at HEAD (rel is relative to the repo root), or None when HEAD has no such file."""
    done = _git_quiet(root, "show", f"HEAD:{rel}")
    return done.stdout if done.returncode == 0 else None


def has_conflict_markers(text: str) -> bool:
    """True when a file still carries git's merge conflict markers (an `<<<<<<<` line
    followed by a `>>>>>>>` line), as a half-resolved merge leaves it."""
    opened = False
    for line in text.splitlines():
        if line.startswith("<<<<<<< ") or line.rstrip() == "<<<<<<<":
            opened = True
        elif opened and (line.startswith(">>>>>>> ") or line.rstrip() == ">>>>>>>"):
            return True
    return False
