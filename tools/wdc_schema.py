#!/usr/bin/env python3
"""Group the columns of all shops into a shared Product schema (step 4 of
docs/wdc-product-matching-report.md).

    .venv/bin/python tools/wdc_schema.py results/<run>

For every shop pair the matcher scores are min-max normalized and averaged; the
type blocker (two passes, tools/wdc_candidates.py) is the prefilter: pairs it
removes get score 0. The mutual top-1 pairs are the pairwise correspondences.
Clustering joins them strongest first and refuses any merge that would put two
columns of the same shop into one cluster (one column per shop and attribute).
Each cluster is one attribute of the shared schema. There is no ground truth, so
the clusters are not scored; they are written for inspection.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wdc_candidates as wc  # noqa: E402
import wdc_matches  # noqa: E402
from wdc_scenarios import safe_name  # noqa: E402


def compatible(a: str, b: str) -> bool:
    """Same rule as the two-pass type blocker."""
    return a == b or wc.PASS2.get(a, a) == wc.PASS2.get(b, b)


def constrained_merge(edges, table_of: list[str]) -> list[int]:
    """Greedy merge along edges (score, i, j), strongest first. Two clusters are
    joined only if they share no shop."""
    parent = list(range(len(table_of)))
    tables = [{t} for t in table_of]

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, i, j in sorted(edges, reverse=True):
        a, b = find(i), find(j)
        if a == b or tables[a] & tables[b]:
            continue
        if len(tables[a]) < len(tables[b]):
            a, b = b, a
        parent[b] = a
        tables[a] |= tables[b]
    return [find(i) for i in range(len(table_of))]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path, help="results/<run> of tools/wdc_scenarios.py run")
    ap.add_argument("--show", type=int, default=25)
    args = ap.parse_args()

    cols = wc.load()
    wc.profile(cols)
    index = {f"{safe_name(c.table)}.{c.name}": c.idx for c in cols}
    edges, pairs = [], 0
    for _, sources, targets, mats in wdc_matches.scenarios(args.run):
        gi, gj = [index[s] for s in sources], [index[t] for t in targets]
        m = mats[wdc_matches.MEAN].copy()
        for i, a in enumerate(gi):
            for j, b in enumerate(gj):
                if not compatible(cols[a].type, cols[b].type):
                    m[i, j] = 0.0
        for i, j in wdc_matches.mutual_top1(m):
            edges.append((m[i, j], gi[i], gj[j]))
        pairs += 1
    print(f"{pairs} shop pairs, {len(cols)} columns, {len(edges)} pairwise correspondences")

    labels = constrained_merge(edges, [c.table for c in cols])
    members = defaultdict(list)
    for i, k in enumerate(labels):
        members[k].append(i)
    rows = []
    for ids in members.values():
        names = Counter(cols[i].name for i in ids)
        rows.append({
            "cluster": 0, "shops": len(ids),
            "most_common_name": names.most_common(1)[0][0],
            "names": "; ".join(f"{n} ({k})" for n, k in names.most_common()),
            "members": "; ".join(f"{cols[i].table}:{cols[i].name}" for i in sorted(ids)),
        })
    rows.sort(key=lambda r: (-r["shops"], r["most_common_name"]))
    for k, r in enumerate(rows, 1):
        r["cluster"] = k
    out = args.run / "global_schema.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    big = [r for r in rows if r["shops"] >= 5]
    print(f"shared schema: {len(rows)} clusters, {len(big)} with at least 5 shops, "
          f"{sum(r['shops'] == 1 for r in rows)} single columns\n")
    print(f"{'#':>3} {'shops':>5}  most common column names")
    for r in big[:args.show]:
        print(f"{r['cluster']:>3} {r['shops']:>5}  {r['names'][:110]}")
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    main()
