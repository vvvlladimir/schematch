"""wdc_expand must flatten raw WDC JSON Lines exactly as vorbereitete_tabellen/ was built."""
import csv
import gzip
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wdc_expand as tool


def read(data: bytes):
    text = data.decode("utf-8")
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:])))


class WdcExpandTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, records, name="Product_shop.example_October2023", raw_lines=()):
        path = self.root / f"{name}.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            for line in raw_lines:
                fh.write(line + "\n")
        return path

    def test_nested_objects_lists_and_dropped_keys(self):
        path = self.write([{
            "row_id": 0, "page_url": "http://x", "name": "Füller",
            "offers": {"price": "9.5", "pricecurrency": "EUR"},
            "sku": ["a", "b"], "gtin": ["only"], "color": [],
            "instock": True, "weight": 1.5, "brand": None,
        }])
        result = tool.expand_file(path, rows=1)
        header, row = read(result.data)
        self.assertEqual(header, ["color[]", "gtin[]", "instock", "name", "offers.price",
                                  "offers.pricecurrency", "sku[]", "weight"])
        self.assertEqual(row, ["", "only", "true", "Füller", "9.5", "EUR", '["a", "b"]', "1.5"])
        self.assertEqual(result.warnings, [])

    def test_list_of_objects_is_transposed(self):
        path = self.write([
            {"offers": [{"price": "1", "method": ["V", "M"]}, {"price": "2", "method": ["A"]}]},
            {"offers": [{"price": "3"}, {"name": "x"}]},
            {"offers": [{"price": None}]},
        ])
        header, *rows = read(tool.expand_file(path).data)
        self.assertEqual(header, ["offers[].method[]", "offers[].name", "offers[].price"])
        self.assertEqual(rows, [['["V", "M", "A"]', "", '["1", "2"]'],
                                ["", "x", "3"],
                                ["", "", ""]])

    def test_columns_come_from_whole_file_unless_rows_requested(self):
        path = self.write([{"name": "a"}, {"name": "b"}, {"name": "c", "late": 1}])
        header, *rows = read(tool.expand_file(path, rows=2).data)
        self.assertEqual(header, ["late", "name"])
        self.assertEqual(rows, [["", "a"], ["", "b"]])
        header, *_ = read(tool.expand_file(path, rows=2, columns="rows").data)
        self.assertEqual(header, ["name"])

    def test_random_sample_is_reproducible_and_keeps_file_order(self):
        path = self.write([{"n": i} for i in range(200)])
        first = tool.expand_file(path, rows=10, sample="random", seed=3)
        again = tool.expand_file(path, rows=10, sample="random", seed=3)
        other = tool.expand_file(path, rows=10, sample="random", seed=4)
        self.assertEqual(first.data, again.data)
        self.assertNotEqual(first.data, other.data)
        values = [int(r[0]) for r in read(first.data)[1:]]
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(set(values)), 10)
        self.assertNotEqual(values, list(range(10)))

    def test_bad_lines_and_short_files_are_reported(self):
        path = self.write([{"name": "a"}], raw_lines=["{broken", "[1]", ""])
        result = tool.expand_file(path, rows=5)
        self.assertEqual(result.rows, 1)
        self.assertEqual(len(result.warnings), 3)
        self.assertTrue(any("fewer than --rows 5" in w for w in result.warnings))

    def test_ambiguous_keys_are_reported(self):
        path = self.write([{"a.b": 1, "a": {"b": 2}}])
        warnings = tool.expand_file(path).warnings
        self.assertTrue(any("ambiguous" in w for w in warnings))
        self.assertTrue(any("same column" in w for w in warnings))

    def test_check_detects_differences(self):
        self.write([{"name": "a"}])
        out = self.root / "out"
        self.assertEqual(tool.main(["expand", "--input", str(self.root), "--output", str(out), "--jobs", "1"]), 0)
        self.assertEqual(tool.main(["check", "--input", str(self.root), "--output", str(out), "--jobs", "1"]), 0)
        target = next(out.glob("*.csv"))
        target.write_bytes(target.read_bytes() + b"x\n")
        self.assertEqual(tool.main(["check", "--input", str(self.root), "--output", str(out), "--jobs", "1"]), 1)


if __name__ == "__main__":
    unittest.main()
