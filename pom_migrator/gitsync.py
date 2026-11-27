"""Reset repositories to a clean copy of <remote>/<branch> (DESTRUCTIVE, with optional backup).

Flow: fetch() -> plan() -> (user confirms) -> execute().
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REMOTE = "origin"
BRANCH = "main"
BACKUP_PREFIX = "backup/pom-migrator"

_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang on a credential prompt


class GitCommandError(Exception):
    pass


def _git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_ENV,
    )


def _git_ok(repo: Path, *args: str, timeout: int = 30) -> str:
    result = _git(repo, *args, timeout=timeout)
    if result.returncode != 0:
        lines = [l.strip() for l in (result.stderr or result.stdout).splitlines() if l.strip()]
        detail = next((l for l in lines if l.startswith(("fatal:", "error:"))), lines[-1] if lines else f"exit {result.returncode}")
        raise GitCommandError(f"git {args[0]}: {detail}")
    return result.stdout.strip()


def _count(repo: Path, range_: str) -> int:
    result = _git(repo, "rev-list", "--count", range_)
    return int(result.stdout.strip()) if result.returncode == 0 else 0


@dataclass
class RepoPlan:
    repo: Path
    projects: list[str]
    target: str = f"{REMOTE}/{BRANCH}"
    branch: str | None = None           # current branch (None = detached)
    commit: str | None = None
    target_commit: str | None = None
    modified: int = 0                   # tracked files with changes
    untracked: int = 0
    head_unpushed: int = 0              # commits on HEAD not in target
    main_unpushed: int = 0              # commits on local BRANCH not in target
    fetch_error: str | None = None
    skip_reason: str | None = None

    @property
    def up_to_date(self) -> bool:
        return (
            self.branch == BRANCH
            and self.commit is not None
            and self.commit == self.target_commit
            and not self.modified
            and not self.untracked
        )

    @property
    def detached(self) -> bool:
        return self.branch is None and self.commit is not None

    def losses(self, clean_untracked: bool) -> list[str]:
        """What is irrecoverably lost when running WITHOUT backup."""
        lost = []
        if self.modified:
            lost.append(f"{self.modified} modified file(s)")
        if self.untracked and clean_untracked:
            lost.append(f"{self.untracked} untracked file(s)")
        if self.main_unpushed:
            lost.append(f"{self.main_unpushed} local commit(s) on {BRANCH}")
        if self.detached and self.head_unpushed:
            lost.append(f"{self.head_unpushed} commit(s) on detached HEAD")
        return lost


@dataclass
class SyncResult:
    plan: RepoPlan
    ok: bool
    actions: list[str] = field(default_factory=list)
    error: str | None = None


def fetch(repo: Path) -> str | None:
    """Fetch the remote. Returns an error message or None."""
    try:
        if _git(repo, "remote", "get-url", REMOTE).returncode != 0:
            return None  # plan() reports the missing remote as the skip reason
        _git_ok(repo, "fetch", "--prune", REMOTE, timeout=180)
        return None
    except GitCommandError as exc:
        return str(exc)
    except subprocess.TimeoutExpired:
        return "git fetch timed out"


def plan(repo: Path, projects: list[str], fetch_error: str | None = None) -> RepoPlan:
    p = RepoPlan(repo=repo, projects=projects, fetch_error=fetch_error)
    try:
        if _git(repo, "remote", "get-url", REMOTE).returncode != 0:
            p.skip_reason = f"no remote '{REMOTE}'"
            return p
        target = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{REMOTE}/{BRANCH}^{{commit}}")
        if target.returncode != 0:
            p.skip_reason = f"{p.target} not found"
            return p
        p.target_commit = target.stdout.strip()

        branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        p.branch = branch.stdout.strip() if branch.returncode == 0 else None
        head = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
        p.commit = head.stdout.strip() if head.returncode == 0 else None

        for line in _git(repo, "status", "--porcelain").stdout.splitlines():
            if line.startswith("??"):
                p.untracked += 1
            elif line.strip():
                p.modified += 1

        if p.commit:
            p.head_unpushed = _count(repo, f"{p.target}..HEAD")
        if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{BRANCH}").returncode == 0:
            p.main_unpushed = _count(repo, f"{p.target}..refs/heads/{BRANCH}")
    except subprocess.TimeoutExpired:
        p.skip_reason = "git timed out"
    except (OSError, ValueError) as exc:
        p.skip_reason = str(exc)
    return p


def execute(p: RepoPlan, *, backup: bool, clean_untracked: bool, stamp: str) -> SyncResult:
    result = SyncResult(plan=p, ok=False)
    repo = p.repo
    try:
        if backup:
            if p.modified or (p.untracked and clean_untracked):
                args = ["stash", "push", "-m", f"pom-migrator backup {stamp}"]
                if p.untracked and clean_untracked:
                    args.insert(2, "--include-untracked")
                _git_ok(repo, *args)
                result.actions.append("stashed local changes (git stash list)")
            if p.detached and p.head_unpushed:
                name = f"{BACKUP_PREFIX}/{stamp}/detached-{p.commit[:8]}"
                _git_ok(repo, "branch", name, "HEAD")
                result.actions.append(f"saved detached HEAD as {name}")
            if p.main_unpushed:
                name = f"{BACKUP_PREFIX}/{stamp}/{BRANCH}"
                _git_ok(repo, "branch", name, f"refs/heads/{BRANCH}")
                result.actions.append(f"saved local {BRANCH} as {name}")

        _git_ok(repo, "checkout", "--force", "-B", BRANCH, p.target)
        _git_ok(repo, "branch", f"--set-upstream-to={p.target}", BRANCH)
        _git_ok(repo, "reset", "--hard", p.target)
        result.actions.append(f"{BRANCH} reset to {p.target} ({p.target_commit[:8]})")

        if clean_untracked:
            removed = [l for l in _git_ok(repo, "clean", "-fd").splitlines() if l.strip()]
            if removed:
                result.actions.append(f"removed {len(removed)} untracked item(s)")

        if p.branch and p.branch != BRANCH:
            note = f"branch '{p.branch}' kept"
            if p.head_unpushed:
                note += f" ({p.head_unpushed} commit(s) not in {p.target})"
            result.actions.append(note)

        head = _git_ok(repo, "rev-parse", "HEAD")
        if head != p.target_commit:
            raise GitCommandError(f"HEAD is {head[:8]}, expected {p.target_commit[:8]}")
        result.ok = True
    except GitCommandError as exc:
        result.error = str(exc)
    except subprocess.TimeoutExpired:
        result.error = "git timed out"
    return result
