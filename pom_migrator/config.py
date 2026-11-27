"""Loading, validating and writing the workspace configuration (TOML)."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_NAME = "pom-migrator.toml"


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Workspace description.

    root         -- directory that contains all projects
    prefix       -- only directories whose name starts with it are scanned ("" = all)
    parent_dir   -- directory name of the parent project (relative to root)
    library_dirs -- directory names of library projects (relative to root)
    source       -- the file the config was loaded from (None if built in memory)
    warnings     -- non-fatal notes from loading (e.g. backslashes taken literally)
    """

    root: Path
    prefix: str
    parent_dir: str
    library_dirs: tuple[str, ...]
    source: Path | None = None
    warnings: tuple[str, ...] = ()


def _require_str(table: dict, key: str, *, required: bool = True, default: str = "") -> str:
    value = table.get(key, None)
    if value is None:
        if required:
            raise ConfigError(f"Missing required key 'workspace.{key}'")
        return default
    if not isinstance(value, str):
        raise ConfigError(f"'workspace.{key}' must be a string, got {type(value).__name__}")
    return value


# A single-line double-quoted string without inner quotes, e.g. "C:\Users\me\" (paths never contain ").
_DOUBLE_QUOTED = re.compile(r'"([^"\r\n]*)"')


def _backslashes_as_literal(text: str) -> tuple[str, list[str]]:
    """Turn double-quoted strings containing backslashes into TOML literal strings ('...').

    Lets users write Windows paths naturally: "C:\\Users\\me" is read as C:\\Users\\me.
    Returns the rewritten text and the original values that were rewritten.
    """
    rewritten: list[str] = []

    def replace(match: re.Match[str]) -> str:
        value = match.group(1)
        if "\\" not in value or "'" in value:
            return match.group(0)
        rewritten.append(value)
        return f"'{value}'"

    return _DOUBLE_QUOTED.sub(replace, text), rewritten


def _parse_toml(path: Path) -> tuple[dict, tuple[str, ...]]:
    """Parse strictly; on failure retry with backslashes in double quotes taken literally."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from None

    def lenient() -> tuple[dict, tuple[str, ...]] | None:
        lenient_text, rewritten = _backslashes_as_literal(text)
        if not rewritten:
            return None
        try:
            data = tomllib.loads(lenient_text)
        except tomllib.TOMLDecodeError:
            return None
        return data, tuple(f"Backslashes in \"{v}\" taken literally (TOML escapes ignored)" for v in rewritten)

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as strict_error:
        result = lenient()
        if result:
            return result
        message = f"Invalid TOML in {path}: {strict_error}"
        if "\\" in text:
            message += (
                "\nHint: for Windows paths use  root = 'C:\\Users\\me\\projects'"
                "\n      or  root = \"C:/Users/me/projects\"  (check the closing quote, too)"
            )
        raise ConfigError(message) from None

    # Valid TOML can still be wrong: "C:\temp\new" parses, but \t and \n become a tab and a newline.
    if _contains_control_chars(data):
        result = lenient()
        if result:
            return result
    return data, ()


def _contains_control_chars(value: object) -> bool:
    if isinstance(value, str):
        return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    if isinstance(value, dict):
        return any(_contains_control_chars(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_control_chars(v) for v in value)
    return False


def load_config(path: Path) -> Config:
    data, warnings = _parse_toml(path)

    workspace = data.get("workspace")
    if not isinstance(workspace, dict):
        raise ConfigError("Missing [workspace] table")

    root_raw = _require_str(workspace, "root")
    prefix = _require_str(workspace, "prefix", required=False)
    parent_dir = _require_str(workspace, "parent").strip().strip("/\\")
    if not parent_dir:
        raise ConfigError("'workspace.parent' must not be empty")

    libraries_raw = workspace.get("libraries", [])
    if not isinstance(libraries_raw, list) or not all(isinstance(x, str) for x in libraries_raw):
        raise ConfigError("'workspace.libraries' must be a list of strings")
    libraries = [x.strip().strip("/\\") for x in libraries_raw if x.strip()]

    duplicates = sorted({x for x in libraries if libraries.count(x) > 1})
    if duplicates:
        raise ConfigError(f"Duplicate entries in 'workspace.libraries': {', '.join(duplicates)}")
    if parent_dir in libraries:
        raise ConfigError(f"'{parent_dir}' is configured both as parent and as a library")

    root = Path(root_raw).expanduser()
    if not root.is_absolute():
        root = (path.parent / root).resolve()

    return Config(
        root=root,
        prefix=prefix,
        parent_dir=parent_dir,
        library_dirs=tuple(libraries),
        source=path.resolve(),
        warnings=warnings,
    )


def _toml_string(value: str) -> str:
    """Prefer TOML literal strings ('...'): no escaping, so Windows paths stay readable."""
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    return json.dumps(value)  # JSON string escaping is valid TOML basic-string escaping


def render_config(config: Config) -> str:
    """Serialize config to TOML."""
    libs = "".join(f"    {_toml_string(name)},\n" for name in config.library_dirs)
    return (
        "# pom-migrator workspace configuration\n"
        "\n"
        "[workspace]\n"
        "# Directory that contains all projects (absolute, ~, or relative to this file)\n"
        "# Windows: use single quotes ('C:\\Users\\me\\projects') or forward slashes\n"
        f"root = {_toml_string(str(config.root))}\n"
        "\n"
        "# Only directories starting with this prefix are scanned (\"\" = all)\n"
        f"prefix = {_toml_string(config.prefix)}\n"
        "\n"
        "# Directory name of the parent project\n"
        f"parent = {_toml_string(config.parent_dir)}\n"
        "\n"
        "# Directory names of library projects; every other project using parent is a service\n"
        "libraries = [\n"
        f"{libs}"
        "]\n"
    )


def write_config(config: Config, path: Path) -> None:
    path.write_text(render_config(config), encoding="utf-8")
