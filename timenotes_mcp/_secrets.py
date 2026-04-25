"""Tiny .secrets / .env loader. No external deps.

Loads ``KEY=value`` lines from ``.secrets`` (or any path passed in) into
``os.environ`` if the key isn't already set. Lines starting with ``#`` and
blank lines are ignored. Supports surrounding single/double quotes.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_secrets(path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Load env-style key/value pairs from ``path`` into ``os.environ``.

    Defaults to ``<project_root>/.secrets`` (the directory two levels above
    this module). Returns the keys it actually set (i.e. previously unset).
    """
    if path is None:
        path = Path(__file__).resolve().parent.parent / ".secrets"
    path = Path(path)
    if not path.exists():
        return {}
    set_keys: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            set_keys[key] = value
    return set_keys
