"""Where configuration comes from.

Precedence, highest first:
  1. the process environment
  2. OPS_SECRETS_JSON (the GitHub workflow passes `toJSON(secrets)` here)
  3. an env file (default automation/.env, gitignored) for servers and local use
Empty values count as unset: GitHub renders a missing secret as "".
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not _KEY.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:   # unquoted: a " #" starts a comment, as in docker compose env files
            value = "" if value.startswith("#") else value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load_settings(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    layers: list[Mapping[str, str]] = []
    if env_file is not None and env_file.is_file():
        layers.append(read_env_file(env_file))
    blob = environ.get("OPS_SECRETS_JSON")
    if blob:
        try:
            data = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"OPS_SECRETS_JSON is not valid JSON: {exc}") from None
        layers.append({k: v for k, v in data.items() if isinstance(v, str)})
    layers.append({k: v for k, v in environ.items() if k != "OPS_SECRETS_JSON"})

    merged: dict[str, str] = {}
    for layer in layers:
        merged.update({k: v for k, v in layer.items() if v != ""})
    return merged
