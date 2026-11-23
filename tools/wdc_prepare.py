#!/usr/bin/env python3
"""Clean the WDC Product tables (step 1 of docs/wdc-product-matching-report.md).

    python tools/wdc_prepare.py clean       # cleaned copies + cleaning report

Input files are never modified. Outputs go to datasets-local/wdc/ (git-ignored).
Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "vorbereitete_tabellen"
DEFAULT_OUTPUT = ROOT / "datasets-local" / "wdc"

NULL_TOKENS = {"undefined", "null", "none", "nan", "n/a", "na", "-", "[]"}
# A column is treated as numeric when at least this share of its non-empty
# values parses as a number. Below that, values are left untouched, so codes
# such as "12E3" in an ID column are never rewritten.
NUMERIC_COLUMN_SHARE = 0.8

CURRENCY_SYMBOLS = "$€£¥₽₩₹"
_NUM_CORE = r"[+-]?(?:\d+(?:[.,]\d+)*|\.\d+)(?:[eE][+-]?\d+)?"
NUMBER_RE = re.compile(
    rf"^\s*[{CURRENCY_SYMBOLS}]?\s*({_NUM_CORE})\s*[{CURRENCY_SYMBOLS}]?\s*$"
)
THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")
DECIMAL_COMMA_RE = re.compile(r"^[+-]?\d+,\d{1,2}$")


def table_id(path: Path) -> str:
    """Product_10x10.co.kr_October2023.csv -> 10x10.co.kr"""
    stem = path.stem
    if stem.startswith("Product_"):
        stem = stem[len("Product_"):]
    return re.sub(r"_[A-Za-z]+\d{4}$", "", stem)


def read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    header = [h.replace("﻿", "").strip() for h in rows[0]]
    body = [r + [""] * (len(header) - len(r)) for r in rows[1:]]
    return header, body


# ---------------------------------------------------------------- cleaning --

def unpack_list(value: str) -> tuple[str, bool]:
    """'["31.70", "31.16"]' -> '31.70'. Keeps one value per cell so rows stay rows."""
    v = value.strip()
    if not (v.startswith("[") and v.endswith("]")):
        return value, False
    try:
        items = json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return value, False
    if not isinstance(items, list):
        return value, False
    for item in items:
        text = "" if item is None else str(item).strip()
        if text and text.lower() not in NULL_TOKENS:
            return text, True
    return "", True


def parse_number(value: str) -> str | None:
    """Return a plain decimal string, or None if value is not a number."""
    m = NUMBER_RE.match(value)
    if not m:
        return None
    core = m.group(1)
    mantissa, _, exponent = core.lower().partition("e")
    if "," in mantissa:
        if THOUSANDS_RE.match(mantissa):
            mantissa = mantissa.replace(",", "")
        elif DECIMAL_COMMA_RE.match(mantissa):
            mantissa = mantissa.replace(",", ".")
        else:
            return None
    if mantissa.count(".") > 1:
        return None
    if not exponent:
        # Plain notation stays as written ("31.70", "003489"); only symbols and
        # separators were removed above.
        return mantissa.lstrip("+")
    try:
        number = float(f"{mantissa}e{exponent}")
    except ValueError:
        return None
    text = f"{number:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def clean_table(header: list[str], body: list[list[str]], stats: Counter) -> list[list[str]]:
    cleaned = [list(r) for r in body]
    for c in range(len(header)):
        # pass 1: unpack lists, normalize nulls and whitespace
        for row in cleaned:
            v = row[c]
            v, was_list = unpack_list(v)
            if was_list:
                stats["list_cells_unpacked"] += 1
            v = v.strip()
            if v.lower() in NULL_TOKENS:
                if v:
                    stats["null_tokens_removed"] += 1
                v = ""
            row[c] = v
        # pass 2: normalize numbers, only in columns that are mostly numeric
        values = [row[c] for row in cleaned if row[c]]
        if not values:
            continue
        parsed = [parse_number(v) for v in values]
        if sum(p is not None for p in parsed) / len(values) < NUMERIC_COLUMN_SHARE:
            continue
        stats["numeric_columns"] += 1
        for row in cleaned:
            if not row[c]:
                continue
            p = parse_number(row[c])
            if p is not None and p != row[c]:
                stats["numbers_normalized"] += 1
                row[c] = p
    return cleaned


def merge_split_columns(header: list[str], body: list[list[str]], stats: Counter
                        ) -> tuple[list[str], list[list[str]]]:
    """Join columns that the WDC flattening split by JSON shape (offers.price /
    offers[].price). Only done when no row fills more than one of them, so no
    value is lost or overwritten."""
    groups = defaultdict(list)
    for i, h in enumerate(header):
        groups[normalize_header(h)].append(i)
    drop = set()
    for idx in groups.values():
        if len(idx) < 2:
            continue
        if any(sum(bool(row[i]) for i in idx) > 1 for row in body):
            stats["split_columns_kept_overlap"] += 1
            continue
        keep = next((i for i in idx if "[]" not in header[i]), idx[0])
        for row in body:
            row[keep] = next((row[i] for i in idx if row[i]), "")
        drop.update(i for i in idx if i != keep)
        stats["split_columns_merged"] += len(idx) - 1
    kept = [i for i in range(len(header)) if i not in drop]
    return [header[i] for i in kept], [[row[i] for i in kept] for row in body]


def cmd_clean(args: argparse.Namespace) -> None:
    out_dir = args.output / "clean"
    out_dir.mkdir(parents=True, exist_ok=True)
    total = Counter()
    report = []
    for path in sorted(args.input.glob("*.csv")):
        header, body = read_table(path)
        stats = Counter()
        cleaned = clean_table(header, body, stats)
        header, cleaned = merge_split_columns(header, cleaned, stats)
        tid = table_id(path)
        with (out_dir / f"{tid}.csv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(cleaned)
        empty_cols = sum(1 for c in range(len(header)) if not any(r[c] for r in cleaned))
        report.append({"table": tid, "source_file": path.name, "rows": len(body),
                       "columns": len(header), "empty_columns": empty_cols, **stats})
        total.update(stats)
        total["tables"] += 1
        total["columns"] += len(header)
        total["empty_columns"] += empty_cols
    fields = ["table", "source_file", "rows", "columns", "empty_columns", "list_cells_unpacked",
              "null_tokens_removed", "numeric_columns", "numbers_normalized",
              "split_columns_merged", "split_columns_kept_overlap"]
    with (args.output / "cleaning_report.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, restval=0)
        w.writeheader()
        w.writerows(report)
    print(f"Cleaned tables written to {out_dir}")
    for k in ["tables", "columns", "empty_columns", "list_cells_unpacked",
              "null_tokens_removed", "numeric_columns", "numbers_normalized",
              "split_columns_merged", "split_columns_kept_overlap"]:
        print(f"  {k:<22} {total[k]}")


def normalize_header(h: str) -> str:
    """offers[].price, offers.price[], Offers.Price -> offers.price"""
    h = h.replace("\ufeff", "").replace("%20", " ").strip().lower()
    return h.replace("[]", "")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("clean", help="write cleaned copies and a cleaning report")
    args = p.parse_args()
    cmd_clean(args)


if __name__ == "__main__":
    main()
