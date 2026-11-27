"""Run a Maven build and condense its output (reactor summary + errors)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BUILDING = re.compile(r"\[INFO\] Building (.+?) (\S+)\s+\[(\d+)/(\d+)\]")
_SUMMARY_ROW = re.compile(r"\[INFO\] (.+?) \.{2,}\s*(SUCCESS|FAILURE|SKIPPED)(?: \[\s*([\d.,:]+ ?\w*)\])?")
_RESULT = re.compile(r"\[INFO\] BUILD (SUCCESS|FAILURE)")
_TOTAL = re.compile(r"\[INFO\] Total time:\s+(.+)")


@dataclass
class BuildResult:
    command: list[str]
    success: bool
    modules: list[tuple[str, str, str]] = field(default_factory=list)  # (name, status, time)
    errors: list[str] = field(default_factory=list)
    total_time: str | None = None
    log_file: Path | None = None
    exit_code: int | None = None
    wall_seconds: float = 0.0


def find_maven(project_dir: Path) -> list[str] | None:
    """Prefer the project's Maven wrapper, else mvn on PATH."""
    wrapper = project_dir / ("mvnw.cmd" if os.name == "nt" else "mvnw")
    if wrapper.is_file():
        return [str(wrapper)]
    mvn = shutil.which("mvn")
    return [mvn] if mvn else None


def run_build(
    project_dir: Path,
    *,
    skip_tests: bool,
    log_file: Path,
    on_progress: Callable[[str], None] | None = None,
    goals: tuple[str, ...] = ("clean", "verify"),
    extra_args: tuple[str, ...] = (),
    append_log: bool = False,
) -> BuildResult:
    maven = find_maven(project_dir)
    if maven is None:
        return BuildResult(command=[], success=False, errors=["Maven not found (no mvnw in parent, no mvn on PATH)"])

    command = [*maven, "-B", "-ntp", "-f", str(project_dir / "pom.xml"), *goals, *extra_args]
    if skip_tests:
        command.append("-DskipTests")
    result = BuildResult(command=command, success=False, log_file=log_file)

    errors: list[str] = []
    in_summary = False
    started = time.monotonic()
    with log_file.open("a" if append_log else "w", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n\n")
        process = subprocess.Popen(
            command,
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "MAVEN_OPTS": os.environ.get("MAVEN_OPTS", "")},
        )
        assert process.stdout is not None
        for raw in process.stdout:
            log.write(raw)
            line = _ANSI.sub("", raw.rstrip())
            if m := _BUILDING.search(line):
                if on_progress:
                    on_progress(f"[{m.group(3)}/{m.group(4)}] {m.group(1)}")
            if "Reactor Summary" in line:
                in_summary = True
            elif in_summary and (m := _SUMMARY_ROW.search(line)):
                result.modules.append((m.group(1).strip(), m.group(2), (m.group(3) or "").strip()))
            if m := _RESULT.search(line):
                in_summary = False
                result.success = m.group(1) == "SUCCESS"
            if m := _TOTAL.search(line):
                result.total_time = m.group(1).strip()
            if line.startswith("[ERROR]") and len(errors) < 200:
                errors.append(line[len("[ERROR]"):].strip())
        result.exit_code = process.wait()
    result.wall_seconds = time.monotonic() - started

    result.success = result.success and result.exit_code == 0
    # Keep the informative part of Maven's error output, drop the generic help footer.
    footer = ("To see the full stack trace", "Re-run Maven", "For more information", "[Help", "-> [Help", "After correcting")
    result.errors = list(dict.fromkeys(e for e in errors if e and not e.startswith(footer)))[:25]
    return result
