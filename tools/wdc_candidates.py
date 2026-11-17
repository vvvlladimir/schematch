#!/usr/bin/env python3
"""Candidate selection (search-space reduction) for the WDC Product tables.

Step 4 of docs/wdc-product-matching-plan.md. Needs the outputs of
tools/wdc_prepare.py (clean + groundtruth).

    python tools/wdc_candidates.py evaluate            # PC/RR for all methods
    python tools/wdc_candidates.py evaluate --write top3   # also write candidates.csv

Column names are never used to build candidates: every method looks only at the
values. Names are used only to score the result against the ground truth.
Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import math
import re
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

csv.field_size_limit(sys.maxsize)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "datasets-local" / "wdc"
SAMPLE = 100  # values per column used for profiling


# ------------------------------------------------------------------ data --

class Column:
    __slots__ = ("idx", "table", "name", "attribute", "values", "type", "tokens", "features")

    def __init__(self, idx, table, name, attribute, values):
        self.idx, self.table, self.name, self.attribute, self.values = idx, table, name, attribute, values


def load(base: Path) -> list[Column]:
    attrs = {}
    with (base / "ground_truth" / "column_attributes.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            attrs[(r["table"], int(r["column_index"]))] = r["shared_attribute"]
    cols = []
    for path in sorted((base / "clean").glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        header, body = rows[0], rows[1:]
        for c, name in enumerate(header):
            values = [r[c] for r in body if c < len(r) and r[c]]
            # Empty columns cannot be matched from values; excluded ones are not scored.
            if values and attrs[(path.stem, c)] != "_exclude":
                cols.append(Column(len(cols), path.stem, name, attrs[(path.stem, c)], values))
    return cols


# --------------------------------------------------------- column profile --

NUM_RE = re.compile(r"^[+-]?(\d+(\.\d+)?|\.\d+)$")
DATE_RE = re.compile(
    r"^(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"                      # 2023-10-04, 2023/10/12
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}"                       # 04.10.2023
    r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})",  # Oct. 21, 2023
    re.I)
# ISO 4217 codes; other three-letter capitals (brands such as "ESR") are not currency.
CURRENCIES = set("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD
CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD
GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT
LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR
NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP
STN SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF
YER ZAR ZMW ZWL RUR
""".split())
URL_RE = re.compile(r"^(https?:)?//")
ENUM_RE = re.compile(r"schema\.org/", re.I)
# schema.org ItemAvailability and OfferItemCondition members, compared without
# case, spaces, "_" or "-": InStock, in_stock, inStock -> instock.
ENUM_VALUES = {
    "instock", "outofstock", "preorder", "presale", "backorder", "discontinued",
    "instoreonly", "onlineonly", "limitedavailability", "soldout", "madetoorder",
    "newcondition", "usedcondition", "refurbishedcondition", "damagedcondition",
}
ID_RE = re.compile(r"^[\w\-./#:\[\]|]{3,80}$")


def value_type(v: str) -> str:
    if (ENUM_RE.search(v) and len(v) < 80) or re.sub(r"[\s_\-]", "", v.lower()) in ENUM_VALUES:
        return "enum"
    if URL_RE.match(v):
        return "url"
    if DATE_RE.match(v):
        return "date"
    if v.upper() in CURRENCIES and len(v) == 3:
        return "currency"
    if NUM_RE.match(v):
        return "number"
    if ID_RE.match(v) and (any(ch.isdigit() for ch in v) or re.search(r"[a-z][\-:_\[][A-Za-z]", v)):
        return "code"
    return "text"


def column_type(values: list[str]) -> str:
    return Counter(value_type(v) for v in values[:SAMPLE]).most_common(1)[0][0]


def features(values: list[str]) -> list[float]:
    vs = values[:SAMPLE]
    lengths = [len(v) for v in vs]
    s = "".join(vs) or " "
    n = len(s)
    mean = sum(lengths) / len(lengths)
    sd = (sum((x - mean) ** 2 for x in lengths) / len(lengths)) ** 0.5
    return [
        math.log1p(mean), math.log1p(sd),
        sum(ch.isdigit() for ch in s) / n,
        sum(ch.isalpha() for ch in s) / n,
        sum(ch.isspace() for ch in s) / n,
        sum(not ch.isalnum() and not ch.isspace() for ch in s) / n,
        sum(ch.isupper() for ch in s) / n,
        len(set(vs)) / len(vs),
        math.log1p(sum(len(v.split()) for v in vs) / len(vs)),
    ]


