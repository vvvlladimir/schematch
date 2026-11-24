"""Scoring helpers of wdc_evaluate: pair metrics and the 1:1 recall bound."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import numpy  # noqa: F401
    import wdc_evaluate as tool
except ImportError:  # numpy is optional for the standard-library tools
    tool = None


class Col:
    def __init__(self, table):
        self.table = table


@unittest.skipIf(tool is None, "numpy is not installed")
class ScoreTest(unittest.TestCase):
    def test_precision_recall_f1(self):
        reference = {(0, 2), (1, 3)}
        s = tool.score({(0, 2), (0, 3), (1, 9)}, reference, keep={0, 1, 2, 3})
        self.assertEqual((s["predicted"], s["correct"]), (2, 1))  # (1, 9) is not evaluated
        self.assertAlmostEqual(s["precision"], 0.5)
        self.assertAlmostEqual(s["recall"], 0.5)
        self.assertAlmostEqual(s["f1"], 0.5)

    def test_one_to_one_bound(self):
        # table A: 0, 1 (both 'name'); table B: 2 ('name') -> a 1:1 selection finds one pair
        cols = [Col("A"), Col("A"), Col("B")]
        self.assertEqual(tool.best_one_to_one({(0, 2), (1, 2)}, cols), 1)


if __name__ == "__main__":
    unittest.main()
