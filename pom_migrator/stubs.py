"""Verify contract stubs (Spring Cloud Contract `*-stubs.jar`) produced by the modules of the parent,
and find stub references (`stubrunner.ids`, `@AutoConfigureStubRunner`) that pin a version."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .pom import parse_pom

STUB_PLUGIN = "spring-cloud-contract-maven-plugin"
CLASSIFIER = "stubs"
_SOURCE_SUFFIXES = {".java", ".kt", ".groovy", ".yml", ".yaml", ".properties"}
_STUB_HINT = re.compile(r"stubrunner|StubRunner", re.I)
_ID = re.compile(r"[\w.\-]+(?::[\w.\-+]*){1,4}")
_INNER = re.compile(r"^META-INF/([^/]+)/([^/]+)/([^/]+)/(mappings|contracts)/(.+[^/])$")


@dataclass
class StubCheck:
    module: str
    group_id: str | None
    artifact_id: str | None
    version: str | None                   # version the module was built with
    expected: str | None = None           # why stubs are expected (None = not expected)
    jar: Path | None = None
    jar_version: str | None = None
    inner: set[tuple[str, str, str]] = field(default_factory=set)
    mappings: int = 0
    contracts: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def relevant(self) -> bool:
        return bool(self.expected or self.jar)

    @property
    def ok(self) -> bool:
        return self.jar is not None and not self.problems

    @property
    def coordinate(self) -> str:
        return f"{self.group_id}:{self.artifact_id}:{self.jar_version or self.version}:{CLASSIFIER}"


@dataclass
class StubReference:
    project: str
    file: Path                            # relative to the project directory
    line: int
    stub_id: str
    library: str
    version: str | None                   # None / '+' = latest


def _coordinates(module_dir: Path) -> tuple[str | None, str | None, str | None]:
    """groupId/artifactId/version as built: the flattened POM has ${revision} resolved."""
    for candidate in (module_dir / "target" / ".flattened-pom.xml", module_dir / ".flattened-pom.xml",
                      module_dir / "pom.xml"):
        if candidate.is_file():
            try:
                info = parse_pom(candidate)
            except Exception:  # noqa: BLE001 - a broken POM just means "unknown"
                continue
            c = info.coordinates
            version = c.version if c.version and "${" not in c.version else None
            return c.group_id, c.artifact_id, version
    return None, None, None


def _expected(module_dir: Path, parent_pom_text: str) -> str | None:
    try:
        pom = (module_dir / "pom.xml").read_text(encoding="utf-8")
    except OSError:
        return None
    if STUB_PLUGIN in pom:
        return f"{STUB_PLUGIN} in pom.xml"
    if f"<classifier>{CLASSIFIER}</classifier>" in pom:
        return "stubs classifier in pom.xml"
    if (module_dir / "src" / "test" / "resources" / "contracts").is_dir():
        return "src/test/resources/contracts exists"
    if _parent_applies_plugin(parent_pom_text):
        return f"{STUB_PLUGIN} in parent <build><plugins>"
    return None


def _parent_applies_plugin(text: str) -> bool:
    """Plugin declared in parent <build><plugins> (not only pluginManagement) runs in every module."""
    without_management = re.sub(r"<pluginManagement>.*?</pluginManagement>", "", text, flags=re.S)
    without_profiles = re.sub(r"<profiles>.*?</profiles>", "", without_management, flags=re.S)
    return STUB_PLUGIN in without_profiles


def check_modules(parent_dir: Path, modules: list[str]) -> list[StubCheck]:
    try:
        parent_text = (parent_dir / "pom.xml").read_text(encoding="utf-8")
    except OSError:
        parent_text = ""
    checks = []
    for rel in modules:
        directory = parent_dir / rel
        group, artifact, version = _coordinates(directory)
        check = StubCheck(rel, group, artifact, version, expected=_expected(directory, parent_text))
        jars = sorted((directory / "target").glob(f"*-{CLASSIFIER}.jar"))
        if artifact:
            jars = [j for j in jars if j.name.startswith(f"{artifact}-")] or jars
        if len(jars) > 1:
            check.problems.append(f"{len(jars)} stub jars in target: {', '.join(j.name for j in jars)}")
        if jars:
            check.jar = jars[0]
            name = check.jar.name
            if artifact and name.startswith(f"{artifact}-"):
                check.jar_version = name[len(artifact) + 1:-len(f"-{CLASSIFIER}.jar")]
            _inspect(check)
        elif check.expected:
            check.problems.append("expected stubs, but no *-stubs.jar was built")
        if check.jar:
            if version and check.jar_version and check.jar_version != version:
                check.problems.append(f"jar version {check.jar_version} ≠ module version {version}")
            for g, a, v in sorted(check.inner):
                if version and v != version:
                    check.problems.append(f"META-INF/{g}/{a}/{v}: version ≠ {version}")
                if artifact and a != artifact:
                    check.problems.append(f"META-INF/{g}/{a}: artifactId ≠ {artifact}")
                if group and g != group:
                    check.problems.append(f"META-INF/{g}: groupId ≠ {group}")
            if not check.mappings:
                check.problems.append("no WireMock mappings inside the jar")
        checks.append(check)
    return checks


def _inspect(check: StubCheck) -> None:
    assert check.jar is not None
    try:
        with zipfile.ZipFile(check.jar) as jar:
            for name in jar.namelist():
                if m := _INNER.match(name):
                    check.inner.add((m.group(1), m.group(2), m.group(3)))
                    if m.group(4) == "mappings" and name.endswith(".json"):
                        check.mappings += 1
                    elif m.group(4) == "contracts":
                        check.contracts += 1
    except (zipfile.BadZipFile, OSError) as exc:
        check.problems.append(f"cannot read jar: {exc}")


def find_references(projects: list[tuple[str, Path]], libraries: set[str]) -> list[StubReference]:
    """Stub ids in test sources that name one of the libraries (by artifactId)."""
    refs: list[StubReference] = []
    for project, directory in projects:
        test_dir = directory / "src" / "test"
        if not test_dir.is_dir():
            continue
        for path in sorted(test_dir.rglob("*")):
            if path.suffix not in _SOURCE_SUFFIXES or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not _STUB_HINT.search(text):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for m in _ID.finditer(line):
                    ref = _parse_id(m.group(0), libraries)
                    if ref:
                        refs.append(StubReference(project, path.relative_to(directory), number, m.group(0), *ref))
    return refs


def _parse_id(stub_id: str, libraries: set[str]) -> tuple[str, str | None] | None:
    """Stub Runner notation: [groupId:]artifactId[:version][:classifier][:port]."""
    parts = stub_id.split(":")
    for index, part in enumerate(parts[:2]):
        if part in libraries:
            rest = parts[index + 1:]
            version = None
            if rest and rest[0] and rest[0] != CLASSIFIER and not rest[0].isdigit():
                version = rest[0]
            return part, version
    return None
