"""Read-only parsing of Maven pom.xml files."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

_PROPERTY_REF = re.compile(r"\$\{([^}]+)\}")


class PomError(Exception):
    """Raised when a pom.xml cannot be read or is not a Maven POM."""


@dataclass(frozen=True)
class Coordinates:
    group_id: str | None
    artifact_id: str | None
    version: str | None

    @property
    def ga(self) -> tuple[str | None, str | None]:
        return (self.group_id, self.artifact_id)

    @property
    def ga_str(self) -> str:
        return f"{self.group_id or '?'}:{self.artifact_id or '?'}"

    def __str__(self) -> str:
        return f"{self.ga_str}:{self.version or '?'}"


@dataclass(frozen=True)
class Dependency:
    group_id: str | None
    artifact_id: str | None
    version: str | None           # as written in the POM (None = managed elsewhere)
    resolved_version: str | None  # after substituting known properties
    scope: str | None
    type: str | None

    @property
    def ga(self) -> tuple[str | None, str | None]:
        return (self.group_id, self.artifact_id)


@dataclass(frozen=True)
class PomInfo:
    path: Path
    coordinates: Coordinates          # effective: groupId/version inherited from <parent> if absent
    declared_version: str | None      # <version> literally present in this POM
    packaging: str
    parent: Coordinates | None
    parent_relative_path: str | None
    properties: dict[str, str]
    dependencies: tuple[Dependency, ...]
    managed_dependencies: tuple[Dependency, ...]
    modules: tuple[str, ...]


def _local(tag: object) -> str:
    # Comments/processing instructions have non-string tags.
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _children(element: ET.Element | None, name: str) -> list[ET.Element]:
    if element is None:
        return []
    return [child for child in element if _local(child.tag) == name]


def _text(element: ET.Element | None, name: str) -> str | None:
    child = _child(element, name)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


class _Resolver:
    def __init__(self, values: dict[str, str]):
        self._values = values

    def __call__(self, value: str | None) -> str | None:
        if value is None:
            return None
        for _ in range(10):  # bounded: guards against cyclic properties
            new = _PROPERTY_REF.sub(lambda m: self._values.get(m.group(1), m.group(0)), value)
            if new == value:
                break
            value = new
        return value


def _dependencies(container: ET.Element | None, resolve: _Resolver) -> tuple[Dependency, ...]:
    result = []
    for dep in _children(container, "dependency"):
        version = _text(dep, "version")
        result.append(
            Dependency(
                group_id=resolve(_text(dep, "groupId")),
                artifact_id=resolve(_text(dep, "artifactId")),
                version=version,
                resolved_version=resolve(version),
                scope=_text(dep, "scope"),
                type=_text(dep, "type"),
            )
        )
    return tuple(result)


def parse_pom(path: Path) -> PomInfo:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise PomError(f"invalid XML: {exc}") from None
    except OSError as exc:
        raise PomError(f"cannot read: {exc}") from None

    if _local(root.tag) != "project":
        raise PomError("root element is not <project>")

    parent_el = _child(root, "parent")
    parent = None
    if parent_el is not None:
        parent = Coordinates(
            _text(parent_el, "groupId"),
            _text(parent_el, "artifactId"),
            _text(parent_el, "version"),
        )

    properties: dict[str, str] = {}
    properties_el = _child(root, "properties")
    for prop in properties_el if properties_el is not None else []:
        name = _local(prop.tag)
        if name:
            properties[name] = (prop.text or "").strip()

    group_id = _text(root, "groupId") or (parent.group_id if parent else None)
    artifact_id = _text(root, "artifactId")
    declared_version = _text(root, "version")
    version = declared_version or (parent.version if parent else None)

    values = dict(properties)
    builtins = {
        "project.groupId": group_id,
        "project.artifactId": artifact_id,
        "project.version": version,
        "project.parent.groupId": parent.group_id if parent else None,
        "project.parent.artifactId": parent.artifact_id if parent else None,
        "project.parent.version": parent.version if parent else None,
    }
    values.update({k: v for k, v in builtins.items() if v is not None})
    resolve = _Resolver(values)

    dep_mgmt = _child(root, "dependencyManagement")
    return PomInfo(
        path=path,
        coordinates=Coordinates(resolve(group_id), resolve(artifact_id), resolve(version)),
        declared_version=declared_version,
        packaging=_text(root, "packaging") or "jar",
        parent=parent,
        parent_relative_path=_text(parent_el, "relativePath"),
        properties=properties,
        dependencies=_dependencies(_child(root, "dependencies"), resolve),
        managed_dependencies=_dependencies(_child(dep_mgmt, "dependencies"), resolve),
        modules=tuple(m.text.strip() for m in _children(_child(root, "modules"), "module") if m.text),
    )
