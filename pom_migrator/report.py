"""Rendering of the analysis (rich console) and export (txt / html / json)."""

from __future__ import annotations

import io
import json
from collections import defaultdict
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from .gitinfo import GitInfo
from .pom import Dependency
from .scanner import Analysis, Level, Project, Role, library_graph

ROLE_STYLE = {Role.PARENT: "bold magenta", Role.LIBRARY: "bold cyan", Role.SERVICE: "bold green"}
LEVEL_STYLE = {Level.ERROR: "bold red", Level.WARNING: "yellow", Level.INFO: "dim"}
LEVEL_ICON = {Level.ERROR: "✖", Level.WARNING: "⚠", Level.INFO: "ℹ"}
MAIN_BRANCHES = {"main", "master"}


# --------------------------------------------------------------------------- helpers

def _role(role: Role) -> Text:
    return Text(role.value, style=ROLE_STYLE[role])


def _branch(git: GitInfo) -> Text:
    if git.error:
        return Text("git error", style="red")
    if not git.is_repo:
        return Text("no git", style="dim")
    if git.commit is None:
        return Text(f"{git.branch or '?'} (no commits)", style="yellow")
    if git.branch is None:
        return Text(f"detached @ {git.commit}", style="bold red")
    if git.branch in MAIN_BRANCHES:
        return Text(git.branch, style="bold green")
    if git.branch == "develop":
        return Text(git.branch, style="cyan")
    return Text(git.branch, style="yellow")


def _status(git: GitInfo) -> Text:
    if not git.is_repo:
        return Text("—", style="dim")
    if git.changes:
        return Text(f"● {git.changes} changed", style="yellow")
    return Text("✔ clean", style="green")


def _sync(git: GitInfo) -> Text:
    if not git.is_repo or git.branch is None:
        return Text("—", style="dim")
    if git.upstream is None:
        return Text("no upstream", style="dim")
    if not git.ahead and not git.behind:
        return Text("✔ in sync", style="green")
    parts = Text()
    if git.ahead:
        parts.append(f"↑{git.ahead} ", style="cyan")
    if git.behind:
        parts.append(f"↓{git.behind}", style="magenta")
    return parts


def _dep_version(dep: Dependency) -> Text:
    if dep.version is None:
        return Text("managed", style="green")
    if dep.resolved_version and dep.resolved_version != dep.version:
        return Text(f"{dep.resolved_version} ({dep.version})", style="yellow")
    return Text(dep.version, style="yellow")


def _table(title: str) -> Table:
    return Table(title=title, title_style="bold", title_justify="left", box=box.ROUNDED, header_style="bold")


# --------------------------------------------------------------------------- sections

def render_header(console: Console, analysis: Analysis) -> None:
    cfg = analysis.config
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column()
    grid.add_row("Config", str(cfg.source or "—"))
    grid.add_row("Root", str(cfg.root))
    grid.add_row("Prefix", repr(cfg.prefix) if cfg.prefix else "(none)")
    grid.add_row("Parent dir", cfg.parent_dir)
    grid.add_row("Library dirs", ", ".join(cfg.library_dirs) or "(none)")
    console.print(Panel(grid, title="[bold]Workspace[/]", border_style="blue", expand=False))


def render_summary(console: Console, analysis: Analysis) -> None:
    counts = defaultdict(int)
    for issue in analysis.issues:
        counts[issue.level] += 1
    graph = library_graph(analysis)

    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right")
    grid.add_column()
    if analysis.parent:
        grid.add_row(Text("parent", style=ROLE_STYLE[Role.PARENT]), str(analysis.parent.pom.coordinates))
    else:
        grid.add_row(Text("parent", style="bold red"), Text("NOT FOUND", style="bold red"))
    grid.add_row(Text(str(len(analysis.libraries)), style=ROLE_STYLE[Role.LIBRARY]), "libraries")
    grid.add_row(Text(str(len(analysis.services)), style=ROLE_STYLE[Role.SERVICE]), "services")
    grid.add_row(Text(str(len(analysis.excluded)), style="dim"), "excluded (not using parent)")
    lib_deps = Text("yes", style="bold yellow") if graph.has_dependencies else Text("no", style="bold green")
    grid.add_row(lib_deps, "libraries depending on other libraries")
    issues = Text()
    issues.append(f"{counts[Level.ERROR]} errors", style=LEVEL_STYLE[Level.ERROR] if counts[Level.ERROR] else "dim")
    issues.append("  ")
    issues.append(f"{counts[Level.WARNING]} warnings", style=LEVEL_STYLE[Level.WARNING] if counts[Level.WARNING] else "dim")
    issues.append("  ")
    issues.append(f"{counts[Level.INFO]} info", style="dim")
    grid.add_row(Text("issues", style="bold"), issues)
    console.print(Panel(grid, title="[bold]Summary[/]", border_style="blue", expand=False))


