"""Copy all libraries into the parent project as Maven modules and rewrite the POMs.

Result (CI-friendly versioning):
    parent/pom.xml      <version>${revision}</version>, <revision> property (local default),
                        <modules>, libraries in dependencyManagement at ${revision},
                        flatten-maven-plugin (published POMs get the real version)
    parent/<lib>/pom.xml  <parent><version>${revision}</version>, own <version> removed,
                        explicit versions of other libraries removed

Source library repositories are only read. Only the parent directory is modified.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .pomedit import Node, PomEditError, PomEditor, dependency_lines
from .scanner import Analysis, Project, library_graph

REVISION = "${revision}"
FLATTEN_VERSION = "1.10.0"
FLATTEN_PROPERTY = "flatten-maven-plugin.version"
DEFAULT_BRANCH = "consolidate-libraries"

# Never copied from a library (top-level names): VCS, build output, IDE, Maven wrapper.
SKIP_TOP_LEVEL = {".git", "target", ".idea", ".vscode", ".settings", ".mvn", "mvnw", "mvnw.cmd", ".flattened-pom.xml"}
SKIP_SUFFIXES = (".iml",)
CI_FILES = ("azure-pipelines.yml", "azure-pipelines.yaml", ".github", "Jenkinsfile", ".gitlab-ci.yml")


@dataclass
class LibraryPlan:
    project: Project
    target: Path
    files: list[str]
    skipped: list[str]
    changes: list[str]
    new_pom: str


@dataclass
class ConsolidationPlan:
    parent: Project
    libraries: list[LibraryPlan] = field(default_factory=list)
    parent_changes: list[str] = field(default_factory=list)
    parent_pom: str = ""
    local_version: str = ""
    gitignore_line: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------- helpers

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, timeout=60)


def _skip(rel: str) -> bool:
    top = rel.replace("\\", "/").split("/", 1)[0]
    return top in SKIP_TOP_LEVEL or rel.endswith(SKIP_SUFFIXES)


def list_library_files(project: Project) -> tuple[list[str], list[str]]:
    """Files to copy (relative, '/'-separated) and skipped top-level entries.

    In a git repository: tracked + untracked-but-not-ignored files (so target/, IDE files and
    everything in .gitignore stay behind). Otherwise: all files.
    """
    root = project.path
    names: list[str]
    result = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard") if project.git.is_repo else None
    if result is not None and result.returncode == 0:
        names = sorted({n for n in result.stdout.split("\0") if n and (root / n).is_file()})
    else:
        names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    files = [n for n in names if not _skip(n)]
    skipped = sorted({n.split("/", 1)[0] for n in names if _skip(n)})
    return files, skipped


class _Resolver:
    def __init__(self, editor: PomEditor):
        props = editor.root.child("properties")
        self.values = {c.name: editor.value(c) or "" for c in props.children} if props else {}
        group = editor.value(editor.root.child("groupId")) or editor.value(editor.root.path("parent", "groupId"))
        if group:
            self.values.setdefault("project.groupId", group)

    def __call__(self, value: str | None) -> str | None:
        if value is None:
            return None
        return re.sub(r"\$\{([^}]+)\}", lambda m: self.values.get(m.group(1), m.group(0)), value)


def _dependency_ga(editor: PomEditor, dep: Node, resolve: _Resolver) -> tuple[str | None, str | None]:
    return resolve(editor.value(dep.child("groupId"))), resolve(editor.value(dep.child("artifactId")))


class _Inserts:
    """Collects insertions so that several blocks at the same anchor become one splice (stable order)."""

    def __init__(self, editor: PomEditor):
        self.editor = editor
        self.before: dict[int, tuple[Node, list[str]]] = {}
        self.into: dict[int, tuple[Node, list[str]]] = {}

    def add_before(self, anchor: Node, lines: list[str]) -> None:
        node, existing = self.before.setdefault(anchor.start, (anchor, []))
        existing.extend(lines + [""])

    def add_into(self, parent: Node, lines: list[str]) -> None:
        node, existing = self.into.setdefault(parent.start, (parent, []))
        existing.extend(lines)

    def add_top_level(self, lines: list[str], *preferred_anchors: str) -> None:
        root = self.editor.root
        for name in preferred_anchors:
            anchor = root.child(name)
            if anchor is not None:
                self.add_before(anchor, lines)
                return
        self.add_into(root, [""] + lines)

    def flush(self) -> None:
        for anchor, lines in self.before.values():
            while lines and lines[-1] == "":
                lines.pop()
            self.editor.insert_before(anchor, lines)
        for parent, lines in self.into.values():
            self.editor.insert_child(parent, lines)


def _flatten_plugin(step: str) -> list[str]:
    s = step
    return [
        "<plugin>",
        f"{s}<groupId>org.codehaus.mojo</groupId>",
        f"{s}<artifactId>flatten-maven-plugin</artifactId>",
        f"{s}<version>${{{FLATTEN_PROPERTY}}}</version>",
        f"{s}<configuration>",
        f"{s*2}<updatePomFile>true</updatePomFile>",
        f"{s*2}<flattenMode>resolveCiFriendliesOnly</flattenMode>",
        f"{s}</configuration>",
        f"{s}<executions>",
        f"{s*2}<execution>",
        f"{s*3}<id>flatten</id>",
        f"{s*3}<phase>process-resources</phase>",
        f"{s*3}<goals>",
        f"{s*4}<goal>flatten</goal>",
        f"{s*3}</goals>",
        f"{s*2}</execution>",
        f"{s*2}<execution>",
        f"{s*3}<id>flatten-clean</id>",
        f"{s*3}<phase>clean</phase>",
        f"{s*3}<goals>",
        f"{s*4}<goal>clean</goal>",
        f"{s*3}</goals>",
        f"{s*2}</execution>",
        f"{s}</executions>",
        "</plugin>",
    ]


# --------------------------------------------------------------------------- parent POM

def _plan_parent(plan: ConsolidationPlan, analysis: Analysis, order: list[Project]) -> None:
    pom_file = plan.parent.path / "pom.xml"
    try:
        editor = PomEditor(pom_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, PomEditError) as exc:
        plan.errors.append(f"parent pom.xml: {exc}")
        return
    root, step, changes = editor.root, editor.step, plan.parent_changes
    resolve = _Resolver(editor)
    inserts = _Inserts(editor)
    properties = root.child("properties")
    defined = {c.name for c in properties.children} if properties else set()

    # version -> ${revision}
    version_node = root.child("version")
    version = editor.value(version_node)
    if version is None:
        plan.errors.append("parent pom.xml has no own <version>")
        return
    new_properties: list[str] = []
    if version == REVISION:
        plan.local_version = resolve.values.get("revision", "")
        if not plan.local_version:
            plan.errors.append("parent uses ${revision} but defines no <revision> property")
            return
    else:
        if "${" in version:
            plan.errors.append(f"parent <version> is an expression ({version}); expected a literal version")
            return
        plan.local_version = version
        editor.set_value(version_node, REVISION)
        changes.append(f"<version> {version} → {REVISION}")
        if "revision" not in defined:
            new_properties.append(f"<revision>{version}</revision>")
            changes.append(f"property <revision>{version}</revision> added (local default version)")
    if FLATTEN_PROPERTY not in defined:
        new_properties.append(f"<{FLATTEN_PROPERTY}>{FLATTEN_VERSION}</{FLATTEN_PROPERTY}>")

    # <modules>
    lib_names = [lp.project.name for lp in plan.libraries]
    modules = root.child("modules")
    existing_modules = {editor.value(m) for m in modules.all("module")} if modules else set()
    missing_modules = [n for n in [p.name for p in order] if n in lib_names and n not in existing_modules]
    if missing_modules:
        module_lines = [f"<module>{n}</module>" for n in missing_modules]
        if modules is not None:
            inserts.add_into(modules, module_lines)
        else:
            block = ["<modules>", *[step + line for line in module_lines], "</modules>"]
            inserts.add_top_level(block, "properties", "dependencyManagement", "dependencies", "build",
                                  "repositories", "distributionManagement", "profiles")
        changes.append(f"<modules> + {', '.join(missing_modules)}")

    # properties (after modules so a new <properties> block lands below <modules>)
    if new_properties:
        if properties is not None:
            inserts.add_into(properties, new_properties)
        else:
            inserts.add_top_level(["<properties>", *[step + p for p in new_properties], "</properties>"],
                                  "dependencyManagement", "dependencies", "build", "repositories",
                                  "distributionManagement", "profiles")

    # dependencyManagement entries for libraries
    dm = root.child("dependencyManagement")
    dm_deps = dm.child("dependencies") if dm else None
    to_add: list[Project] = []
    replaced_properties: set[str] = set()
    for lib in order:
        ga = lib.pom.coordinates.ga
        matches = [d for d in (dm_deps.all("dependency") if dm_deps else []) if _dependency_ga(editor, d, resolve) == ga]
        if not matches:
            to_add.append(lib)
            continue
        for dep in matches:
            v = dep.child("version")
            old = editor.value(v)
            if v is None:
                inserts.add_into(dep, [f"<version>{REVISION}</version>"])
                changes.append(f"dependencyManagement {ga[1]}: version added → {REVISION}")
            elif old != REVISION:
                editor.set_value(v, REVISION)
                shown = f"{resolve(old)} ({old})" if old and "${" in old else old
                changes.append(f"dependencyManagement {ga[1]}: {shown} → {REVISION}")
                m = re.fullmatch(r"\$\{([^}]+)\}", old or "")
                if m:
                    replaced_properties.add(m.group(1))
    if to_add:
        dep_lines = [line for lib in to_add for line in dependency_lines(
            lib.pom.coordinates.group_id or "", lib.pom.coordinates.artifact_id or "", REVISION, step)]
        if dm_deps is not None:
            inserts.add_into(dm_deps, dep_lines)
        elif dm is not None:
            inserts.add_into(dm, ["<dependencies>", *[step + l for l in dep_lines], "</dependencies>"])
        else:
            inserts.add_top_level(
                ["<dependencyManagement>", step + "<dependencies>", *[step * 2 + l for l in dep_lines],
                 step + "</dependencies>", "</dependencyManagement>"],
                "dependencies", "build", "repositories", "distributionManagement", "profiles")
        changes.append(f"dependencyManagement + {', '.join(l.pom.coordinates.artifact_id or '?' for l in to_add)} at {REVISION}")

    # flatten-maven-plugin
    build = root.child("build")
    plugins = build.child("plugins") if build else None
    has_flatten = any(editor.value(p.child("artifactId")) == "flatten-maven-plugin" for p in (plugins.all("plugin") if plugins else []))
    if not has_flatten:
        plugin = _flatten_plugin(step)
        if plugins is not None:
            inserts.add_into(plugins, plugin)
        elif build is not None:
            inserts.add_into(build, ["<plugins>", *[step + l for l in plugin], "</plugins>"])
        else:
            inserts.add_top_level(["<build>", step + "<plugins>", *[step * 2 + l for l in plugin], step + "</plugins>", "</build>"],
                                  "repositories", "distributionManagement", "profiles")
        changes.append(f"flatten-maven-plugin {FLATTEN_VERSION} added (replaces ${{revision}} in published POMs)")

    inserts.flush()
    try:
        plan.parent_pom = editor.result()
    except PomEditError as exc:
        plan.errors.append(f"parent pom.xml: {exc}")
        return

    # Version properties that no longer drive the libraries but may still be referenced by services.
    for prop in sorted(replaced_properties):
        users = [s.name for s in analysis.services if f"${{{prop}}}" in s.pom.path.read_text(encoding="utf-8", errors="replace")]
        if users:
            plan.warnings.append(
                f"property '{prop}' is kept but no longer used for the library; services still referencing "
                f"${{{prop}}} would pin the old version: {', '.join(users)}")

    gitignore = plan.parent.path / ".gitignore"
    content = gitignore.read_text(encoding="utf-8", errors="replace") if gitignore.exists() else ""
    if ".flattened-pom.xml" not in content.split():
        plan.gitignore_line = ".flattened-pom.xml"
        changes.append(".gitignore + .flattened-pom.xml")


# --------------------------------------------------------------------------- library POMs

def _plan_library(lib: Project, library_gas: set[tuple[str | None, str | None]], plan: ConsolidationPlan) -> LibraryPlan | None:
    try:
        editor = PomEditor(lib.pom.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, PomEditError) as exc:
        plan.errors.append(f"{lib.name}/pom.xml: {exc}")
        return None
    root, changes = editor.root, []

    parent = root.child("parent")
    parent_version = parent.child("version") if parent else None
    if parent_version is None:
        plan.errors.append(f"{lib.name}: <parent> has no <version>")
        return None
    old = editor.value(parent_version)
    if old != REVISION:
        editor.set_value(parent_version, REVISION)
        changes.append(f"<parent> version {old} → {REVISION}")
    relative = parent.child("relativePath")
    if relative is not None:
        editor.remove(relative)
        changes.append(f"<relativePath> {editor.value(relative) or '(empty)'} removed (default ../pom.xml)")

    own_version = root.child("version")
    if own_version is not None:
        editor.remove(own_version)
        changes.append(f"own <version> {editor.value(own_version)} removed (inherits parent version)")

    resolve = _Resolver(editor)
    containers = [root.child("dependencies"), root.path("dependencyManagement", "dependencies")]
    for container in containers:
        for dep in container.all("dependency") if container else []:
            ga = _dependency_ga(editor, dep, resolve)
            version = dep.child("version")
            if ga in library_gas and version is not None:
                editor.remove(version)
                changes.append(f"dependency {ga[1]}: <version> {editor.value(version)} removed (managed by parent)")

    try:
        new_pom = editor.result()
    except PomEditError as exc:
        plan.errors.append(f"{lib.name}/pom.xml: {exc}")
        return None

    files, skipped = list_library_files(lib)
    ci = [f for f in files if f.split("/", 1)[0] in CI_FILES]
    if ci:
        plan.warnings.append(f"{lib.name}: CI files copied, they no longer run from a subdirectory: {', '.join(sorted({c.split('/', 1)[0] for c in ci}))}")
    if lib.git.changes:
        plan.warnings.append(f"{lib.name}: {lib.git.changes} uncommitted change(s) in the source will be copied as they are")
    return LibraryPlan(lib, plan.parent.path / lib.name, files, skipped, changes, new_pom)


# --------------------------------------------------------------------------- plan / execute

def build_plan(analysis: Analysis) -> ConsolidationPlan:
    if analysis.parent is None:
        raise ValueError("analysis has no parent")
    plan = ConsolidationPlan(parent=analysis.parent)
    if not analysis.libraries:
        plan.errors.append("no libraries found")
        return plan

    parent_git = analysis.parent.git
    if parent_git.is_repo and parent_git.changes:
        plan.errors.append(f"parent has {parent_git.changes} uncommitted change(s): commit or stash them first")
    if not parent_git.is_repo:
        plan.warnings.append("parent is not a git repository: changes cannot be reviewed or reverted with git")

    graph = library_graph(analysis)
    by_name = {lib.name: lib for lib in analysis.libraries}
    order = [by_name[n] for n in graph.build_order] if graph.build_order else sorted(analysis.libraries, key=lambda l: l.name)
    if graph.cycles:
        plan.errors.append("library dependency cycle: " + " → ".join(graph.cycles[0]))

    library_gas = {lib.pom.coordinates.ga for lib in analysis.libraries}
    for lib in order:
        target = analysis.parent.path / lib.name
        if target.exists():
            plan.errors.append(f"{target} already exists")
            continue
        if analysis.parent.path in lib.path.parents:
            plan.errors.append(f"{lib.name} is already inside the parent directory")
            continue
        planned = _plan_library(lib, library_gas, plan)
        if planned:
            plan.libraries.append(planned)

    if plan.ok:
        _plan_parent(plan, analysis, order)
    return plan


def create_branch(parent: Path, branch: str) -> str | None:
    """Create and switch to branch. Returns the previous branch (or None if not a git repo)."""
    current = _git(parent, "symbolic-ref", "--quiet", "--short", "HEAD")
    previous = current.stdout.strip() if current.returncode == 0 else _git(parent, "rev-parse", "HEAD").stdout.strip()
    result = _git(parent, "switch", "-c", branch)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return previous


def branch_exists(parent: Path, branch: str) -> bool:
    return _git(parent, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def execute(plan: ConsolidationPlan) -> list[str]:
    """Copy libraries and write POMs. Returns a log of performed actions."""
    log = []
    for lib in plan.libraries:
        for rel in lib.files:
            dst = lib.target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lib.project.path / rel, dst)
        (lib.target / "pom.xml").write_text(lib.new_pom, encoding="utf-8", newline="")
        log.append(f"{lib.project.name}: {len(lib.files)} files copied, pom.xml updated")
    (plan.parent.path / "pom.xml").write_text(plan.parent_pom, encoding="utf-8", newline="")
    log.append("parent pom.xml updated")
    if plan.gitignore_line:
        gitignore = plan.parent.path / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8", errors="replace") if gitignore.exists() else ""
        sep = "" if not existing or existing.endswith("\n") else "\n"
        gitignore.write_text(existing + sep + plan.gitignore_line + "\n", encoding="utf-8")
        log.append(".gitignore updated")
    return log


def revert(parent: Path, previous: str | None, branch: str | None, created: list[Path] | None = None) -> list[str]:
    """Undo execute() on a parent that was clean before: remove copied modules, reset, clean,
    switch back, delete branch."""
    log = []
    for directory in created or []:
        if directory.is_dir() and parent in directory.parents:
            shutil.rmtree(directory)
            log.append(f"removed {directory.name}/")
    # clean twice: the first pass deletes .gitignore files we added, whose patterns
    # (e.g. .flattened-pom.xml) still protect build output during that same pass.
    for args in (("reset", "--hard", "-q"), ("clean", "-fdq"), ("clean", "-fdq")):
        result = _git(parent, *args)
        log.append(f"git {' '.join(args)}: {'ok' if result.returncode == 0 else result.stderr.strip()}")
    if previous and branch:
        result = _git(parent, "switch", "-q", previous) if not re.fullmatch(r"[0-9a-f]{40}", previous) else _git(parent, "switch", "-q", "--detach", previous)
        log.append(f"switched back to {previous}: {'ok' if result.returncode == 0 else result.stderr.strip()}")
        if result.returncode == 0:
            deleted = _git(parent, "branch", "-D", branch)
            log.append(f"branch {branch} deleted: {'ok' if deleted.returncode == 0 else deleted.stderr.strip()}")
    return log
