"""Workspace analysis: classify projects into parent / libraries / services (read-only)."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .config import Config
from .gitinfo import GitInfo, read_git_info
from .pom import Dependency, PomError, PomInfo, parse_pom


class Role(str, Enum):
    PARENT = "parent"
    LIBRARY = "library"
    SERVICE = "service"


class Level(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Project:
    name: str
    path: Path
    role: Role
    pom: PomInfo
    git: GitInfo = field(default_factory=lambda: GitInfo(is_repo=False))
    library_dependencies: list[Dependency] = field(default_factory=list)

    @property
    def parent_version(self) -> str | None:
        return self.pom.parent.version if self.pom.parent else None


@dataclass
class Excluded:
    name: str
    path: Path
    reason: str


@dataclass
class Issue:
    level: Level
    message: str
    project: str | None = None


@dataclass
class Analysis:
    config: Config
    parent: Project | None = None
    libraries: list[Project] = field(default_factory=list)
    services: list[Project] = field(default_factory=list)
    excluded: list[Excluded] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def projects(self) -> list[Project]:
        head = [self.parent] if self.parent else []
        return head + self.libraries + self.services

    @property
    def children(self) -> list[Project]:
        return self.libraries + self.services

    def library_by_ga(self) -> dict[tuple[str | None, str | None], Project]:
        return {lib.pom.coordinates.ga: lib for lib in self.libraries}

    def issue(self, level: Level, message: str, project: str | None = None) -> None:
        self.issues.append(Issue(level, message, project))


ProgressCallback = Callable[[str], None]


def _candidate_dirs(config: Config, analysis: Analysis) -> list[Path]:
    root = config.root
    found = {
        p.name: p
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name.startswith(config.prefix)
    }
    # Explicitly configured projects are always considered, even if outside the prefix.
    for name in (config.parent_dir, *config.library_dirs):
        if name in found:
            continue
        path = root / name
        if path.is_dir():
            analysis.issue(Level.WARNING, f"'{name}' does not start with prefix '{config.prefix}'", name)
            found[name] = path
    return [found[name] for name in sorted(found)]


def analyze(config: Config, progress: ProgressCallback | None = None) -> Analysis:
    report = progress or (lambda _msg: None)
    analysis = Analysis(config=config)

    if not config.root.is_dir():
        analysis.issue(Level.ERROR, f"Workspace root does not exist or is not a directory: {config.root}")
        return analysis

    # --- parent -------------------------------------------------------------
    report("Reading parent POM")
    parent_path = config.root / config.parent_dir
    parent_pom_file = parent_path / "pom.xml"
    if not parent_pom_file.is_file():
        analysis.issue(Level.ERROR, f"Parent project has no pom.xml: {parent_pom_file}", config.parent_dir)
        return analysis
    try:
        parent_pom = parse_pom(parent_pom_file)
    except PomError as exc:
        analysis.issue(Level.ERROR, f"Cannot parse parent POM: {exc}", config.parent_dir)
        return analysis

    analysis.parent = Project(config.parent_dir, parent_path, Role.PARENT, parent_pom)
    parent_ga = parent_pom.coordinates.ga
    if parent_pom.packaging != "pom":
        analysis.issue(
            Level.WARNING,
            f"Parent packaging is '{parent_pom.packaging}', expected 'pom'",
            config.parent_dir,
        )

    # --- children -----------------------------------------------------------
    report("Scanning workspace")
    library_names = set(config.library_dirs)
    for path in _candidate_dirs(config, analysis):
        name = path.name
        if name == config.parent_dir:
            continue
        pom_file = path / "pom.xml"
        if not pom_file.is_file():
            analysis.excluded.append(Excluded(name, path, "no pom.xml"))
            continue
        try:
            pom = parse_pom(pom_file)
        except PomError as exc:
            analysis.excluded.append(Excluded(name, path, f"unreadable pom.xml ({exc})"))
            analysis.issue(Level.WARNING, f"Cannot parse pom.xml: {exc}", name)
            continue
        if pom.parent is None:
            analysis.excluded.append(Excluded(name, path, "no <parent>"))
            continue
        if pom.parent.ga != parent_ga:
            analysis.excluded.append(Excluded(name, path, f"<parent> is {pom.parent.ga_str}"))
            continue

        role = Role.LIBRARY if name in library_names else Role.SERVICE
        project = Project(name, path, role, pom)
        (analysis.libraries if role is Role.LIBRARY else analysis.services).append(project)

    # --- configured libraries that did not end up as libraries -----------------
    found_libraries = {lib.name for lib in analysis.libraries}
    excluded_by_name = {ex.name: ex for ex in analysis.excluded}
    for name in config.library_dirs:
        if name in found_libraries:
            continue
        if name in excluded_by_name:
            analysis.issue(
                Level.ERROR,
                f"Configured as library but excluded: {excluded_by_name[name].reason}",
                name,
            )
        else:
            analysis.issue(Level.ERROR, "Configured as library but directory not found", name)

    # --- library dependencies -------------------------------------------------
    libs_by_ga = analysis.library_by_ga()
    for project in analysis.children:
        project.library_dependencies = [
            dep for dep in project.pom.dependencies if dep.ga in libs_by_ga and dep.ga != project.pom.coordinates.ga
        ]

    _check_versions(analysis)
    for cycle in library_graph(analysis).cycles:
        analysis.issue(Level.ERROR, "Library dependency cycle: " + " → ".join(cycle))

    # --- git (parallel, read-only) -----------------------------------------------
    report("Reading git state")
    projects = analysis.projects
    with ThreadPoolExecutor(max_workers=min(8, len(projects) or 1)) as pool:
        for project, info in zip(projects, pool.map(lambda p: read_git_info(p.path), projects)):
            project.git = info
            if info.error:
                analysis.issue(Level.WARNING, f"git: {info.error}", project.name)
            elif not info.is_repo:
                analysis.issue(Level.INFO, "Not a git repository", project.name)
            elif info.detached:
                analysis.issue(Level.WARNING, f"Detached HEAD at {info.commit}", project.name)

    return analysis


@dataclass
class LibraryGraph:
    """Library -> library dependency graph."""

    edges: dict[str, list[tuple[str, Dependency]]]  # library name -> [(depends-on library name, dependency)]
    cycles: list[list[str]]
    build_order: list[str] | None                   # None when cycles make an order impossible

    @property
    def has_dependencies(self) -> bool:
        return any(self.edges.values())


def library_graph(analysis: Analysis) -> LibraryGraph:
    libs_by_ga = analysis.library_by_ga()
    names = [lib.name for lib in analysis.libraries]
    edges: dict[str, list[tuple[str, Dependency]]] = {name: [] for name in names}
    for lib in analysis.libraries:
        for dep in lib.library_dependencies:
            edges[lib.name].append((libs_by_ga[dep.ga].name, dep))

    # Cycle detection (DFS, colour marking).
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {name: WHITE for name in names}
    stack: list[str] = []
    cycles: list[list[str]] = []
    seen_cycles: set[frozenset[str]] = set()

    def visit(node: str) -> None:
        colour[node] = GREY
        stack.append(node)
        for target, _dep in edges[node]:
            if colour[target] == GREY:
                cycle = stack[stack.index(target):] + [target]
                key = frozenset(cycle)
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    cycles.append(cycle)
            elif colour[target] == WHITE:
                visit(target)
        stack.pop()
        colour[node] = BLACK

    for name in names:
        if colour[name] == WHITE:
            visit(name)

    # Topological order (dependencies first), stable by name.
    build_order: list[str] | None = None
    if not cycles:
        remaining = {name: {t for t, _ in edges[name]} for name in names}
        build_order = []
        while remaining:
            ready = sorted(n for n, deps in remaining.items() if not deps)
            build_order.extend(ready)
            for n in ready:
                del remaining[n]
            for deps in remaining.values():
                deps.difference_update(ready)

    return LibraryGraph(edges=edges, cycles=cycles, build_order=build_order)


def _check_versions(analysis: Analysis) -> None:
    assert analysis.parent is not None
    parent_version = analysis.parent.pom.coordinates.version
    for project in analysis.children:
        ref = project.parent_version
        if ref is None:
            analysis.issue(Level.WARNING, "<parent> has no <version>", project.name)
        elif "${" in ref:
            analysis.issue(Level.INFO, f"<parent> version is a property: {ref}", project.name)
        elif ref == parent_version:
            analysis.issue(
                Level.INFO,
                f"<parent> points to local version {ref} (not a pipeline build)",
                project.name,
            )

    parent_managed = {dep.ga: dep for dep in analysis.parent.pom.managed_dependencies}
    for project in analysis.children:
        for dep in project.library_dependencies:
            if dep.version is None and dep.ga not in parent_managed:
                analysis.issue(
                    Level.WARNING,
                    f"Uses {dep.artifact_id} without <version>, but parent does not manage it",
                    project.name,
                )
