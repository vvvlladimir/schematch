#!/usr/bin/env python3
"""Clean the WDC Product tables and draft their header-derived ground truth.

Step 1 and step 2 of docs/wdc-product-matching-plan.md.

    python tools/wdc_prepare.py clean       # cleaned copies + cleaning report
    python tools/wdc_prepare.py headers     # header review file for ground truth
    python tools/wdc_prepare.py groundtruth # apply the ground truth rules

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


# ---------------------------------------------------------- header review --

def normalize_header(h: str) -> str:
    """offers[].price, offers.price[], Offers.Price -> offers.price"""
    h = h.replace("﻿", "").replace("%20", " ").strip().lower()
    return h.replace("[]", "")


def cmd_headers(args: argparse.Namespace) -> None:
    src = args.output / "clean"
    if not src.is_dir():
        sys.exit("Run 'clean' first.")
    occurrences = defaultdict(list)  # normalized -> [(table, original header, non-empty?)]
    for path in sorted(src.glob("*.csv")):
        header, body = read_table(path)
        for c, h in enumerate(header):
            filled = any(r[c] for r in body)
            occurrences[normalize_header(h)].append((path.stem, h, filled))
    rows = []
    for norm, occ in occurrences.items():
        rows.append({
            "normalized": norm,
            "last_segment": norm.split(".")[-1],
            "tables": len({t for t, _, _ in occ}),
            "non_empty_tables": len({t for t, _, f in occ if f}),
            "variants": " | ".join(sorted({h for _, h, _ in occ})),
            "shared_attribute": norm,  # edit this column to merge attributes
        })
    rows.sort(key=lambda r: (-r["tables"], r["normalized"]))
    path = args.output / "header_review.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    by_segment = defaultdict(list)
    for r in rows:
        by_segment[r["last_segment"]].append(r["normalized"])
    clashes = {s: n for s, n in by_segment.items() if len(n) > 1}
    print(f"Header review written to {path}")
    print(f"  original header variants  {sum(len(o) for o in occurrences.values())} columns")
    print(f"  normalized names          {len(rows)}")
    print(f"  only in one table         {sum(r['tables'] == 1 for r in rows)}")
    print(f"  same last segment, different path (merge candidates): {len(clashes)}")
    for seg, names in sorted(clashes.items(), key=lambda kv: -len(kv[1]))[:args.show]:
        print(f"    {seg:<18} {', '.join(names)}")


# ---------------------------------------------------------- ground truth --
# Decisions recorded in docs/wdc-product-matching-plan.md, "Ground truth rules".

# Rule 1: brand and brand.name are one attribute.
# Rule 2: SKU variants are one attribute; productid and mpn stay separate.
EXPLICIT_MERGES = {
    "brand.name": "brand",
    "offers.brand": "brand",
    "url.brand": "brand",
    "offers.sku": "sku",
    "url.sku": "sku",
}
# Rule 3: offers.X and X are one attribute for properties of the offer or of the
# product that schema.org allows on both. offers.name, url.name, brand.name and
# similar names of *other* objects stay separate.
OFFER_PROPERTIES = {
    "price", "pricecurrency", "lowprice", "highprice", "pricevaliduntil",
    "availability", "itemcondition", "category", "offercount",
    "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "isbn",
}
# Rule 4: price, lowprice and highprice stay separate attributes. Pairs between
# them are recorded as related (1:n) and reported apart, not as true matches.
RELATED_GROUPS = [{"price", "lowprice", "highprice"}]

# Manual corrections after the plausibility audit (tools/wdc_gt_audit.py): the
# header of some columns does not describe their values. Each row relabels one
# column; EXCLUDE removes the column from every evaluation.
DEFAULT_OVERRIDES = ROOT / "tools" / "wdc_gt_overrides.csv"
EXCLUDE = "_exclude"


def load_overrides(path: Path | None) -> dict[tuple[str, str], tuple[str, str]]:
    if path is None:
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return {(r["table"], r["column"]): (r["new_attribute"], r["reason"]) for r in csv.DictReader(fh)}


def shared_attribute(normalized: str) -> str:
    if normalized in EXPLICIT_MERGES:
        return EXPLICIT_MERGES[normalized]
    prefix, _, prop = normalized.partition(".")
    if prefix == "offers" and prop in OFFER_PROPERTIES:
        return prop
    return normalized


def cmd_groundtruth(args: argparse.Namespace) -> None:
    src = args.output / "clean"
    if not src.is_dir():
        sys.exit("Run 'clean' first.")
    out = args.output / "ground_truth"
    out.mkdir(parents=True, exist_ok=True)
    overrides = load_overrides(None if args.no_overrides else args.overrides)
    used = set()
    columns = []
    for path in sorted(src.glob("*.csv")):
        header, body = read_table(path)
        for c, h in enumerate(header):
            norm = normalize_header(h)
            by_header = shared_attribute(norm)
            attr, reason = overrides.get((path.stem, h), (by_header, ""))
            if (path.stem, h) in overrides:
                used.add((path.stem, h))
            non_empty = int(any(r[c] for r in body))
            columns.append({
                "table": path.stem, "column_index": c, "column": h, "normalized": norm,
                "shared_attribute": attr, "header_attribute": by_header,
                "override_reason": reason, "non_empty": non_empty,
                "evaluated": int(non_empty and attr != EXCLUDE),
            })
    unknown = set(overrides) - used
    if unknown:
        sys.exit(f"overrides for unknown columns: {sorted(unknown)}")
    with (out / "column_attributes.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(columns[0]))
        w.writeheader()
        w.writerows(columns)

    by_attr = defaultdict(list)
    for col in columns:
        if col["shared_attribute"] != EXCLUDE:
            by_attr[col["shared_attribute"]].append(col)
    attr_rows = []
    for attr, cols in by_attr.items():
        filled = [c for c in cols if c["evaluated"]]
        tables = {c["table"] for c in filled}
        attr_rows.append({"shared_attribute": attr, "columns": len(cols),
                          "non_empty_columns": len(filled), "non_empty_tables": len(tables),
                          "source_names": " | ".join(sorted({c["normalized"] for c in cols}))})
    attr_rows.sort(key=lambda r: (-r["non_empty_tables"], r["shared_attribute"]))
    with (out / "attributes.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(attr_rows[0]))
        w.writeheader()
        w.writerows(attr_rows)

    # Pair counts over non-empty columns of different tables.
    filled = [c for c in columns if c["evaluated"]]
    per_table = Counter(c["table"] for c in filled)
    total_pairs = (sum(per_table.values()) ** 2 - sum(n * n for n in per_table.values())) // 2
    true_pairs = 0
    for cols in by_attr.values():
        t = Counter(c["table"] for c in cols if c["evaluated"])
        true_pairs += (sum(t.values()) ** 2 - sum(n * n for n in t.values())) // 2
    related_pairs = 0
    for group in RELATED_GROUPS:
        members = sorted(group)
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                ta = Counter(c["table"] for c in by_attr.get(a, []) if c["evaluated"])
                tb = Counter(c["table"] for c in by_attr.get(b, []) if c["evaluated"])
                related_pairs += sum(na * nb for x, na in ta.items() for y, nb in tb.items() if x != y)
    singletons = sum(1 for r in attr_rows if r["non_empty_tables"] == 1)
    print(f"Ground truth written to {out}")
    changed = [c for c in columns if c["override_reason"]]
    print(f"  manual overrides applied      {len(changed)} "
          f"({sum(c['shared_attribute'] == EXCLUDE for c in changed)} excluded, "
          f"{sum(c['shared_attribute'] != EXCLUDE for c in changed)} relabelled)")
    print(f"  shared attributes             {len(attr_rows)} ({singletons} in only one table)")
    print(f"  evaluated columns             {len(filled)} (non-empty, not excluded)")
    print(f"  cross-table pairs (non-empty) {total_pairs}")
    print(f"  true pairs                    {true_pairs}")
    print(f"  related 1:n pairs (reported apart) {related_pairs}")
    print("  top attributes:")
    for r in attr_rows[:args.show]:
        print(f"    {r['shared_attribute']:<30} {r['non_empty_tables']:>3} tables  <- {r['source_names']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("clean", help="write cleaned copies and a cleaning report")
    h = sub.add_parser("headers", help="write the header review file for the ground truth")
    h.add_argument("--show", type=int, default=25, help="merge candidates to print")
    g = sub.add_parser("groundtruth", help="apply the ground truth rules and write the answers")
    g.add_argument("--show", type=int, default=20, help="attributes to print")
    g.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES,
                   help="manual corrections (table, column, new_attribute, reason)")
    g.add_argument("--no-overrides", action="store_true",
                   help="header-based ground truth only (version 1 of the report)")
    args = p.parse_args()
    {"clean": cmd_clean, "headers": cmd_headers, "groundtruth": cmd_groundtruth}[args.command](args)


if __name__ == "__main__":
    main()
