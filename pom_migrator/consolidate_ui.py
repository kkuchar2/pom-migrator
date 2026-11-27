"""Interactive flow: copy libraries into parent, rewrite POMs, verify with a Maven build."""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from . import consolidate, maven
from .consolidate import ConsolidationPlan
from .scanner import Analysis

LOG_FILE = Path("pom-migrator-build.log")


def _render_plan(console: Console, plan: ConsolidationPlan) -> None:
    table = Table(title="Libraries → parent modules", title_justify="left", title_style="bold",
                  box=box.ROUNDED, header_style="bold")
    table.add_column("Library", style="bold cyan", no_wrap=True)
    table.add_column("Copy to")
    table.add_column("Files", justify="right")
    table.add_column("pom.xml changes")
    for lib in plan.libraries:
        changes = Text("\n".join(lib.changes) if lib.changes else "none", style="" if lib.changes else "dim")
        if lib.skipped:
            changes.append(f"\nnot copied: {', '.join(lib.skipped)}", style="dim")
        table.add_row(lib.project.name, f"{plan.parent.name}/{lib.project.name}/", str(len(lib.files)), changes)
    console.print(table)

    parent = Table(title=f"Parent ({plan.parent.name}/pom.xml)", title_justify="left", title_style="bold",
                   box=box.ROUNDED, show_header=False)
    parent.add_column()
    for change in plan.parent_changes:
        parent.add_row(change)
    console.print(parent)

    for warning in plan.warnings:
        console.print(f"[yellow]⚠ {warning}[/]")
    for error in plan.errors:
        console.print(f"[bold red]✖ {error}[/]")


def _render_build(console: Console, result: maven.BuildResult) -> None:
    if result.modules:
        table = Table(box=box.SIMPLE_HEAD, header_style="bold", title="Build", title_justify="left", title_style="bold")
        table.add_column("Module")
        table.add_column("Result")
        table.add_column("Time", justify="right", style="dim")
        styles = {"SUCCESS": "green", "FAILURE": "bold red", "SKIPPED": "yellow"}
        for name, status, time in result.modules:
            table.add_row(name, Text(status, style=styles.get(status, "")), time)
        console.print(table)
    if result.success:
        console.print(f"[bold green]✔ BUILD SUCCESS[/] [dim]({result.total_time or '?'})[/]")
    else:
        console.print(f"[bold red]✖ BUILD FAILED[/] [dim](exit code {result.exit_code})[/]")
        for line in result.errors[:15]:
            console.print(f"  [red]{line}[/]")
    if result.log_file:
        console.print(f"[dim]Full log: {result.log_file.resolve()}[/]")


def run(console: Console, analysis: Analysis) -> bool:
    """Returns True if files were changed (caller should re-scan)."""
    if analysis.parent is None:
        console.print("[red]No parent project.[/]")
        return False
    parent_dir = analysis.parent.path

    console.print(Panel(
        f"Copies every library into [bold]{parent_dir}[/] as a Maven module and rewrites the POMs\n"
        "(CI-friendly [bold]${revision}[/] versioning + flatten-maven-plugin). "
        "Source library repositories are [bold]not[/] modified.\n"
        "Changes are made on a new git branch in the parent and are not committed.",
        title="[bold]Consolidate libraries into parent[/]", border_style="cyan", expand=False,
    ))

    with console.status("[cyan]Planning…[/]", spinner="dots"):
        plan = consolidate.build_plan(analysis)
    _render_plan(console, plan)
    if not plan.ok:
        console.print("[red]Fix the errors above and re-scan. Nothing was changed.[/]")
        return False

    branch = None
    if analysis.parent.git.is_repo:
        while True:
            branch = Prompt.ask("[cyan]Create branch in parent[/]", default=consolidate.DEFAULT_BRANCH).strip()
            if branch and not consolidate.branch_exists(parent_dir, branch):
                break
            console.print(f"[red]Branch '{branch}' already exists or is empty, choose another name.[/]")

    if not Confirm.ask(f"Copy {len(plan.libraries)} libraries and modify POMs now?", default=False):
        console.print("[dim]Cancelled. Nothing was changed.[/]")
        return False

    previous = None
    try:
        if branch:
            previous = consolidate.create_branch(parent_dir, branch)
            console.print(f"[green]✔[/] switched to new branch [bold]{branch}[/]")
        with console.status("[cyan]Copying and rewriting…[/]", spinner="dots"):
            log = consolidate.execute(plan)
        for line in log:
            console.print(f"[green]✔[/] {line}")
    except (OSError, RuntimeError) as exc:
        console.print(f"[bold red]✖ {exc}[/]")
        if branch and Confirm.ask("Revert partial changes?", default=True):
            for line in consolidate.revert(parent_dir, previous, branch, [lib.target for lib in plan.libraries]):
                console.print(f"  {line}")
        return True

    if not Confirm.ask("\nVerify now with a Maven build ([bold]mvn clean verify[/])?", default=True):
        _next_steps(console, parent_dir, branch, plan)
        return True
    skip_tests = Confirm.ask("Skip tests?", default=True)
    with console.status("[cyan]Maven: starting…[/]", spinner="dots") as status:
        result = maven.run_build(parent_dir, skip_tests=skip_tests, log_file=LOG_FILE,
                                 on_progress=lambda msg: status.update(f"[cyan]Maven: building {msg}…[/]"))
    _render_build(console, result)

    if not result.success and branch and Confirm.ask(
        f"Revert all changes (reset parent, back to '{previous}', delete '{branch}')?", default=False
    ):
        for line in consolidate.revert(parent_dir, previous, branch, [lib.target for lib in plan.libraries]):
            console.print(f"  {line}")
        return True
    _next_steps(console, parent_dir, branch, plan)
    return True


def _next_steps(console: Console, parent_dir: Path, branch: str | None, plan: ConsolidationPlan) -> None:
    lines = [
        f"Review: [bold]git -C {parent_dir} status[/] / [bold]git diff[/]",
        f"Local build: version [bold]{plan.local_version}[/] (from the <revision> property)",
        "Pipeline: build with [bold]mvn -Drevision=<build number> deploy[/]",
    ]
    if branch:
        lines.append(f"Undo: [bold]git -C {parent_dir} reset --hard && git clean -fd[/], then switch back and delete '{branch}'")
    console.print(Panel("\n".join(lines), title="[bold]Next steps[/]", border_style="green", expand=False))
