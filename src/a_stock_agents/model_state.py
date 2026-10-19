from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import iso_now
from .errors import StateCorruptedError
from .storage import atomic_write_json, read_json


class ModelState:
    def __init__(self, path: Path, model_names: list[str]) -> None:
        if not model_names:
            raise ValueError("模型列表不能为空")
        self.path = path
        self.model_names = model_names

    def default(self) -> dict[str, Any]:
        return {"current_model": self.model_names[0], "rotation_index": 0, "history": []}

    def load(self) -> dict[str, Any]:
        state = read_json(self.path, self.default())
        if not isinstance(state, dict) or state.get("current_model") not in self.model_names:
            raise StateCorruptedError(f"模型状态不合法: {self.path}")
        return state

    def repair(self, reason: str) -> dict[str, Any]:
        if self.path.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.path.replace(self.path.with_suffix(f".corrupt_{stamp}.json"))
        state = self.default()
        state["history"].append({"at": iso_now(), "action": "state_repaired", "reason": reason})
        atomic_write_json(self.path, state)
        return state

    def current(self) -> str:
        return str(self.load()["current_model"])

    def rotate(self, reason: str, recommendation_id: str | None = None) -> tuple[str, str]:
        try:
            state = self.load()
        except StateCorruptedError as exc:
            state = self.repair(str(exc))
        old = str(state["current_model"])
        index = (self.model_names.index(old) + 1) % len(self.model_names)
        new = self.model_names[index]
        state["current_model"] = new
        state["rotation_index"] = int(state.get("rotation_index", 0)) + 1
        state.setdefault("history", []).append(
            {
                "at": iso_now(), "action": "model_rotated", "from": old, "to": new,
                "reason": reason, "recommendation_id": recommendation_id,
            }
        )
        atomic_write_json(self.path, state)
        return old, new

