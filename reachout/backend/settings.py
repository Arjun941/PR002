"""Small server-side settings kept in a JSON file (SETTINGS_FILE, default settings.json): today just the
default provider that new campaigns start with. Read on every call, so a change applies at once."""
from __future__ import annotations

import json
import os
from pathlib import Path


def _path() -> Path:
    return Path(os.getenv("SETTINGS_FILE", "settings.json"))


def load() -> dict:
    try:
        data = json.loads(_path().read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(**updates) -> dict:
    data = load() | updates
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(_path())
    return data