def render_projects(console: Console, analysis: Analysis, roles: set[Role] | None = None) -> None:
    table = _table("Projects")
    table.add_column("Role")
    table.add_column("Directory", style="bold", no_wrap=True)
    table.add_column("Artifact")
    table.add_column("Version")
    table.add_column("<parent> version")
    table.add_column("Branch")
    table.add_column("Working tree")
    table.add_column("Upstream")

    for project in analysis.projects:
        if roles and project.role not in roles:
            continue
        coords = project.pom.coordinates
        if project.role is Role.PARENT:
            parent_ref = Text(str(project.pom.parent) if project.pom.parent else "—", style="dim")
        else:
            parent_ref = Text(project.parent_version or "missing", style="cyan" if project.parent_version else "red")
        version = Text(coords.version or "?")
        if project.role is not Role.PARENT and project.pom.declared_version is None:
            version.stylize("dim")
            version.append(" (inherited)", style="dim italic")
        table.add_row(
            _role(project.role),
            project.name,
            coords.ga_str,
            version,
            parent_ref,
            _branch(project.git),
            _status(project.git),
            _sync(project.git),
        )
    console.print(table)


def render_branches(console: Console, analysis: Analysis) -> None:
    by_branch: dict[str, list[Project]] = defaultdict(list)
    for project in analysis.projects:
        git = project.git
        key = git.branch if git.branch else ("(detached)" if git.detached else "(no git)")
        by_branch[key].append(project)

    tree = Tree("[bold]Branches[/]")
    for branch in sorted(by_branch, key=lambda b: (b not in MAIN_BRANCHES, b)):
        style = "bold green" if branch in MAIN_BRANCHES else ("red" if branch.startswith("(") else "yellow")
        node = tree.add(Text(f"{branch}  ({len(by_branch[branch])})", style=style))
        for project in by_branch[branch]:
            line = Text()
            line.append(project.name, style="bold")
            line.append("  ")
            line.append_text(_role(project.role))
            if project.git.changes:
                line.append(f"  ● {project.git.changes} changed", style="yellow")
            node.add(line)
    console.print(tree)


def render_parent_versions(console: Console, analysis: Analysis) -> None:
    by_version: dict[str, list[Project]] = defaultdict(list)
    for project in analysis.children:
        by_version[project.parent_version or "(missing)"].append(project)

    table = _table("Which parent version libraries and services reference")
    table.add_column("<parent> version", style="cyan")
    table.add_column("#", justify="right")
    table.add_column("Projects")
    for version in sorted(by_version, reverse=True):
        projects = by_version[version]
        names = Text()
        for i, project in enumerate(projects):
            if i:
                names.append(", ")
            names.append(project.name, style=ROLE_STYLE[project.role].replace("bold ", ""))
        table.add_row(version, str(len(projects)), names)
    console.print(table)
    if len(by_version) > 1:
        console.print(Text(f"⚠ {len(by_version)} different parent versions in use (version drift)", style="yellow"))


def render_library_dependencies(console: Console, analysis: Analysis) -> None:
    graph = library_graph(analysis)
    if graph.has_dependencies:
        console.print(Text("Does any library depend on another library?  YES", style="bold yellow"))
    else:
        console.print(Text("Does any library depend on another library?  NO", style="bold green"))
        return

    table = _table("Library → library dependencies")
    table.add_column("Library", style="bold cyan")
    table.add_column("Depends on", style="cyan")
    table.add_column("Version")
    table.add_column("Scope")
    for name, targets in graph.edges.items():
        for i, (target, dep) in enumerate(targets):
            table.add_row(name if i == 0 else "", target, _dep_version(dep), dep.scope or "compile")
    console.print(table)

    for cycle in graph.cycles:
        console.print(Text("✖ cycle: " + " → ".join(cycle), style="bold red"))
    if graph.build_order:
        order = Text("Build order (dependencies first): ", style="bold")
        order.append(" → ".join(graph.build_order), style="cyan")
        console.print(order)


def render_library_usage(console: Console, analysis: Analysis) -> None:
    usage: dict[str, list[tuple[Project, Dependency]]] = {lib.name: [] for lib in analysis.libraries}
    libs_by_ga = analysis.library_by_ga()
    for project in analysis.children:
        for dep in project.library_dependencies:
            usage[libs_by_ga[dep.ga].name].append((project, dep))

    table = _table("Who uses which library")
    table.add_column("Library", style="bold cyan")
    table.add_column("Used by")
    table.add_column("Role")
    table.add_column("Version")
    for name, users in usage.items():
        if not users:
            table.add_row(name, Text("not used", style="dim"), "", "")
            continue
        for i, (project, dep) in enumerate(users):
            table.add_row(name if i == 0 else "", project.name, _role(project.role), _dep_version(dep))
        table.add_section()
    console.print(table)


