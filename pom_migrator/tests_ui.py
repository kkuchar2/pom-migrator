"""Interactive flow: run the tests of every module inside parent and show a results table."""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from . import maven
from .pomedit import PomEditError, parse_tree
from .scanner import Analysis
from .testreport import ModuleResult, collect, list_modules, remove_old_reports

LOG_FILE = Path("pom-migrator-test.log")


def _display_name(directory: Path) -> str | None:
    """What Maven prints in the reactor summary: <name>, else <artifactId>."""
    pom = directory / "pom.xml"
    try:
        text = pom.read_text(encoding="utf-8")
        root = parse_tree(text)
    except (OSError, PomEditError):
        return None
    for tag in ("name", "artifactId"):
        node = root.child(tag)
        if node is not None:
            value = text[node.inner_start:node.inner_end].strip()
            if value:
                return value
    return None


def _seconds(value: float) -> str:
    return f"{value:.1f}s" if value < 60 else f"{int(value // 60)}m {value % 60:.0f}s"


def _status(module: ModuleResult, reactor: str | None) -> Text:
    if reactor in ("FAILURE", "SKIPPED") and not module.has_reports:
        return Text("not run" if reactor == "SKIPPED" else "build failed", style="bold red" if reactor == "FAILURE" else "yellow")
    if not module.has_reports:
        return Text("no tests", style="dim")
    if module.failures or module.errors:
        return Text("✖ failed", style="bold red")
    if reactor == "FAILURE":
        return Text("build failed", style="bold red")
    return Text("✔ passed", style="green")