def profile(cols: list[Column]) -> None:
    for c in cols:
        c.type = column_type(c.values)
        c.tokens = {w for v in c.values[:SAMPLE] for w in re.findall(r"\w+", v.lower())}
        c.features = features(c.values)
    dims = len(cols[0].features)
    mu = [sum(c.features[k] for c in cols) / len(cols) for k in range(dims)]
    sd = [(sum((c.features[k] - mu[k]) ** 2 for c in cols) / len(cols)) ** 0.5 or 1.0 for k in range(dims)]
    for c in cols:
        c.features = [(c.features[k] - mu[k]) / sd[k] for k in range(dims)]


# -------------------------------------------------------- candidate rules --

def pairs_from_buckets(cols: list[Column], buckets: dict) -> set[tuple[int, int]]:
    out = set()
    for ids in buckets.values():
        for i, j in combinations(ids, 2):
            if cols[i].table != cols[j].table:
                out.add((i, j) if i < j else (j, i))
    return out


def blocking(cols: list[Column], key) -> set[tuple[int, int]]:
    buckets = defaultdict(list)
    for c in cols:
        k = key(c)
        if k is not None:
            buckets[k].append(c.idx)
    return pairs_from_buckets(cols, buckets)


# Further passes group types that the same attribute takes in different shops:
# pass 2: SKUs are numbers in one shop and codes in another;
# pass 3: IDs are codes in one shop and free text in another.
PASS2 = {"number": "idlike", "code": "idlike"}
PASS3 = {"code": "codetext", "text": "codetext"}


def type_blocking_two_pass(cols):
    return blocking(cols, lambda c: c.type) | blocking(cols, lambda c: PASS2.get(c.type, c.type))


def type_blocking_three_pass(cols):
    return type_blocking_two_pass(cols) | blocking(cols, lambda c: PASS3.get(c.type))


def top_k(cols, base: set[tuple[int, int]], k: int) -> set[tuple[int, int]]:
    """Keep, for every column, its k nearest columns per other table (union of both directions)."""
    near = defaultdict(lambda: defaultdict(list))
    for i, j in base:
        d = sum((x - y) ** 2 for x, y in zip(cols[i].features, cols[j].features))
        near[i][cols[j].table].append((d, j))
        near[j][cols[i].table].append((d, i))
    out = set()
    for i, per_table in near.items():
        for lst in per_table.values():
            for _, j in sorted(lst)[:k]:
                out.add((i, j) if i < j else (j, i))
    return out


def inverted_index(cols, max_df_share=0.10):
    df = Counter(w for c in cols for w in c.tokens)
    limit = int(max_df_share * len(cols))
    buckets = defaultdict(list)
    for c in cols:
        for w in c.tokens:
            if df[w] <= limit:
                buckets[w].append(c.idx)
    return pairs_from_buckets(cols, buckets)


def minhash_lsh(cols, bands=16, rows=4):
    k = bands * rows
    buckets = defaultdict(list)
    for c in cols:
        if not c.tokens:
            continue
        sig = [min(int(hashlib.md5(f"{h}:{w}".encode()).hexdigest()[:8], 16) for w in c.tokens)
               for h in range(k)]
        for b in range(bands):
            buckets[(b, tuple(sig[b * rows:(b + 1) * rows]))].append(c.idx)
    return pairs_from_buckets(cols, buckets)


def size_filter(cols, base, min_ratio=0.1):
    dc = {c.idx: len(set(c.values)) for c in cols}
    return {(i, j) for i, j in base if min(dc[i], dc[j]) / max(dc[i], dc[j]) >= min_ratio}


# ------------------------------------------------------------ evaluation --

RELATED = [{"price", "lowprice", "highprice"}]


def truth(cols):
    by_attr = defaultdict(list)
    for c in cols:
        by_attr[c.attribute].append(c.idx)
    true = set()
    for ids in by_attr.values():
        for i, j in combinations(ids, 2):
            if cols[i].table != cols[j].table:
                true.add((i, j))
    related = set()
    for group in RELATED:
        for a, b in combinations(sorted(group), 2):
            for i in by_attr.get(a, []):
                for j in by_attr.get(b, []):
                    if cols[i].table != cols[j].table:
                        related.add((min(i, j), max(i, j)))
    return true, related


