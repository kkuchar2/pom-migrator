"""Read-only git inspection (never fetches, never writes)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class GitInfo:
    is_repo: bool
    branch: str | None = None        # None when detached or not a repo
    commit: str | None = None        # short hash of HEAD
    changes: int = 0                 # entries in `git status --porcelain`
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    toplevel: Path | None = None     # repository root (differs from project dir in a monorepo)
    error: str | None = None

    @property
    def detached(self) -> bool:
        return self.is_repo and self.branch is None and self.commit is not None


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # --no-optional-locks: `git status` must not rewrite the index (keeps the scan read-only).
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


def read_git_info(directory: Path) -> GitInfo:
    try:
        inside = _git(directory, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return GitInfo(is_repo=False)

        toplevel_out = _git(directory, "rev-parse", "--show-toplevel")
        toplevel = Path(toplevel_out.stdout.strip()) if toplevel_out.returncode == 0 else None

        branch_out = _git(directory, "symbolic-ref", "--quiet", "--short", "HEAD")
        branch = branch_out.stdout.strip() if branch_out.returncode == 0 else None

        commit_out = _git(directory, "rev-parse", "--short", "HEAD")
        commit = commit_out.stdout.strip() if commit_out.returncode == 0 else None  # None: no commits yet

        status_out = _git(directory, "status", "--porcelain", "--", ".")
        changes = len([line for line in status_out.stdout.splitlines() if line.strip()])

        upstream = ahead = behind = None
        if branch:
            up = _git(directory, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
            if up.returncode == 0:
                upstream = up.stdout.strip()
                counts = _git(directory, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
                if counts.returncode == 0:
                    behind_s, ahead_s = counts.stdout.split()
                    behind, ahead = int(behind_s), int(ahead_s)

        return GitInfo(
            is_repo=True,
            branch=branch,
            commit=commit,
            changes=changes,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            toplevel=toplevel,
        )
    except FileNotFoundError:
        return GitInfo(is_repo=False, error="git executable not found")
    except subprocess.TimeoutExpired:
        return GitInfo(is_repo=False, error="git timed out")
    except (OSError, ValueError) as exc:
        return GitInfo(is_repo=False, error=str(exc))
