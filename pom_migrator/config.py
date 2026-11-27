"""Loading, validating and writing the workspace configuration (TOML)."""

from __future__ import annotations

import json
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
    """

    root: Path
    prefix: str
    parent_dir: str
    library_dirs: tuple[str, ...]
    source: Path | None = None


def _require_str(table: dict, key: str, *, required: bool = True, default: str = "") -> str:
    value = table.get(key, None)
    if value is None:
        if required:
            raise ConfigError(f"Missing required key 'workspace.{key}'")
        return default
    if not isinstance(value, str):
        raise ConfigError(f"'workspace.{key}' must be a string, got {type(value).__name__}")
    return value


def load_config(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        message = f"Invalid TOML in {path}: {exc}"
        if "\\" in path.read_text(encoding="utf-8", errors="replace"):
            message += (
                "\nHint: inside double quotes TOML treats \\ as an escape character (e.g. \\U in C:\\Users)."
                "\n      For Windows paths use single quotes:  root = 'C:\\Users\\me\\projects'"
                "\n      or forward slashes:                  root = \"C:/Users/me/projects\""
            )
        raise ConfigError(message) from None
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from None

    workspace = data.get("workspace")
    if not isinstance(workspace, dict):
        raise ConfigError("Missing [workspace] table")

    root_raw = _require_str(workspace, "root")
    prefix = _require_str(workspace, "prefix", required=False)
    parent_dir = _require_str(workspace, "parent").strip().strip("/")
    if not parent_dir:
        raise ConfigError("'workspace.parent' must not be empty")

    libraries_raw = workspace.get("libraries", [])
    if not isinstance(libraries_raw, list) or not all(isinstance(x, str) for x in libraries_raw):
        raise ConfigError("'workspace.libraries' must be a list of strings")
    libraries = [x.strip().strip("/") for x in libraries_raw if x.strip()]

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