def all_pairs(cols):
    per_table = Counter(c.table for c in cols)
    n = sum(per_table.values())
    return (n * n - sum(v * v for v in per_table.values())) // 2


def cmd_evaluate(args):
    cols = load(args.dir)
    profile(cols)
    true, related = truth(cols)
    total = all_pairs(cols)
    print(f"non-empty columns {len(cols)}, all cross-table pairs {total}, "
          f"true pairs {len(true)}, related 1:n pairs {len(related)}")
    print("column types:", dict(Counter(c.type for c in cols).most_common()))
    print()

    type1 = blocking(cols, lambda c: c.type)
    type2 = type_blocking_two_pass(cols)
    type3 = type_blocking_three_pass(cols)
    methods = {
        "type1": ("Type blocking, one pass", type1),
        "type2": ("Type blocking, two passes", type2),
        "type3": ("Type blocking, three passes", type3),
    }
    for k in (5, 3, 2, 1):
        methods[f"top{k}"] = (f"Three passes + top-{k} per table", top_k(cols, type3, k))
    methods["inverted"] = ("Inverted index on value tokens", inverted_index(cols))
    methods["lsh"] = ("MinHash + LSH on value tokens (b=16, r=4)", minhash_lsh(cols))
    methods["size"] = ("Three passes + size filter (distinct ratio >= 0.1)", size_filter(cols, type3))

    rows = []
    print(f"{'method':<50}{'candidates':>11}{'PC':>8}{'RR':>8}{'PC 1:n':>8}")
    for key, (label, cand) in methods.items():
        pc = len(cand & true) / len(true)
        rr = 1 - len(cand) / total
        pc_rel = len(cand & related) / len(related) if related else 0
        rows.append({"method": key, "label": label, "candidates": len(cand),
                     "pc": round(pc, 4), "rr": round(rr, 4), "pc_related": round(pc_rel, 4)})
        print(f"{label:<50}{len(cand):>11}{pc:>8.1%}{rr:>8.1%}{pc_rel:>8.1%}")

    out = args.dir / "candidates"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "reduction_report.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    # Loss analysis for the chosen method: which attributes and type combinations are lost.
    chosen = methods[args.analyze][1]
    lost = true - chosen
    by_attr = Counter(cols[i].attribute for i, _ in lost)
    attr_total = Counter(cols[i].attribute for i, _ in true)
    by_types = Counter(tuple(sorted((cols[i].type, cols[j].type))) for i, j in lost)
    print(f"\nLost true pairs with '{methods[args.analyze][0]}': {len(lost)}")
    print("  by attribute (lost / true):")
    for a, n in by_attr.most_common(args.show):
        print(f"    {a:<30} {n:>6} / {attr_total[a]:<6} ({n / attr_total[a]:.0%})")
    print("  by type combination:")
    for t, n in by_types.most_common(args.show):
        print(f"    {' vs '.join(t):<30} {n:>6}")
    with (out / f"lost_{args.analyze}.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["attribute", "table_a", "column_a", "type_a", "sample_a",
                    "table_b", "column_b", "type_b", "sample_b"])
        for i, j in sorted(lost):
            a, b = cols[i], cols[j]
            w.writerow([a.attribute, a.table, a.name, a.type, a.values[0][:60],
                        b.table, b.name, b.type, b.values[0][:60]])

    if args.write:
        cand = methods[args.write][1]
        with (out / "candidates.csv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["table_a", "column_a", "table_b", "column_b", "true_match"])
            for i, j in sorted(cand):
                w.writerow([cols[i].table, cols[i].name, cols[j].table, cols[j].name, int((i, j) in true)])
        print(f"\nCandidates of '{methods[args.write][0]}' written to {out / 'candidates.csv'}")
    print(f"Report written to {out / 'reduction_report.csv'}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("evaluate", help="measure PC/RR of all candidate methods")
    e.add_argument("--analyze", default="top3", help="method whose losses are analysed")
    e.add_argument("--write", help="method whose candidates are written to candidates.csv")
    e.add_argument("--show", type=int, default=12)
    args = p.parse_args()
    {"evaluate": cmd_evaluate}[args.command](args)


if __name__ == "__main__":
    main()