def render_excluded(console: Console, analysis: Analysis) -> None:
    if not analysis.excluded:
        console.print(Text("No excluded projects.", style="dim"))
        return
    table = _table("Excluded (do not use parent)")
    table.add_column("Directory", style="bold")
    table.add_column("Reason", style="dim")
    for ex in analysis.excluded:
        table.add_row(ex.name, ex.reason)
    console.print(table)


def render_issues(console: Console, analysis: Analysis, min_level: Level = Level.INFO) -> None:
    order = [Level.ERROR, Level.WARNING, Level.INFO]
    shown = [i for i in analysis.issues if order.index(i.level) <= order.index(min_level)]
    if not shown:
        console.print(Text("✔ No issues.", style="green"))
        return
    table = _table("Issues")
    table.add_column("", width=2)
    table.add_column("Project", style="bold")
    table.add_column("Message")
    for issue in sorted(shown, key=lambda i: (order.index(i.level), i.project or "")):
        style = LEVEL_STYLE[issue.level]
        table.add_row(Text(LEVEL_ICON[issue.level], style=style), issue.project or "—", Text(issue.message, style=style))
    console.print(table)


def render_full(console: Console, analysis: Analysis) -> None:
    sections = [
        render_header,
        render_summary,
        render_projects,
        render_branches,
        render_parent_versions,
        render_library_dependencies,
        render_library_usage,
        render_excluded,
        render_issues,
    ]
    for section in sections:
        if analysis.parent is None and section not in (render_header, render_summary, render_issues):
            continue
        section(console, analysis)
        console.print()


# --------------------------------------------------------------------------- export

def _to_dict(analysis: Analysis) -> dict:
    graph = library_graph(analysis)

    def dep(d: Dependency) -> dict:
        return {
            "groupId": d.group_id,
            "artifactId": d.artifact_id,
            "version": d.version,
            "resolvedVersion": d.resolved_version,
            "scope": d.scope,
            "type": d.type,
        }

    def project(p: Project) -> dict:
        c = p.pom.coordinates
        return {
            "name": p.name,
            "path": str(p.path),
            "role": p.role.value,
            "groupId": c.group_id,
            "artifactId": c.artifact_id,
            "version": c.version,
            "declaredVersion": p.pom.declared_version,
            "packaging": p.pom.packaging,
            "parent": str(p.pom.parent) if p.pom.parent else None,
            "git": {
                "isRepo": p.git.is_repo,
                "branch": p.git.branch,
                "commit": p.git.commit,
                "changes": p.git.changes,
                "upstream": p.git.upstream,
                "ahead": p.git.ahead,
                "behind": p.git.behind,
                "toplevel": str(p.git.toplevel) if p.git.toplevel else None,
            },
            "libraryDependencies": [dep(d) for d in p.library_dependencies],
        }

    cfg = analysis.config
    return {
        "config": {
            "source": str(cfg.source) if cfg.source else None,
            "root": str(cfg.root),
            "prefix": cfg.prefix,
            "parent": cfg.parent_dir,
            "libraries": list(cfg.library_dirs),
        },
        "parent": project(analysis.parent) if analysis.parent else None,
        "libraries": [project(p) for p in analysis.libraries],
        "services": [project(p) for p in analysis.services],
        "excluded": [{"name": e.name, "path": str(e.path), "reason": e.reason} for e in analysis.excluded],
        "libraryGraph": {
            "hasLibraryToLibraryDependencies": graph.has_dependencies,
            "edges": {k: [t for t, _ in v] for k, v in graph.edges.items()},
            "cycles": graph.cycles,
            "buildOrder": graph.build_order,
        },
        "issues": [{"level": i.level.value, "project": i.project, "message": i.message} for i in analysis.issues],
    }


def export(analysis: Analysis, path: Path) -> None:
    """Write report to path; format chosen by extension (.json, .html, anything else = plain text)."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        path.write_text(json.dumps(_to_dict(analysis), indent=2) + "\n", encoding="utf-8")
        return
    recorder = Console(record=True, width=160, file=io.StringIO(), force_terminal=True)
    render_full(recorder, analysis)
    if suffix in (".html", ".htm"):
        recorder.save_html(str(path))
    else:
        recorder.save_text(str(path))
