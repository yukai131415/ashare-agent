import tempfile
import unittest
from pathlib import Path

from a_stock_agents.model_state import ModelState


class ModelStateTests(unittest.TestCase):
    def test_negative_feedback_rotation(self):
        with tempfile.TemporaryDirectory() as folder:
            state = ModelState(Path(folder) / "model.json", ["balanced", "momentum", "value_reversal"])
            self.assertEqual(state.current(), "balanced")
            old, new = state.rotate("negative return", "rec-1")
            self.assertEqual((old, new), ("balanced", "momentum"))
            self.assertEqual(state.current(), "momentum")


if __name__ == "__main__":
    unittest.main()

