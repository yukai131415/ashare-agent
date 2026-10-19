from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    values: dict[str, Any]

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    def section(self, name: str) -> dict[str, Any]:
        return self.values[name]


def load_settings(path: str | Path | None = None) -> Settings:
    root = project_root()
    default_path = root / "config" / "settings.json"
    with default_path.open("r", encoding="utf-8") as handle:
        values = json.load(handle)
    selected = path or os.getenv("ASTOCK_CONFIG")
    if selected:
        custom_path = Path(selected).resolve()
        with custom_path.open("r", encoding="utf-8") as handle:
            values = _deep_merge(values, json.load(handle))
    return Settings(root=root, values=values)

