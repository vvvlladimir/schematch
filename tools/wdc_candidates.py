#!/usr/bin/env python3
"""Search-space reduction (blockers) for the WDC Product tables, step 3 of
docs/wdc-product-matching-report.md.

    .venv/bin/python tools/wdc_candidates.py evaluate results/<run>

There is no ground truth. As proposed in the course (Einführung, slide 13), every
matcher is run on all column pairs of all shop pairs (tools/wdc_scenarios.py);
the correspondences it selects (mutual top-1) serve as its reference. For each
blocker and matcher:

    PC = matcher correspondences kept by the blocker / all matcher correspondences
    RR = 1 - candidates / all cross-table column pairs

A blocker suits a matcher when it keeps almost all of that matcher's
correspondences (PC near 100%) while removing many pairs (high RR).
The blockers look only at values, never at column names.
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

import wdc_matches
from wdc_scenarios import safe_name

csv.field_size_limit(sys.maxsize)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "datasets-local" / "wdc"
SAMPLE = 100  # values per column used for profiling


# ------------------------------------------------------------------ data --

class Column:
    __slots__ = ("idx", "table", "name", "values", "type", "tokens", "features")

    def __init__(self, idx, table, name, values):
        self.idx, self.table, self.name, self.values = idx, table, name, values


def load(base: Path = DEFAULT_DIR) -> list[Column]:
    """Non-empty columns of the cleaned tables (empty ones cannot be matched from values)."""
    cols = []
    for path in sorted((base / "clean").glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        header, body = rows[0], rows[1:]
        for c, name in enumerate(header):
            values = [r[c] for r in body if c < len(r) and r[c]]
            if values:
                cols.append(Column(len(cols), path.stem, name, values))
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

def all_pairs(cols):
    per_table = Counter(c.table for c in cols)
    n = sum(per_table.values())
    return (n * n - sum(v * v for v in per_table.values())) // 2


def blockers(cols) -> dict[str, set[tuple[int, int]]]:
    type3 = type_blocking_three_pass(cols)
    out = {
        "Type blocking, one pass": blocking(cols, lambda c: c.type),
        "Type blocking, two passes": type_blocking_two_pass(cols),
        "Type blocking, three passes": type3,
    }
    for k in (3, 1):
        out[f"Three passes + top-{k} per table"] = top_k(cols, type3, k)
    out["Three passes + size filter (distinct ratio >= 0.1)"] = size_filter(cols, type3)
    out["Inverted index on value tokens"] = inverted_index(cols)
    out["MinHash + LSH on value tokens (b=16, r=4)"] = minhash_lsh(cols)
    return out


def matcher_correspondences(run: Path, cols) -> dict[str, set[tuple[int, int]]]:
    """Mutual top-1 pairs of every matcher over all shop pairs, as global column index pairs."""
    index = {f"{safe_name(c.table)}.{c.name}": c.idx for c in cols}
    found = defaultdict(set)
    n = 0
    for _, sources, targets, mats in wdc_matches.scenarios(run):
        gi, gj = [index[s] for s in sources], [index[t] for t in targets]
        for matcher, m in mats.items():
            for i, j in wdc_matches.mutual_top1(m):
                a, b = gi[i], gj[j]
                found[matcher].add((a, b) if a < b else (b, a))
        n += 1
    print(f"{n} shop pairs read from {run}")
    return dict(found)


def cmd_evaluate(args):
    cols = load(args.dir)
    profile(cols)
    total = all_pairs(cols)
    print(f"non-empty columns {len(cols)}, all cross-table pairs {total}")
    print("column types:", dict(Counter(c.type for c in cols).most_common()))
    ref = matcher_correspondences(args.run, cols)
    names = sorted(ref, key=lambda m: (m == "Mean of all matchers", m))
    print("\ncorrespondences per matcher (mutual top-1):")
    for m in names:
        print(f"  {m:<28} {len(ref[m]):>7}")

    rows = []
    for label, cand in blockers(cols).items():
        row = {"blocker": label, "candidates": len(cand), "RR": round(1 - len(cand) / total, 4)}
        for m in names:
            row[m] = round(len(cand & ref[m]) / len(ref[m]), 4)
        rows.append(row)

    short = {m: m.replace("Matcher", "")[:10] for m in names}
    print(f"\nPC per matcher (share of its correspondences the blocker keeps)\n")
    print(f"{'blocker':<52}{'RR':>7}" + "".join(f"{short[m]:>11}" for m in names))
    for r in rows:
        print(f"{r['blocker']:<52}{r['RR']:>7.1%}" + "".join(f"{r[m]:>11.1%}" for m in names))

    out = args.run / "blocker_evaluation.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\nwritten to {out}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("evaluate", help="PC/RR of every blocker against every matcher's correspondences")
    e.add_argument("run", type=Path, help="results/<run> of tools/wdc_scenarios.py run")
    args = p.parse_args()
    {"evaluate": cmd_evaluate}[args.command](args)


if __name__ == "__main__":
    main()
