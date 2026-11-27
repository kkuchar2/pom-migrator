"""Interactive flow: reset every project's repository to a clean <remote>/<branch>."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from . import gitsync
from .gitsync import RepoPlan, SyncResult
from .scanner import Analysis

CONFIRM_WORD = "RESET"


def _group_repositories(console: Console, analysis: Analysis) -> dict[Path, list[str]]:
    """One entry per git repository (a monorepo holding several projects is handled once)."""
    repos: dict[Path, list[str]] = {}
    for project in analysis.projects:
        git = project.git
        if not git.is_repo or git.toplevel is None:
            console.print(f"[dim]• {project.name}: not a git repository, skipped[/]")
            continue
        repos.setdefault(git.toplevel, []).append(project.name)
    return repos


def _fetch_all(console: Console, repos: dict[Path, list[str]]) -> dict[Path, str | None]:
    """Fetch without a storm of credential prompts.

    A credential helper (e.g. Git Credential Manager on Windows) may open one sign-in window
    per git process. So per remote host one repository is fetched alone first; that prompts
    at most once and caches the credential. The rest of that host then runs in parallel,
    or one by one if the first fetch failed (so a cancelled sign-in can't open many windows).
    """
    by_host: dict[str | None, list[Path]] = {}
    for repo in repos:
        by_host.setdefault(gitsync.remote_host(repo), []).append(repo)

    errors: dict[Path, str | None] = {}
    for host, host_repos in by_host.items():
        if host is None:  # no remote: nothing to fetch, plan() reports the skip reason
            errors.update({repo: None for repo in host_repos})
            continue
        label = host
        first, rest = host_repos[0], host_repos[1:]
        with console.status(
            f"[cyan]Fetching {', '.join(repos[first])} ({label}) — sign in if asked…[/]", spinner="dots"
        ):
            errors[first] = gitsync.fetch(first)
        if not rest:
            continue
        if errors[first] is None:
            with console.status(f"[cyan]Fetching {len(rest)} more from {label}…[/]", spinner="dots"):
                with ThreadPoolExecutor(max_workers=min(8, len(rest))) as pool:
                    errors.update(zip(rest, pool.map(gitsync.fetch, rest)))
        else:
            console.print(f"[yellow]⚠ Fetch from {label} failed ({errors[first]}); continuing one at a time.[/]")
            for repo in rest:
                with console.status(f"[cyan]Fetching {', '.join(repos[repo])} ({label})…[/]", spinner="dots"):
                    errors[repo] = gitsync.fetch(repo)
    return errors


def _number(value: int, style: str) -> Text:
    return Text(str(value), style=style) if value else Text("0", style="dim")


def _render_plans(console: Console, plans: list[RepoPlan], clean_untracked: bool | None = None) -> None:
    table = Table(title=f"Reset to {gitsync.REMOTE}/{gitsync.BRANCH} — preview", title_justify="left",
                  title_style="bold", box=box.ROUNDED, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Repository / projects", style="bold")
    table.add_column("Current")
    table.add_column("Modified", justify="right")
    table.add_column("Untracked", justify="right")
    table.add_column(f"Local {gitsync.BRANCH} ahead", justify="right")
    table.add_column("HEAD unpushed", justify="right")
    table.add_column("Action")

    for i, p in enumerate(plans, 1):
        name = Text(", ".join(p.projects))
        if len(p.projects) > 1 or p.repo.name not in p.projects:
            name.append(f"\n{p.repo}", style="dim")
        if p.branch:
            current = Text(p.branch, style="green" if p.branch == gitsync.BRANCH else "yellow")
        elif p.commit:
            current = Text(f"detached {p.commit[:8]}", style="red")
        else:
            current = Text("—", style="dim")
        if p.skip_reason:
            action = Text(f"skip: {p.skip_reason}", style="dim red")
        elif p.up_to_date:
            action = Text("already clean", style="green")
        else:
            action = Text("reset", style="bold yellow")
        if p.fetch_error:
            action.append(f"\nfetch failed: {p.fetch_error}", style="red")
        table.add_row(
            str(i),
            name,
            current,
            _number(p.modified, "yellow"),
            _number(p.untracked, "yellow"),
            _number(p.main_unpushed, "bold red"),
            _number(p.head_unpushed, "magenta"),
            action,
        )
    console.print(table)


def _render_results(console: Console, results: list[SyncResult]) -> None:
    table = Table(title="Result", title_justify="left", title_style="bold", box=box.ROUNDED, header_style="bold")
    table.add_column("", width=2)
    table.add_column("Repository", style="bold")
    table.add_column("Details")
    for r in results:
        icon = Text("✔", style="green") if r.ok else Text("✖", style="bold red")
        details = Text("\n".join(r.actions))
        if r.error:
            if details:
                details.append("\n")
            details.append(r.error, style="red")
        table.add_row(icon, ", ".join(r.plan.projects), details)
    console.print(table)


def run(console: Console, analysis: Analysis) -> bool:
    """Returns True if any repository was changed (caller should re-scan)."""
    console.print(Panel(
        Text.assemble(
            ("DESTRUCTIVE: ", "bold red"),
            f"every project's repository will be switched to '{gitsync.BRANCH}' and hard-reset to "
            f"'{gitsync.REMOTE}/{gitsync.BRANCH}'.\nUncommitted changes and local commits on "
            f"'{gitsync.BRANCH}' are discarded unless backed up. Other local branches are not deleted.",
        ),
        border_style="red",
        expand=False,
    ))

    repos = _group_repositories(console, analysis)
    if not repos:
        console.print("[yellow]No git repositories found.[/]")
        return False

    fetch_errors = _fetch_all(console, repos)
    with console.status("[cyan]Inspecting repositories…[/]", spinner="dots"):
        plans = [gitsync.plan(repo, names, fetch_errors[repo]) for repo, names in repos.items()]

    _render_plans(console, plans)

    candidates = [p for p in plans if not p.skip_reason and not p.up_to_date]
    if not candidates:
        skipped = sum(1 for p in plans if p.skip_reason)
        clean = sum(1 for p in plans if p.up_to_date)
        console.print(f"[green]✔ Nothing to reset[/] ({clean} already clean, {skipped} skipped).")
        return False
    if any(p.fetch_error for p in candidates):
        console.print("[yellow]⚠ Some fetches failed: those repositories would be reset to their last known remote state.[/]")

    exclude = Prompt.ask("[cyan]Exclude repositories?[/] numbers from the # column, e.g. 2,5 (empty = none)", default="")
    excluded: set[int] = set()
    for part in exclude.replace(" ", "").split(","):
        if part.isdigit():
            excluded.add(int(part))
    selected = [p for i, p in enumerate(plans, 1) if p in candidates and i not in excluded]
    if not selected:
        console.print("[dim]Nothing selected.[/]")
        return False

    clean_untracked = Confirm.ask("Also delete untracked files ([bold]git clean -fd[/], ignored files are kept)?", default=True)
    backup = Confirm.ask("Back up local work first (stash + backup branches)?", default=True)

    if not backup:
        losses = [(p, p.losses(clean_untracked)) for p in selected]
        losses = [(p, lost) for p, lost in losses if lost]
        if losses:
            console.print("[bold red]Without backup the following is lost permanently:[/]")
            for p, lost in losses:
                console.print(f"  [red]• {', '.join(p.projects)}: {', '.join(lost)}[/]")

    console.print(
        f"\n[bold]{len(selected)}[/] repositories will be reset"
        f" ({'with' if backup else '[bold red]WITHOUT[/]'} backup"
        f"{', untracked files deleted' if clean_untracked else ''})."
    )
    if Prompt.ask(f"Type [bold red]{CONFIRM_WORD}[/] to continue (anything else cancels)", default="") != CONFIRM_WORD:
        console.print("[dim]Cancelled. Nothing was changed.[/]")
        return False

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results: list[SyncResult] = []
    for p in selected:
        with console.status(f"[cyan]Resetting {', '.join(p.projects)}…[/]", spinner="dots"):
            results.append(gitsync.execute(p, backup=backup, clean_untracked=clean_untracked, stamp=stamp))
    _render_results(console, results)

    if backup and any(r.ok and len(r.actions) > 1 for r in results):
        console.print(
            "[dim]Recover: [bold]git stash list[/] / [bold]git stash pop[/] for changes, "
            f"[bold]git branch --list '{gitsync.BACKUP_PREFIX}/{stamp}/*'[/] for commits.[/]"
        )
    return True
