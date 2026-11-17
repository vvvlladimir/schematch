#!/usr/bin/env python3
"""Plausibility check of the header-based ground truth.

Flags columns whose values do not look like the attribute their header claims,
so that a person can decide: keep, relabel, or exclude (tools/wdc_gt_overrides.csv).
The check only flags; it never changes the ground truth.

    python tools/wdc_gt_audit.py            # writes datasets-local/wdc/ground_truth/audit.csv

Needs the outputs of `wdc_prepare.py clean` and `wdc_prepare.py groundtruth`.
"""
from __future__ import annotations

import csv
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wdc_candidates as wc  # noqa: E402

csv.field_size_limit(sys.maxsize)
ROOT = Path(__file__).resolve().parent.parent
WDC = ROOT / "datasets-local" / "wdc"
CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
PERSON = re.compile(r"^[A-Z][\w'\-]+, [A-Z]")          # "Byrd, Charlie"
VARIANT = re.compile(r"^\S{1,4} / \S")                 # "M / Turquoise Blue"
MIN_SHOPS_FOR_TYPE = 5


def words(v: str) -> float:
    """Word count; text in CJK scripts has no spaces, count ~4 characters as a word."""
    n = len(v.split())
    return max(n, len(CJK.findall(v)) / 4)


def numbers(values):
    out = []
    for v in values:
        try:
            out.append(float(v))
        except ValueError:
            pass
    return out


def profile(values):
    nums = numbers(values)
    return {
        "type": wc.column_type(values),
        "distinct": len(set(values)) / len(values),
        "words": statistics.mean(words(v) for v in values),
        "length": statistics.mean(len(v) for v in values),
        "numeric": len(nums) / len(values),
        "max": max(nums) if nums else None,
        "person": sum(bool(PERSON.match(v)) for v in values) / len(values),
        "variant": sum(bool(VARIANT.match(v)) for v in values) / len(values),
    }


def rules(attr: str, p: dict, n: int) -> list[str]:
    """Attribute-specific expectations. Returns the reasons a column looks wrong."""
    r = []
    constant = n >= 10 and p["distinct"] <= 0.02
    if attr == "name":
        if constant:
            r.append("same value in every row")
        if p["variant"] > 0.5:
            r.append("values look like size/colour variants")
        if p["distinct"] < 0.6 and not constant:
            r.append(f"many repeated values (distinct {p['distinct']:.2f})")
        if p["words"] <= 1.5:
            r.append(f"very short (avg {p['words']:.1f} words)")
    elif attr == "brand":
        if p["words"] > 3 or p["length"] > 30:
            r.append(f"long values (avg {p['words']:.1f} words, {p['length']:.0f} chars)")
        if p["person"] > 0.3:
            r.append("values look like person names ('Last, First')")
        if p["distinct"] > 0.95 and n >= 20:
            r.append(f"almost no repeated values (distinct {p['distinct']:.2f})")
    elif attr == "description":
        if p["words"] < 3:
            r.append(f"very short for a description (avg {p['words']:.1f} words)")
        if constant:
            r.append("same value in every row")
    elif attr in ("price", "lowprice", "highprice"):
        if p["numeric"] < 0.8:
            r.append(f"only {p['numeric']:.0%} numbers")
    elif attr == "pricecurrency":
        if p["type"] != "currency":
            r.append(f"not ISO currency codes (type {p['type']})")
    elif attr == "availability":
        if p["type"] != "enum":
            r.append(f"not schema.org availability values (type {p['type']})")
    elif attr == "aggregaterating.ratingvalue":
        if p["numeric"] < 0.8:
            r.append(f"only {p['numeric']:.0%} numbers")
        elif p["max"] is not None and p["max"] > 100:
            r.append(f"values up to {p['max']:g}, too large for a rating")
    return r


def main() -> None:
    attrs = {}
    with (WDC / "ground_truth" / "column_attributes.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            attrs[(r["table"], int(r["column_index"]))] = r["shared_attribute"]
    cols = []
    for path in sorted((WDC / "clean").glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        header, body = rows[0], rows[1:]
        for c, name in enumerate(header):
            values = [r[c] for r in body if c < len(r) and r[c]]
            if values:
                cols.append({"table": path.stem, "column": name, "attribute": attrs[(path.stem, c)],
                             "values": values, **profile(values)})

    # Generic check: the column's value type differs from the type most shops use
    # for this attribute (number and code count as the same, as in pass 2).
    by_attr = defaultdict(list)
    for c in cols:
        by_attr[c["attribute"]].append(c)
    majority = {}
    for a, cs in by_attr.items():
        if len({c["table"] for c in cs}) >= MIN_SHOPS_FOR_TYPE:
            majority[a] = Counter(wc.PASS2.get(c["type"], c["type"]) for c in cs).most_common(1)[0][0]

    flagged = []
    for c in cols:
        reasons = rules(c["attribute"], c, len(c["values"]))
        maj = majority.get(c["attribute"])
        if maj and wc.PASS2.get(c["type"], c["type"]) != maj:
            reasons.append(f"type {c['type']}, most shops: {maj}")
        if reasons:
            top = [v[:40] for v, _ in Counter(c["values"]).most_common(4)]
            flagged.append({"table": c["table"], "column": c["column"], "attribute": c["attribute"],
                            "reasons": "; ".join(reasons), "samples": " | ".join(top)})

    flagged.sort(key=lambda f: (f["attribute"], f["table"]))
    out = WDC / "ground_truth" / "audit.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["table", "column", "attribute", "reasons", "samples"])
        w.writeheader()
        w.writerows(flagged)
    print(f"{len(cols)} non-empty columns checked, {len(flagged)} flagged -> {out}\n")
    for f in flagged:
        print(f"[{f['attribute']}] {f['table']} :: {f['column']}")
        print(f"    why:     {f['reasons']}")
        print(f"    values:  {f['samples']}")


if __name__ == "__main__":
    main()
