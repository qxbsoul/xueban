import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("full_grading_preflight", ROOT / "scripts" / "full_grading_preflight.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PreflightParserTest(unittest.TestCase):
    def test_parses_scores_with_descriptions(self):
        specs = MODULE.parse_points("Q018P1 正确去括号 1分\nQ018P2 合并同类项得到-9 1分\nQ018P3 说明理由 2分")
        self.assertEqual(specs, {"Q018P1": 1.0, "Q018P2": 1.0, "Q018P3": 2.0})

    def test_parses_compact_scores(self):
        self.assertEqual(MODULE.parse_points("Q001P1 2分 Q001P2 3分"), {"Q001P1": 2.0, "Q001P2": 3.0})

    def test_unparsable_returns_empty(self):
        self.assertEqual(MODULE.parse_points("按步骤给分，无编号"), {})


if __name__ == "__main__":
    unittest.main()
