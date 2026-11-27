"""Read-only git inspection (never fetches, never writes)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_TIMEOUT_SECONDS = 15
REMOTE = "origin"

PROVIDER_AZURE = "Azure DevOps"
PROVIDER_GITHUB = "GitHub"
PROVIDER_LOCAL = "local path"
PROVIDER_OTHER = "other"


def host_of(url: str) -> str:
    """Host of a remote URL: https://host/..., ssh://user@host:port/..., user@host:path, or 'local'."""
    url = url.strip()
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if scheme.lower() == "file":
            return "local"
        authority = rest.split("/", 1)[0]
        return authority.rsplit("@", 1)[-1].split(":", 1)[0].lower() or "local"
    if ":" in url and not url.startswith(("/", ".")) and not (len(url) > 1 and url[1] == ":"):
        return url.split(":", 1)[0].rsplit("@", 1)[-1].lower() or "local"  # scp-like: git@host:org/repo
    return "local"  # filesystem path (incl. C:\\...)


def redact_url(url: str) -> str:
    """Hide credentials embedded in a URL: https://user:TOKEN@host/... -> https://user:***@host/..."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    authority, sep, path = rest.partition("/")
    if "@" not in authority:
        return url
    userinfo, host = authority.rsplit("@", 1)
    user = userinfo.split(":", 1)[0]
    masked = f"{user}:***" if ":" in userinfo else userinfo
    return f"{scheme}://{masked}@{host}{sep}{path}"


def provider_of(host: str) -> str:
    if host in ("dev.azure.com", "ssh.dev.azure.com", "vs-ssh.visualstudio.com") or host.endswith(".visualstudio.com"):
        return PROVIDER_AZURE
    if host == "github.com" or host.endswith((".github.com", ".ghe.com")):
        return PROVIDER_GITHUB
    if host == "local":
        return PROVIDER_LOCAL
    return PROVIDER_OTHER


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
    remote_url: str | None = None    # URL of REMOTE (None = no such remote)
    error: str | None = None

    @property
    def remote_display(self) -> str | None:
        return redact_url(self.remote_url) if self.remote_url else None

    @property
    def remote_host(self) -> str | None:
        return host_of(self.remote_url) if self.remote_url else None

    @property
    def provider(self) -> str | None:
        host = self.remote_host
        return provider_of(host) if host else None

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

        remote_out = _git(directory, "remote", "get-url", REMOTE)
        remote_url = remote_out.stdout.strip() if remote_out.returncode == 0 else None

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
            remote_url=remote_url,
        )
    except FileNotFoundError:
        return GitInfo(is_repo=False, error="git executable not found")
    except subprocess.TimeoutExpired:
        return GitInfo(is_repo=False, error="git timed out")
    except (OSError, ValueError) as exc:
        return GitInfo(is_repo=False, error=str(exc))
