"""Collect JUnit results (surefire / failsafe XML reports) for the modules of the parent."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .pomedit import PomEditError, parse_tree

REPORT_DIRS = ("surefire-reports", "failsafe-reports")


@dataclass
class TestCase:
    class_name: str
    name: str
    status: str                 # passed / failed / error / skipped
    time: float = 0.0
    message: str | None = None


@dataclass
class ClassResult:
    name: str
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    time: float = 0.0

    @property
    def passed(self) -> int:
        return self.tests - self.failures - self.errors - self.skipped


@dataclass
class ModuleResult:
    name: str
    path: Path
    classes: list[ClassResult] = field(default_factory=list)
    problems: list[TestCase] = field(default_factory=list)   # failed + error cases
    unreadable: list[str] = field(default_factory=list)

    def _sum(self, attr: str) -> int:
        return sum(getattr(c, attr) for c in self.classes)

    @property
    def tests(self) -> int:
        return self._sum("tests")

    @property
    def failures(self) -> int:
        return self._sum("failures")

    @property
    def errors(self) -> int:
        return self._sum("errors")

    @property
    def skipped(self) -> int:
        return self._sum("skipped")

    @property
    def passed(self) -> int:
        return self._sum("passed")

    @property
    def time(self) -> float:
        return sum(c.time for c in self.classes)

    @property
    def has_reports(self) -> bool:
        return bool(self.classes)


def list_modules(parent_dir: Path) -> list[str]:
    """Module directories declared in the parent's <modules> (nested aggregators are expanded)."""
    result: list[str] = []

    def walk(directory: Path, prefix: str) -> None:
        pom = directory / "pom.xml"
        try:
            root = parse_tree(pom.read_text(encoding="utf-8"))
        except (OSError, PomEditError):
            return
        modules = root.child("modules")
        if modules is None:
            return
        text = pom.read_text(encoding="utf-8")
        for node in modules.all("module"):
            name = text[node.inner_start:node.inner_end].strip()
            if not name:
                continue
            rel = f"{prefix}{name}".replace("\\", "/")
            result.append(rel)
            walk(directory / name, rel + "/")

    walk(parent_dir, "")
    return result


def _float(value: str | None) -> float:
    try:
        return float((value or "0").replace(",", ""))
    except ValueError:
        return 0.0


def _read_report(path: Path, module: ModuleResult) -> None:
    try:
        suite = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        module.unreadable.append(f"{path.name}: {exc}")
        return
    suites = [suite] if suite.tag == "testsuite" else suite.findall("testsuite")
    for s in suites:
        cls = ClassResult(
            name=s.get("name", path.stem.removeprefix("TEST-")),
            tests=int(s.get("tests", 0)),
            failures=int(s.get("failures", 0)),
            errors=int(s.get("errors", 0)),
            skipped=int(s.get("skipped", 0)),
            time=_float(s.get("time")),
        )
        module.classes.append(cls)
        for case in s.findall("testcase"):
            for tag, status in (("failure", "failed"), ("error", "error")):
                problem = case.find(tag)
                if problem is not None:
                    message = (problem.get("message") or problem.get("type") or "").strip()
                    module.problems.append(TestCase(
                        class_name=case.get("classname", cls.name),
                        name=case.get("name", "?"),
                        status=status,
                        time=_float(case.get("time")),
                        message=message.splitlines()[0] if message else None,
                    ))
                    break


def remove_old_reports(parent_dir: Path, modules: list[str]) -> None:
    """Maven skips `clean` for modules it never reaches (-fae), so stale reports would look current."""
    for rel in modules:
        for report_dir in REPORT_DIRS:
            shutil.rmtree(parent_dir / rel / "target" / report_dir, ignore_errors=True)


def collect(parent_dir: Path, modules: list[str]) -> list[ModuleResult]:
    results = []
    for rel in modules:
        directory = parent_dir / rel
        module = ModuleResult(name=rel, path=directory)
        for report_dir in REPORT_DIRS:
            for report in sorted((directory / "target" / report_dir).glob("TEST-*.xml")):
                _read_report(report, module)
        results.append(module)
    return results
