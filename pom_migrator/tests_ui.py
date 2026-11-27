"""Interactive flow: run the tests of every module inside parent and show a results table."""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from . import maven, stubs
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


def _maven_seconds(value: str) -> float | None:
    """Maven reactor times: '1.050 s', '01:05 min', '1:02:03 h'."""
    number = value.split()[0].replace(",", ".") if value.strip() else ""
    try:
        parts = [float(x) for x in number.split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    if value.endswith(" h") and len(parts) < 3:
        seconds *= 60
    return seconds


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


def _render(console: Console, results: list[ModuleResult], reactor: dict[str, str],
            build_times: dict[str, str] | None = None) -> None:
    table = Table(title="Tests per module", title_justify="left", title_style="bold",
                  box=box.ROUNDED, header_style="bold", show_footer=True)
    total = {"tests": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    rows = []
    for module in results:
        display = _display_name(module.path) or module.name
        status = _status(module, reactor.get(display))
        rows.append((module, status, _maven_seconds((build_times or {}).get(display, ""))))
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
    if build_times is not None:
        build_total = sum(b for _, _, b in rows if b is not None)
        table.add_column("Build", justify="right", style="dim", footer=_seconds(build_total))
    table.add_column("Test time", justify="right", style="dim", footer=_seconds(total["time"]))
    for module, status, build_time in rows:
        has = module.has_reports
        table.add_row(
            module.name, status,
            str(module.tests) if has else "–",
            num(module.passed, "green") if has else Text("–", style="dim"),
            num(module.failures, "bold red") if has else Text("–", style="dim"),
            num(module.errors, "bold red") if has else Text("–", style="dim"),
            num(module.skipped, "yellow") if has else Text("–", style="dim"),
            *([_seconds(build_time) if build_time is not None else ""] if build_times is not None else []),
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


def _render_stubs(console: Console, parent_dir: Path, modules: list[str], analysis: Analysis) -> None:
    checks = stubs.check_modules(parent_dir, modules)
    relevant = [c for c in checks if c.relevant]
    libraries = {c.artifact_id for c in checks if c.artifact_id}
    projects = [(m, parent_dir / m) for m in modules] + [(s.name, s.path) for s in analysis.services]
    refs = stubs.find_references(projects, libraries)
    if not relevant and not refs:
        console.print("[dim]Stubs: no module produces contract stubs and no stub references were found.[/]")
        return

    if relevant:
        versions = {c.version for c in checks if c.version}
        table = Table(title="Contract stubs", title_justify="left", title_style="bold",
                      box=box.ROUNDED, header_style="bold")
        table.add_column("Module", style="bold cyan", no_wrap=True)
        table.add_column("Result")
        table.add_column("Stub artifact (as published)")
        table.add_column("Mappings", justify="right")
        table.add_column("Contracts", justify="right")
        table.add_column("Details", overflow="fold")
        for c in relevant:
            if c.ok:
                result = Text("✔ ok", style="green")
            elif c.jar is None:
                result = Text("✖ missing", style="bold red")
            else:
                result = Text("✖ check", style="bold red")
            details = Text("\n".join(c.problems), style="red") if c.problems else Text(
                c.expected or "built without an explicit stubs setup", style="dim")
            table.add_row(c.module, result, c.coordinate if c.jar else Text("–", style="dim"),
                          str(c.mappings) if c.jar else "–", str(c.contracts) if c.jar else "–", details)
        console.print(table)
        bad = [c for c in relevant if not c.ok]
        if bad:
            console.print(f"[bold red]✖ {len(bad)} of {len(relevant)} stub modules have problems[/]")
        else:
            console.print(f"[bold green]✔ {len(relevant)} module(s) produce stubs, all with version "
                          f"{', '.join(sorted(versions)) or '?'}[/]")

    if refs:
        current = next((c.version for c in checks if c.version), None)
        producers = {c.artifact_id for c in checks if c.jar}
        table = Table(title="Stub references in tests (stubrunner.ids / @AutoConfigureStubRunner)",
                      title_justify="left", title_style="bold", box=box.SIMPLE_HEAD, header_style="bold")
        table.add_column("Project", style="cyan", no_wrap=True)
        table.add_column("File")
        table.add_column("Stub id")
        table.add_column("Version")
        for r in refs:
            if r.library not in producers:
                version = Text(f"✖ {r.library} produces no stubs", style="bold red")
            elif r.version in (None, "+"):
                version = Text("latest (+)", style="green")
            elif current and r.version == current:
                version = Text(r.version, style="green")
            else:
                version = Text(f"pinned {r.version}", style="yellow")
            table.add_row(r.project, f"{r.file.as_posix()}:{r.line}", r.stub_id, version)
        console.print(table)
        orphans = [r for r in refs if r.library not in producers]
        if orphans:
            console.print(f"[bold red]✖ {len(orphans)} reference(s) to stubs that no module in the parent builds "
                          f"({', '.join(sorted({r.library for r in orphans}))})[/]")
        pinned = [r for r in refs if r.library in producers and r.version not in (None, "+") and r.version != current]
        if pinned:
            console.print(f"[yellow]⚠ {len(pinned)} reference(s) pin an old stub version. After consolidation all stubs "
                          f"are published with the parent version; use '+' or the new version.[/]")


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
        "1. [bold]mvn clean package -DskipTests[/]  → clean build time (compile + package, no tests)\n"
        "2. [bold]mvn verify -fae -Dmaven.test.failure.ignore=true[/]  → runs the tests of every module\n"
        "Results come from target/surefire-reports and target/failsafe-reports. Sources are not modified.",
        title="[bold]Build time + test results per module[/]", border_style="cyan", expand=False,
    ))

    existing = any((parent_dir / m / "target" / d).is_dir() for m in modules
                   for d in ("surefire-reports", "failsafe-reports"))
    mode = "run"
    if existing:
        mode = Prompt.ask("[cyan]Run build + tests now, or show existing reports from the last build?[/]",
                          choices=["run", "existing", "cancel"], default="run")
    if mode == "cancel":
        return
    if mode == "existing":
        results = collect(parent_dir, modules)
        _render(console, results, {})
        _render_stubs(console, parent_dir, modules, analysis)
        _offer_classes(console, results)
        return

    with console.status("[cyan]Maven: clean build…[/]", spinner="dots") as status:
        build = maven.run_build(
            parent_dir, skip_tests=True, log_file=LOG_FILE, goals=("clean", "package"), extra_args=("-fae",),
            on_progress=lambda msg: status.update(f"[cyan]Maven: building {msg}…[/]"),
        )
    build_times = {name: time for name, _, time in build.modules}
    if build.success:
        console.print(f"[green]✔[/] clean build (no tests): [bold]{_seconds(build.wall_seconds)}[/]")
    else:
        console.print(f"[bold red]✖ Clean build failed[/] [dim]after {_seconds(build.wall_seconds)} "
                      f"(exit code {build.exit_code})[/]")
        for line in build.errors[:15]:
            console.print(f"  [red]{line}[/]")
        console.print(f"[dim]Full log: {LOG_FILE.resolve()}[/]")
        if not Confirm.ask("Run the tests of the modules that did build?", default=False):
            return

    remove_old_reports(parent_dir, modules)
    with console.status("[cyan]Maven: running tests…[/]", spinner="dots") as status:
        tests = maven.run_build(
            parent_dir, skip_tests=False, log_file=LOG_FILE, goals=("verify",), append_log=True,
            extra_args=("-fae", "-Dmaven.test.failure.ignore=true"),
            on_progress=lambda msg: status.update(f"[cyan]Maven: testing {msg}…[/]"),
        )
    console.print(f"[cyan]⏱[/] test run finished in [bold]{_seconds(tests.wall_seconds)}[/]")

    results = collect(parent_dir, modules)
    _render(console, results, {name: result for name, result, _ in tests.modules}, build_times)
    if not tests.success and build.success:
        console.print(f"[bold red]✖ Test run failed[/] [dim](exit code {tests.exit_code}) — non-test errors:[/]")
        for line in tests.errors[:15]:
            console.print(f"  [red]{line}[/]")

    executed = sum(m.time for m in results)
    timing = Table(show_header=False, box=None, padding=(0, 2))
    timing.add_column(style="bold")
    timing.add_column(justify="right")
    timing.add_column(style="dim")
    timing.add_row("Clean build", _seconds(build.wall_seconds), "mvn clean package -DskipTests (compile, test-compile, jars)")
    timing.add_row("Test run", _seconds(tests.wall_seconds), f"mvn verify, of which {_seconds(executed)} inside tests")
    timing.add_row("Total", _seconds(build.wall_seconds + tests.wall_seconds), "both runs; one mvn clean verify in CI takes about the same")
    console.print(Panel(timing, title="[bold]Timing[/]", border_style="cyan", expand=False))
    console.print(f"[dim]Full log (both runs): {LOG_FILE.resolve()}[/]")
    _render_stubs(console, parent_dir, modules, analysis)
    _offer_classes(console, results)


def _offer_classes(console: Console, results: list[ModuleResult]) -> None:
    if any(m.classes for m in results) and Confirm.ask("Show results per test class?", default=False):
        _render_classes(console, results)
