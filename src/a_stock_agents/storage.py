from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable

from .errors import StateCorruptedError


def ensure_runtime(root: Path) -> None:
    folders = [
        "market/data", "market/raw", "market/logs",
        "news/data", "news/raw", "news/logs",
        "analysis/data", "analysis/logs",
        "recommendation/data", "recommendation/logs",
        "backtest/data", "backtest/logs", "supervisor/logs", "state",
    ]
    for folder in folders:
        (root / "agents" / folder if folder != "state" else root / folder).mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        temp_name = handle.name
    os.replace(temp_name, path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise StateCorruptedError(f"无法读取状态文件 {path}: {exc}") from exc


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", newline="", encoding="utf-8-sig", dir=path.parent, delete=False, suffix=".tmp") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        temp_name = handle.name
    os.replace(temp_name, path)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def append_jsonl_deduplicated(path: Path, rows: list[dict[str, Any]], key: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    known: set[str] = set()
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    known.add(str(json.loads(line)[key]))
                except (json.JSONDecodeError, KeyError):
                    continue
    new_rows = [row for row in rows if str(row[key]) not in known]
    if new_rows:
        with path.open("a", encoding="utf-8") as handle:
            for row in new_rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(new_rows)


def upsert_jsonl_by_key(path: Path, rows: list[dict[str, Any]], key_fn) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    merged: dict[str, dict[str, Any]] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    merged[str(key_fn(row))] = row
    for row in rows:
        merged[str(key_fn(row))] = row
    ordered = sorted(merged.values(), key=lambda row: str(row.get("created_at", "")))
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        for row in ordered:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def newest_file(folder: Path, pattern: str) -> Path | None:
    files = sorted(folder.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return files[0] if files else None