def _render(console: Console, results: list[ModuleResult], reactor: dict[str, str]) -> None:
    table = Table(title="Tests per module", title_justify="left", title_style="bold",
                  box=box.ROUNDED, header_style="bold", show_footer=True)
    total = {"tests": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    rows = []
    for module in results:
        status = _status(module, reactor.get(_display_name(module.path) or module.name))
        rows.append((module, status))
        for key in total:
            total[key] += getattr(module, key)

    def num(value: int, style: str) -> Text:
        return Text(str(value), style=style if value else "dim")

    table.add_column("Module", style="bold cyan", footer="Total", no_wrap=True)
    table.add_column("Result", footer="")
    table.add_column("Tests", justify="right", footer=str(total["tests"]))
    table.add_column("Passed", justify="right", footer=Text(str(total["passed"]), style="green"))
    table.add_column("Failed", justify="right", footer=num(total["failures"], "bold red"))
    table.add_column("Errors", justify="right", footer=num(total["errors"], "bold red"))
    table.add_column("Skipped", justify="right", footer=num(total["skipped"], "yellow"))
    table.add_column("Time", justify="right", style="dim", footer=_seconds(total["time"]))
    for module, status in rows:
        has = module.has_reports
        table.add_row(
            module.name, status,
            str(module.tests) if has else "–",
            num(module.passed, "green") if has else Text("–", style="dim"),
            num(module.failures, "bold red") if has else Text("–", style="dim"),
            num(module.errors, "bold red") if has else Text("–", style="dim"),
            num(module.skipped, "yellow") if has else Text("–", style="dim"),
            _seconds(module.time) if has else "",
        )
    console.print(table)

    problems = [(m, p) for m in results for p in m.problems]
    if problems:
        fail = Table(title="Failing tests", title_justify="left", title_style="bold red",
                     box=box.SIMPLE_HEAD, header_style="bold")
        fail.add_column("Module", style="cyan", no_wrap=True)
        fail.add_column("Test")
        fail.add_column("Type")
        fail.add_column("Message", overflow="fold")
        for module, case in problems[:50]:
            short_class = case.class_name.rsplit(".", 1)[-1]
            fail.add_row(module.name, f"{short_class}.{case.name}",
                         Text(case.status, style="red"), (case.message or "")[:200])
        console.print(fail)
        if len(problems) > 50:
            console.print(f"[dim]… and {len(problems) - 50} more[/]")

    for module in results:
        for problem in module.unreadable:
            console.print(f"[yellow]⚠ {module.name}: unreadable report {problem}[/]")

    failed = total["failures"] + total["errors"]
    if failed:
        console.print(f"[bold red]✖ {failed} of {total['tests']} tests failed[/]")
    elif total["tests"]:
        skipped = f" [dim]({total['skipped']} skipped)[/]" if total["skipped"] else ""
        console.print(f"[bold green]✔ all {total['passed']} tests passed[/]{skipped}")
    else:
        console.print("[yellow]No test reports found.[/]")


def _render_classes(console: Console, results: list[ModuleResult]) -> None:
    table = Table(title="Tests per class", title_justify="left", title_style="bold",
                  box=box.SIMPLE_HEAD, header_style="bold")
    for col, justify in (("Module", "left"), ("Class", "left"), ("Tests", "right"), ("Passed", "right"),
                         ("Failed", "right"), ("Errors", "right"), ("Skipped", "right"), ("Time", "right")):
        table.add_column(col, justify=justify)
    for module in results:
        for cls in module.classes:
            bad = cls.failures or cls.errors
            table.add_row(
                Text(module.name, style="cyan"),
                Text(cls.name.rsplit(".", 1)[-1], style="red" if bad else ""),
                str(cls.tests), str(cls.passed),
                Text(str(cls.failures), style="bold red" if cls.failures else "dim"),
                Text(str(cls.errors), style="bold red" if cls.errors else "dim"),
                Text(str(cls.skipped), style="yellow" if cls.skipped else "dim"),
                Text(_seconds(cls.time), style="dim"),
            )
    console.print(table)


def run(console: Console, analysis: Analysis) -> None:
    if analysis.parent is None:
        console.print("[red]No parent project.[/]")
        return
    parent_dir = analysis.parent.path
    modules = list_modules(parent_dir)
    if not modules:
        console.print(f"[yellow]{parent_dir.name}/pom.xml has no <modules>. "
                      "Consolidate the libraries first (option c).[/]")
        return

    console.print(Panel(
        f"Modules in [bold]{parent_dir.name}[/]: {', '.join(modules)}\n"
        "Runs [bold]mvn clean verify -fae -Dmaven.test.failure.ignore=true[/] so every module is tested,\n"
        "then reads target/surefire-reports and target/failsafe-reports. Sources are not modified.",
        title="[bold]Test results per module[/]", border_style="cyan", expand=False,
    ))

    existing = any((parent_dir / m / "target" / d).is_dir() for m in modules
                   for d in ("surefire-reports", "failsafe-reports"))
    mode = "run"
    if existing:
        mode = Prompt.ask("[cyan]Run tests now, or show existing reports from the last build?[/]",
                          choices=["run", "existing", "cancel"], default="run")
    if mode == "cancel":
        return

    reactor: dict[str, str] = {}
    build: maven.BuildResult | None = None
    if mode == "run":
        remove_old_reports(parent_dir, modules)
        with console.status("[cyan]Maven: starting…[/]", spinner="dots") as status:
            build = maven.run_build(
                parent_dir, skip_tests=False, log_file=LOG_FILE,
                on_progress=lambda msg: status.update(f"[cyan]Maven: testing {msg}…[/]"),
                extra_args=("-fae", "-Dmaven.test.failure.ignore=true"),
            )
        reactor = {name: result for name, result, _ in build.modules}

    results = collect(parent_dir, modules)
    _render(console, results, reactor)

    if build is not None:
        if not build.success:
            console.print(f"[bold red]✖ Build failed[/] [dim](exit code {build.exit_code}) — "
                          "compilation or other non-test errors:[/]")
            for line in build.errors[:15]:
                console.print(f"  [red]{line}[/]")
        console.print(f"[dim]Full log: {LOG_FILE.resolve()}[/]")

    if any(m.classes for m in results) and Confirm.ask("Show results per test class?", default=False):
        _render_classes(console, results)
