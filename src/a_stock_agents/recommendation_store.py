from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


RECOMMENDATION_COLUMNS = (
    "recommendation_id", "recommendation_date", "created_at", "target_date", "code", "name",
    "reference_price", "model", "model_version", "score", "basis_json", "disclaimer",
)


class RecommendationStore:
    """SQLite 推荐仓库：同一推荐日期保留最新计算，实时记录只保留当前快照。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS recommendation_history (
                    recommendation_id TEXT NOT NULL UNIQUE,
                    recommendation_date TEXT NOT NULL PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    reference_price REAL NOT NULL,
                    model TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    score REAL NOT NULL,
                    basis_json TEXT NOT NULL,
                    disclaimer TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_recommendation_history_target_date
                    ON recommendation_history(target_date);
                CREATE TABLE IF NOT EXISTS recommendation_realtime (
                    slot TEXT NOT NULL PRIMARY KEY CHECK (slot = 'current'),
                    recommendation_id TEXT NOT NULL,
                    recommendation_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    reference_price REAL NOT NULL,
                    model TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    score REAL NOT NULL,
                    basis_json TEXT NOT NULL,
                    disclaimer TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def save(self, recommendation: dict[str, Any]) -> bool:
        """原子写入；返回当天历史记录是否为首次插入。

        看板展示的是“每天最新一次计算结果”。如果当天启动服务时先用旧行情算过，
        后续刷新到新行情后需要覆盖当天记录，避免页面继续显示昨天或早盘旧快照。
        """
        row = _serialize(recommendation)
        placeholders = ", ".join("?" for _ in RECOMMENDATION_COLUMNS)
        columns = ", ".join(RECOMMENDATION_COLUMNS)
        update_columns = ", ".join(
            f"{column}=excluded.{column}" for column in RECOMMENDATION_COLUMNS
        )
        values = tuple(row[column] for column in RECOMMENDATION_COLUMNS)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existed = connection.execute(
                "SELECT 1 FROM recommendation_history WHERE recommendation_date = ?",
                (row["recommendation_date"],),
            ).fetchone() is not None
            connection.execute(
                f"""
                INSERT INTO recommendation_history ({columns})
                VALUES ({placeholders})
                ON CONFLICT(recommendation_date) DO UPDATE SET {update_columns}, recorded_at=CURRENT_TIMESTAMP
                """,
                values,
            )
            history_inserted = not existed
            connection.execute(
                f"""
                INSERT INTO recommendation_realtime (slot, {columns})
                VALUES ('current', {placeholders})
                ON CONFLICT(slot) DO UPDATE SET {update_columns}, updated_at=CURRENT_TIMESTAMP
                """,
                values,
            )
            connection.commit()
        return history_inserted

    def insert_history(self, recommendation: dict[str, Any]) -> bool:
        """仅用于导入既有历史；同一推荐日期已存在时保持原记录不变。"""
        row = _serialize(recommendation)
        placeholders = ", ".join("?" for _ in RECOMMENDATION_COLUMNS)
        columns = ", ".join(RECOMMENDATION_COLUMNS)
        with self._connect() as connection:
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO recommendation_history ({columns}) VALUES ({placeholders})",
                tuple(row[column] for column in RECOMMENDATION_COLUMNS),
            )
            connection.commit()
            return cursor.rowcount == 1

    def history(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recommendation_history ORDER BY recommendation_date DESC, created_at DESC"
            ).fetchall()
        return [_deserialize(row) for row in rows]

    def realtime(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recommendation_realtime WHERE slot = 'current'"
            ).fetchone()
        return _deserialize(row) if row is not None else None

    def migrate_jsonl(self, path: Path) -> int:
        """幂等迁移旧 JSONL；只在实时表为空时以最后一条记录初始化实时表。"""
        if not path.exists():
            return 0
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
        rows.sort(key=lambda item: str(item.get("created_at", "")))
        added = sum(1 for row in rows if self.insert_history(row))
        if rows and self.realtime() is None:
            self.save(rows[-1])
        return added


def recommendation_store(runtime: Path) -> RecommendationStore:
    data_dir = runtime / "agents" / "recommendation" / "data"
    store = RecommendationStore(data_dir / "recommendations.db")
    store.migrate_jsonl(data_dir / "recommendations.jsonl")
    return store


def _serialize(value: dict[str, Any]) -> dict[str, Any]:
    created_at = str(value.get("created_at", "")).strip()
    recommendation_date = str(value.get("recommendation_date") or created_at[:10]).strip()
    required = {
        "recommendation_id": str(value.get("recommendation_id", "")).strip(),
        "recommendation_date": recommendation_date,
        "created_at": created_at,
        "target_date": str(value.get("target_date", "")).strip(),
        "code": str(value.get("code", "")).strip(),
        "name": str(value.get("name", "")).strip(),
        "reference_price": float(value.get("reference_price", 0)),
        "model": str(value.get("model", "")).strip(),
        "model_version": str(value.get("model_version", "1.0")).strip(),
        "score": float(value.get("score", 0)),
        "basis_json": json.dumps(value.get("basis", {}), ensure_ascii=False, separators=(",", ":")),
        "disclaimer": str(value.get("disclaimer", "")).strip(),
    }
    text_required = (
        "recommendation_id", "recommendation_date", "created_at", "target_date", "code", "name", "model",
    )
    missing = [key for key in text_required if not required[key]]
    if missing:
        raise ValueError(f"推荐记录缺少必填字段：{', '.join(missing)}")
    return required


def _deserialize(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["basis"] = json.loads(value.pop("basis_json") or "{}")
    value.pop("slot", None)
    value.pop("recorded_at", None)
    value.pop("updated_at", None)
    return value
