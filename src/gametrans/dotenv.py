"""Minimal `.env` loader.

Editing one file next to the app is much friendlier than `setx` for a
non-developer, and it keeps the key out of the shell history. Kept dependency
free - the format we need is a handful of `KEY=value` lines.

Precedence follows the usual convention: a variable already present in the real
environment wins over the file. `gametrans check` reports which source each key
came from, so that precedence never becomes a mystery.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

DEFAULT_ENV_FILENAME = ".env"

# Filled in by load_dotenv() so `check` can say where a key came from.
_loaded_from_file: Dict[str, str] = {}


def parse_env_text(text: str) -> Dict[str, str]:
    """Parse `.env` content into a dict.

    Supports `KEY=value`, `export KEY=value`, `#` comments, blank lines, and
    single or double quotes around the value.
    """
    values: Dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        key, separator, value = line.partition("=")
        if not separator:
            continue

        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # An unquoted trailing comment is not part of the value.
            hash_index = value.find(" #")
            if hash_index != -1:
                value = value[:hash_index].rstrip()

        values[key] = value

    return values


def find_env_file(config_path: Optional[str] = None) -> Optional[Path]:
    """Locate the `.env` to use: beside the config file, else in the cwd."""
    candidates = []
    if config_path:
        candidates.append(Path(config_path).resolve().parent / DEFAULT_ENV_FILENAME)
    candidates.append(Path.cwd() / DEFAULT_ENV_FILENAME)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_dotenv(config_path: Optional[str] = None, override: bool = False) -> Dict[str, str]:
    """Load `.env` into the process environment.

    Returns the variables that were actually applied. Existing environment
    variables are left alone unless `override` is set.
    """
    path = find_env_file(config_path)
    if path is None:
        return {}

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}

    applied: Dict[str, str] = {}
    for key, value in parse_env_text(text).items():
        if not override and os.environ.get(key):
            continue
        os.environ[key] = value
        applied[key] = value
        _loaded_from_file[key] = str(path)

    if applied:
        # Never log the values themselves.
        log.info("loaded %d key(s) from %s", len(applied), path)
    return applied


def source_of(key: str) -> str:
    """Where a variable came from: '.env', 'environment', or '' if unset."""
    if not os.environ.get(key):
        return ""
    return ".env" if key in _loaded_from_file else "environment"


def env_file_path(config_path: Optional[str] = None) -> Path:
    """Where a `.env` should be written, whether or not it exists yet."""
    existing = find_env_file(config_path)
    if existing is not None:
        return existing
    if config_path:
        return Path(config_path).resolve().parent / DEFAULT_ENV_FILENAME
    return Path.cwd() / DEFAULT_ENV_FILENAME


def set_key(key: str, value: str, config_path: Optional[str] = None) -> Path:
    """Write or replace one `KEY=value` line in `.env`, keeping the rest intact.

    Comments and unrelated keys are preserved, so a user's own notes in the file
    survive editing through the CLI.
    """
    path = env_file_path(config_path)
    lines = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8-sig").splitlines()

    replaced = False
    output = []
    for line in lines:
        stripped = line.strip()
        candidate = stripped[len("export "):].lstrip() if stripped.startswith("export ") else stripped
        name = candidate.partition("=")[0].strip()
        if not stripped.startswith("#") and name == key:
            if not replaced:
                output.append(f"{key}={value}")
                replaced = True
            continue
        output.append(line)

    if not replaced:
        if output and output[-1].strip():
            output.append("")
        output.append(f"{key}={value}")

    path.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")

    # Reflect it immediately so the same process can use it without a restart.
    os.environ[key] = value
    _loaded_from_file[key] = str(path)
    return path
