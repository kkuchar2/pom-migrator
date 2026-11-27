"""Interactive command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import DEFAULT_CONFIG_NAME, Config, ConfigError, load_config, write_config
from .report import (
    export,
    render_branches,
    render_excluded,
    render_full,
    render_header,
    render_issues,
    render_library_dependencies,
    render_library_usage,
    render_parent_versions,
    render_projects,
    render_remotes,
    render_summary,
)
from . import consolidate_ui, sync_ui
from .gitsync import BRANCH, REMOTE
from .scanner import Analysis, Level, Role, analyze

MENU = [
    ("1", "Summary", lambda c, a: (render_header(c, a), render_summary(c, a))),
    ("2", "All projects", lambda c, a: render_projects(c, a)),
    ("3", "Libraries only", lambda c, a: render_projects(c, a, {Role.PARENT, Role.LIBRARY})),
    ("4", "Services only", lambda c, a: render_projects(c, a, {Role.PARENT, Role.SERVICE})),
    ("5", "Branches", lambda c, a: render_branches(c, a)),
    ("g", "Git remotes (Azure DevOps / GitHub)", lambda c, a: render_remotes(c, a)),
    ("6", "Parent versions in use", lambda c, a: render_parent_versions(c, a)),
    ("7", "Library → library dependencies", lambda c, a: render_library_dependencies(c, a)),
    ("8", "Who uses which library", lambda c, a: render_library_usage(c, a)),
    ("9", "Excluded projects", lambda c, a: render_excluded(c, a)),
    ("i", "Issues", lambda c, a: render_issues(c, a)),
    ("f", "Full report", lambda c, a: render_full(c, a)),
]

BANNER = r"""
 ___  ___  __  __     __  __ _                 _
| _ \/ _ \|  \/  |___|  \/  (_)__ _ _ _ __ _| |_ ___ _ _
|  _/ (_) | |\/| |___| |\/| | / _` | '_/ _` |  _/ _ \ '_|
|_|  \___/|_|  |_|   |_|  |_|_\__, |_| \__,_|\__\___/_|
                              |___/
