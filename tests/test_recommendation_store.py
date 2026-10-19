import json
import tempfile
import unittest
from pathlib import Path

from a_stock_agents.recommendation_store import RecommendationStore


def recommendation(identifier: str, code: str, created_at: str) -> dict:
    return {
        "recommendation_id": identifier,
        "created_at": created_at,
        "target_date": "2026-08-25",
        "code": code,
        "name": f"股票{code}",
        "reference_price": 10.0,
        "model": "balanced",
        "model_version": "1.0",
        "score": 1.2,
        "basis": {"momentum_z": 0.5},
        "disclaimer": "test",
    }


class RecommendationStoreTests(unittest.TestCase):
    def test_same_day_history_and_realtime_use_latest_calculation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RecommendationStore(Path(folder) / "recommendations.db")
            first = recommendation("r1", "600001", "2026-08-24T09:00:00+08:00")
            second = recommendation("r2", "600002", "2026-08-24T15:00:00+08:00")
            self.assertTrue(store.save(first))
            self.assertFalse(store.save(second))
            self.assertEqual(store.history()[0]["code"], "600002")
            self.assertEqual(store.realtime()["code"], "600002")

    def test_jsonl_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "recommendations.jsonl"
            rows = [
                recommendation("r1", "600001", "2026-08-23T09:00:00+08:00"),
                recommendation("r2", "600002", "2026-08-24T09:00:00+08:00"),
            ]
            archive.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
            store = RecommendationStore(root / "recommendations.db")
            self.assertEqual(store.migrate_jsonl(archive), 2)
            self.assertEqual(store.migrate_jsonl(archive), 0)
            self.assertEqual(len(store.history()), 2)
            self.assertEqual(store.realtime()["recommendation_id"], "r2")


if __name__ == "__main__":
    unittest.main()
