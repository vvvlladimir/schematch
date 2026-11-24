"""wdc_gt must turn AI pre-labels, rules and overrides into one concept per cleaned column."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wdc_gt as tool


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class GtinTest(unittest.TestCase):
    def test_check_digits(self):
        self.assertTrue(tool.gtin_valid("4006381333931"))    # EAN-13
        self.assertTrue(tool.gtin_valid("036000291452"))     # UPC-A
        self.assertTrue(tool.gtin_valid("163006209X"))       # ISBN-10
        self.assertFalse(tool.gtin_valid("4006381333932"))
        self.assertFalse(tool.gtin_valid("RB-Elf-395"))

    def test_prefixes_and_dropped_leading_zero(self):
        self.assertTrue(tool.is_gtin("isbn:9780201702255"))
        self.assertTrue(tool.is_gtin("36000291452"))          # UPC-A without its leading 0
        self.assertFalse(tool.is_gtin("123"))


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.tables, self.gt = root / "tables", root / "gt"
        write(self.tables / "Product_a.com_October2023.csv",
              "﻿manufacturer,mpn,offers.price,offers[].price,sku,unused\n"
              "Acme,X1,1.00,,X1,\nAcme,X2,,2.00,X2,\nAcme,X3,3.00,,X3,\n"
              "Acme,X4,4.00,,X4,\nAcme,X5,5.00,,X5,\n")
        write(self.tables / "Product_b.com_October2023.csv",
              "﻿brand.name,gtin13,offers.lowprice,offers.highprice,name\n"
              "Zed,4006381333931,1,2,Lamp\nZed,036000291452,3,4,Desk\n")
        write(self.gt / "draft_ai.csv",
              "table,column,ai_group,ai_rule,ai_risk\n"
              "a.com,manufacturer,manufacturer,,\na.com,mpn,mpn,,\na.com,offers.price,price,,\n"
              "a.com,offers[].price,price,,\na.com,sku,sku,,\na.com,unused,unused,,\n"
              "b.com,brand.name,brand,,\nb.com,gtin13,gtin13,,\nb.com,offers.lowprice,lowprice,,\n"
              "b.com,offers.highprice,highprice,,\nb.com,name,name,,\n")
        write(self.gt / "concept_rules.csv",
              "ai_group,concept,reason\nmanufacturer,brand,manufacturer = brand\n")
        write(self.gt / "overrides.csv", "table,column,concept,reason\nb.com,name,title,test\n")
        write(self.gt / "complex_rules.csv", "left,right,reason\nprice,lowprice+highprice,range\n")

    def labels(self):
        rows, problems = tool.build(self.gt, self.tables)
        return {(r["table"], r["column"]): r for r in rows}, rows, problems

    def test_layers(self):
        labels, _, problems = self.labels()
        self.assertEqual(problems, [])
        self.assertEqual(labels[("a.com", "manufacturer")]["concept"], "brand")
        self.assertEqual(labels[("a.com", "manufacturer")]["source"], "rule")
        self.assertEqual(labels[("b.com", "gtin13")]["concept"], "gtin")
        self.assertEqual(labels[("a.com", "mpn")]["concept"], "sku")        # copy of the sku
        self.assertEqual(labels[("a.com", "mpn")]["source"], "identifier")
        self.assertEqual(labels[("b.com", "name")]["concept"], "title")
        self.assertEqual(labels[("b.com", "name")]["source"], "override")

    def test_split_columns_are_merged_and_empty_columns_not_evaluated(self):
        labels, _, _ = self.labels()
        self.assertNotIn(("a.com", "offers[].price"), labels)
        self.assertEqual(labels[("a.com", "offers.price")]["raw_columns"], "offers.price | offers[].price")
        self.assertEqual(labels[("a.com", "offers.price")]["non_empty"], 5)
        self.assertEqual(labels[("a.com", "unused")]["evaluated"], 0)

    def test_pairs_and_complex(self):
        _, rows, _ = self.labels()
        pairs = tool.positive_pairs(rows)
        self.assertIn((("a.com", "manufacturer"), ("b.com", "brand.name")), pairs)
        self.assertTrue(all(a[0] != b[0] for a, b in pairs))
        complex_rows = tool.complex_correspondences(rows, self.gt)
        self.assertEqual(len(complex_rows), 1)
        self.assertEqual(complex_rows[0]["columns_b"], "offers.lowprice + offers.highprice")
        self.assertEqual(complex_rows[0]["kind"], "1:2")

    def test_unknown_override_is_a_problem(self):
        write(self.gt / "overrides.csv", "table,column,concept,reason\nb.com,nope,title,test\n")
        _, _, problems = self.labels()
        self.assertTrue(any("unknown column b.com.nope" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