"""


def _parse_selection(text: str, size: int) -> list[int]:
    """Parse '1,3,5-7' into zero-based indexes."""
    result: list[int] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            if start > end:
                start, end = end, start
            numbers = range(start, end + 1)
        else:
            numbers = [int(part)]
        for n in numbers:
            if not 1 <= n <= size:
                raise ValueError(f"{n} is out of range 1–{size}")
            if n - 1 not in result:
                result.append(n - 1)
    return result


def _create_config_interactively(console: Console, target: Path) -> Config | None:
    console.print(Panel("Let's create a configuration file.", border_style="cyan", expand=False))

    while True:
        root = Path(Prompt.ask("[cyan]Directory containing all projects[/]", default=str(Path.cwd().parent))).expanduser()
        if root.is_dir():
            root = root.resolve()
            break
        console.print(f"[red]Not a directory:[/] {root}")

    prefix = Prompt.ask("[cyan]Common directory prefix[/] (empty = all)", default="")
    dirs = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name.startswith(prefix))
    if not dirs:
        console.print(f"[red]No directories in {root} start with {prefix!r}.[/]")
        return None

    table = Table(show_header=False, box=None, padding=(0, 2))
    for i, name in enumerate(dirs, 1):
        marker = "[green]pom.xml[/]" if (root / name / "pom.xml").is_file() else "[dim]no pom[/]"
        table.add_row(f"[bold]{i}[/]", name, marker)
    console.print(table)

    while True:
        choice = Prompt.ask("[cyan]Number of the parent project[/]")
        try:
            (parent_idx,) = _parse_selection(choice, len(dirs))
            break
        except ValueError as exc:
            console.print(f"[red]Invalid choice:[/] {exc or choice}")

    while True:
        choice = Prompt.ask("[cyan]Numbers of library projects[/] (e.g. 2,4,6-9, empty = none)", default="")
        try:
            lib_idx = [i for i in _parse_selection(choice, len(dirs)) if i != parent_idx]
            break
        except ValueError as exc:
            console.print(f"[red]Invalid choice:[/] {exc}")

    config = Config(
        root=root,
        prefix=prefix,
        parent_dir=dirs[parent_idx],
        library_dirs=tuple(dirs[i] for i in lib_idx),
        source=target.resolve(),
    )
    if Confirm.ask(f"Save configuration to [bold]{target}[/]?", default=True):
        write_config(config, target)
        console.print(f"[green]✔ Saved[/] {target}")
    return config


def _resolve_config(console: Console, path: Path, interactive: bool) -> Config | None:
    while True:
        try:
            config = load_config(path)
            for warning in config.warnings:
                console.print(f"[yellow]⚠ {warning}[/]")
            return config
        except ConfigError as exc:
            console.print(f"[bold red]✖ {exc}[/]")
            if not interactive:
                return None
            if not path.exists() and Confirm.ask("Create a new configuration interactively?", default=True):
                return _create_config_interactively(console, path)
            answer = Prompt.ask("[cyan]Path to configuration file[/] (empty = quit)", default="")
            if not answer:
                return None
            path = Path(answer).expanduser()


def _run_analysis(console: Console, config: Config) -> Analysis:
    with console.status("[cyan]Analyzing workspace…[/]", spinner="dots") as status:
        return analyze(config, progress=lambda msg: status.update(f"[cyan]{msg}…[/]"))


def _export_prompt(console: Console, analysis: Analysis) -> None:
    default = "pom-report.html"
    target = Path(Prompt.ask("[cyan]Export to[/] (.html / .txt / .json)", default=default)).expanduser()
    if target.exists() and not Confirm.ask(f"[yellow]{target} exists. Overwrite?[/]", default=False):
        return
    try:
        export(analysis, target)
    except OSError as exc:
        console.print(f"[red]✖ Export failed:[/] {exc}")
        return
    console.print(f"[green]✔ Report written to[/] {target.resolve()}")


def _interactive_loop(console: Console, config: Config, analysis: Analysis) -> None:
    keys = [key for key, _, _ in MENU] + ["e", "r", "s", "c", "q"]
    while True:
        console.print()
        menu = Table(show_header=False, box=None, padding=(0, 1))
        for key, label, _ in MENU:
            menu.add_row(f"[bold cyan]{key}[/]", label)
        menu.add_row("[bold cyan]e[/]", "Export report")
        menu.add_row("[bold cyan]r[/]", "Re-scan")
        menu.add_row("[bold red]s[/]", f"Reset all to clean {REMOTE}/{BRANCH}  [red](destructive)[/]")
        menu.add_row("[bold yellow]c[/]", "Consolidate libraries into parent  [yellow](modifies parent)[/]")
        menu.add_row("[bold cyan]q[/]", "Quit")
        console.print(Panel(menu, title="[bold]Menu[/]", border_style="cyan", expand=False))

        choice = Prompt.ask("[bold]Choose[/]", choices=keys, default="f", show_choices=False)
        console.print()
        if choice == "q":
            return
        if choice == "e":
            _export_prompt(console, analysis)
        elif choice == "r":
            analysis = _run_analysis(console, config)
            render_summary(console, analysis)
        elif choice == "c":
            if consolidate_ui.run(console, analysis):
                analysis = _run_analysis(console, config)
                render_summary(console, analysis)
        elif choice == "s":
            if sync_ui.run(console, analysis):
                analysis = _run_analysis(console, config)
                render_projects(console, analysis)
        else:
            next(fn for key, _, fn in MENU if key == choice)(console, analysis)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pom-migrator",
        description="Read-only analysis of a parent / libraries / services Maven workspace.",
    )
    parser.add_argument("-c", "--config", type=Path, default=Path(DEFAULT_CONFIG_NAME),
                        help=f"configuration file (default: ./{DEFAULT_CONFIG_NAME})")
    parser.add_argument("-n", "--non-interactive", action="store_true",
                        help="print the full report and exit (no prompts)")
    parser.add_argument("-e", "--export", type=Path, metavar="FILE",
                        help="also write the report to FILE (.html, .txt or .json)")
    parser.add_argument("--no-color", action="store_true", help="disable colors")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    interactive = not args.non_interactive and sys.stdin.isatty()
    console = Console(no_color=args.no_color, highlight=False)
    console.print(Text(BANNER, style="bold cyan"))
    console.print(Text("analysis is read-only — repositories change only via the explicit reset option\n", style="dim italic"))

    try:
        config = _resolve_config(console, args.config.expanduser(), interactive)
        if config is None:
            return 2

        analysis = _run_analysis(console, config)

        if args.export:
            export(analysis, args.export.expanduser())
            console.print(f"[green]✔ Report written to[/] {args.export.expanduser().resolve()}\n")

        if interactive:
            render_header(console, analysis)
            render_summary(console, analysis)
            render_issues(console, analysis, min_level=Level.ERROR)
            _interactive_loop(console, config, analysis)
        else:
            render_full(console, analysis)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
        return 130

    return 1 if any(i.level is Level.ERROR for i in analysis.issues) else 0
