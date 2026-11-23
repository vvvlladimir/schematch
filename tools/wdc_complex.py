#!/usr/bin/env python3
"""Derive 1:n, n:1 and n:m correspondences per shop pair (step 1, last item of the
"Vorgehensweise"; section 6 of docs/wdc-product-matching-report.md).

    .venv/bin/python tools/wdc_complex.py results/<run>
    .venv/bin/python tools/wdc_complex.py results/<run> --delta 0.1

Selection MaxDelta + Both, as in COMA (Do & Rahm; Massmann et al. 2011): a cell
(i, j) of the combined matrix (tools/wdc_schema.py: mean of all matchers, two-pass
type blocker as prefilter) is selected when its score is within a relative
distance `delta` of the best score of row i AND of the best score of column j:

    m[i, j] >= (1 - delta) * max(row i)   and   m[i, j] >= (1 - delta) * max(column j)

delta = 0 is the mutual top-1 used for the 1:1 correspondences. With delta > 0 a
column can keep several near-best partners. The selected cells of one shop pair
form a bipartite graph; each connected component is one correspondence, typed by
its number of source and target columns: 1:1, 1:n, n:1 or n:m.
Column names are used only to print and count recurring patterns.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wdc_candidates as wc  # noqa: E402
import wdc_schema  # noqa: E402

DELTAS = [0.0, 0.02, 0.05, 0.1, 0.2]


def max_delta_both(m: np.ndarray, delta: float) -> list[tuple[int, int]]:
    if m.size == 0:
        return []
    keep = ((m >= (1 - delta) * m.max(axis=1, keepdims=True) - 1e-12)
            & (m >= (1 - delta) * m.max(axis=0, keepdims=True) - 1e-12) & (m > 1e-9))
    return list(zip(*np.nonzero(keep)))


def components(cells: list[tuple[int, int]]) -> list[tuple[list[int], list[int]]]:
    """Connected components of the bipartite graph of selected cells: (rows, columns)."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in cells:
        parent[find(("s", i))] = find(("t", j))
    groups = {}
    for node in list(parent):
        groups.setdefault(find(node), []).append(node)
    return [(sorted(k for side, k in g if side == "s"), sorted(k for side, k in g if side == "t"))
            for g in groups.values()]


def kind(n_source: int, n_target: int) -> str:
    if n_source == 1:
        return "1:1" if n_target == 1 else "1:n"
    return "n:1" if n_target == 1 else "n:m"


def pattern(left: list[str], right: list[str]) -> str:
    """Orientation-free name pattern, the smaller side first."""
    a, b = sorted(set(left)), sorted(set(right))
    if (len(a), a) > (len(b), b):
        a, b = b, a
    return f"{' + '.join(a)}  <->  {' + '.join(b)}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path, help="results/<run> of tools/wdc_scenarios.py run")
    ap.add_argument("--delta", type=float, default=0.05, help="relative MaxDelta distance for the written result")
    ap.add_argument("--show", type=int, default=15)
    args = ap.parse_args()

    cols = wc.load()
    wc.profile(cols)
    shop_pairs = list(wdc_schema.filtered_scores(args.run, cols))
    deltas = sorted(set(DELTAS) | {args.delta})

    print(f"{len(shop_pairs)} shop pairs; correspondences by type for each delta\n")
    print(f"{'delta':>6}{'1:1':>9}{'1:n':>8}{'n:1':>8}{'n:m':>8}{'columns in complex':>21}")
    rows = []
    for delta in deltas:
        counts, in_complex = Counter(), 0
        for scenario, gi, gj, m in shop_pairs:
            for r, c in components(max_delta_both(m, delta)):
                k = kind(len(r), len(c))
                counts[k] += 1
                if k != "1:1":
                    in_complex += len(r) + len(c)
                if delta == args.delta:
                    rows.append({
                        "scenario": scenario, "type": k,
                        "source_columns": "; ".join(f"{cols[gi[i]].table}:{cols[gi[i]].name}" for i in r),
                        "target_columns": "; ".join(f"{cols[gj[j]].table}:{cols[gj[j]].name}" for j in c),
                        "min_score": round(float(min(m[i, j] for i in r for j in c)), 4),
                        "pattern": pattern([cols[gi[i]].name for i in r], [cols[gj[j]].name for j in c]),
                    })
        print(f"{delta:>6}{counts['1:1']:>9}{counts['1:n']:>8}{counts['n:1']:>8}{counts['n:m']:>8}{in_complex:>21}")

    complex_rows = [r for r in rows if r["type"] != "1:1"]
    print(f"\ndelta {args.delta}: most frequent 1:n / n:1 / n:m patterns (over all shop pairs)\n")
    for p, n in Counter(r["pattern"] for r in complex_rows).most_common(args.show):
        print(f"  {n:>5}  {p}")

    out = args.run / "complex_correspondences.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["type"] == "1:1", r["scenario"])))
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()
