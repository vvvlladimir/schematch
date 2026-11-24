#!/usr/bin/env python3
"""Precision, recall and F1 of the WDC Product matching against the ground truth.

    .venv/bin/python tools/wdc_evaluate.py results/<run>

Reference: ground_truth/wdc_product/columns.csv (tools/wdc_gt.py). Two columns of
different shops correspond when they have the same concept. Columns that are
excluded or empty in the reference are left out of predictions and reference.

Scored, each as a set of column pairs over all 4,950 shop pairs:

- every matcher and their mean, selection mutual top-1 (as in the report);
- the mean with the two-pass type blocker as prefilter (tools/wdc_schema.py), and
  the same mean without the label matcher;
- the clustering (tools/wdc_schema.py): all pairs of columns in one cluster;
- MaxDelta + Both, delta 0.05 (tools/wdc_complex.py): all selected pairs.

    precision = correct predicted pairs / predicted pairs
    recall    = correct predicted pairs / reference pairs
    F1        = 2 * precision * recall / (precision + recall)

A 1:1 selection can find at most one partner per column and shop pair, while the
reference allows several (url.name and name both are `name`). The best possible
recall of any 1:1 selection is printed as well. The blockers are scored by the
share of reference pairs they keep (pair completeness) and their reduction ratio.

Caveat: the reference started from header names, so LevenshteinMatcher (which
compares names) and every mean containing it are favoured; "Mean of value
matchers" is the fair number for matching by values.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wdc_candidates as wc  # noqa: E402
import wdc_complex  # noqa: E402
import wdc_matches  # noqa: E402
import wdc_schema  # noqa: E402
import numpy as np  # noqa: E402
from wdc_scenarios import safe_name  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GT_COLUMNS = ROOT / "ground_truth" / "wdc_product" / "columns.csv"
DELTA = 0.05
LABEL_MATCHER = "LevenshteinMatcher"

Pair = tuple[int, int]


def norm(a: int, b: int) -> Pair:
    return (a, b) if a < b else (b, a)


def load_reference(cols, path: Path = GT_COLUMNS):
    """Concept per column index (only evaluated columns) and the reference pairs."""
    labels = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            labels[(r["table"], r["column"])] = r
    concept = {}
    missing = []
    for c in cols:
        r = labels.get((c.table, c.name))
        if r is None:
            missing.append(f"{c.table}.{c.name}")
        elif int(r["evaluated"]):
            concept[c.idx] = r["concept"]
    if missing:
        sys.exit(f"{len(missing)} columns of the run are not in {path}, e.g. {missing[:3]}; "
                 "rerun tools/wdc_prepare.py clean and tools/wdc_gt.py build")
    by_concept = defaultdict(list)
    for i, k in concept.items():
        by_concept[k].append(i)
    reference = set()
    for members in by_concept.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                a, b = members[x], members[y]
                if cols[a].table != cols[b].table:
                    reference.add(norm(a, b))
    return concept, reference


def best_one_to_one(reference: set[Pair], cols) -> int:
    """Largest number of reference pairs a 1:1 selection can contain: per shop pair,
    a maximum bipartite matching of the reference pairs (Kuhn's algorithm)."""
    by_shops = defaultdict(list)
    for a, b in reference:
        ta, tb = cols[a].table, cols[b].table
        if ta > tb:
            a, b = b, a
        by_shops[(cols[a].table, cols[b].table)].append((a, b))
    total = 0
    for edges in by_shops.values():
        adj = defaultdict(list)
        for a, b in edges:
            adj[a].append(b)
        match: dict[int, int] = {}

        def augment(u, seen):
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    if v not in match or augment(match[v], seen):
                        match[v] = u
                        return True
            return False

        total += sum(augment(u, set()) for u in adj)
    return total


def score(predicted: set[Pair], reference: set[Pair], keep: set[int]) -> dict:
    predicted = {p for p in predicted if p[0] in keep and p[1] in keep}
    tp = len(predicted & reference)
    p = tp / len(predicted) if predicted else 0.0
    r = tp / len(reference) if reference else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"predicted": len(predicted), "correct": tp, "precision": p, "recall": r, "f1": f}


def predictions(run: Path, cols) -> dict[str, set[Pair]]:
    out = defaultdict(set)
    index = {f"{safe_name(c.table)}.{c.name}": c.idx for c in cols}
    for name, found in wc.matcher_correspondences(run, cols).items():
        out[name if name != wdc_matches.MEAN else "Mean of all matchers"] = found
    edges = []
    for _, gi, gj, m in wdc_schema.filtered_scores(run, cols):
        for i, j in wdc_matches.mutual_top1(m):
            out["Mean + type blocker"].add(norm(gi[i], gj[j]))
            edges.append((m[i, j], gi[i], gj[j]))
        for i, j in wdc_complex.max_delta_both(m, DELTA):
            out[f"Mean + type blocker, MaxDelta {DELTA}"].add(norm(gi[i], gj[j]))
    # The same without the label matcher: the reference started from header names.
    for _, sources, targets, mats in wdc_matches.scenarios(run):
        gi = [index[x] for x in sources]
        gj = [index[x] for x in targets]
        m = np.mean([wdc_matches.minmax(v) for k, v in mats.items()
                     if k not in (wdc_matches.MEAN, LABEL_MATCHER)], axis=0)
        for i, a in enumerate(gi):
            for j, b in enumerate(gj):
                if not wdc_schema.compatible(cols[a].type, cols[b].type):
                    m[i, j] = 0.0
        for i, j in wdc_matches.mutual_top1(m):
            out["Mean of value matchers + type blocker"].add(norm(gi[i], gj[j]))
    labels = wdc_schema.constrained_merge(edges, [c.table for c in cols])
    members = defaultdict(list)
    for i, k in enumerate(labels):
        members[k].append(i)
    for ids in members.values():
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                out["Clustering (mean + type blocker)"].add(norm(ids[x], ids[y]))
    return dict(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run", type=Path, help="results/<run> of tools/wdc_scenarios.py run")
    ap.add_argument("--ground-truth", type=Path, default=GT_COLUMNS)
    ap.add_argument("--concepts", type=int, default=15, help="concepts shown in the per-concept table")
    args = ap.parse_args(argv)

    cols = wc.load()
    wc.profile(cols)
    concept, reference = load_reference(cols, args.ground_truth)
    keep = set(concept)
    bound = best_one_to_one(reference, cols)
    print(f"{len(keep)} evaluated columns, {len(reference)} reference pairs; "
          f"a 1:1 selection can reach at most {bound} of them (recall {bound / len(reference):.1%})")

    preds = predictions(args.run, cols)
    order = sorted(preds, key=lambda n: (n.startswith(("Mean", "Clustering")), n))
    rows = []
    print(f"\n{'method':<42}{'predicted':>10}{'correct':>9}{'P':>8}{'R':>8}{'F1':>8}")
    for name in order:
        s = score(preds[name], reference, keep)
        rows.append({"method": name, **s})
        print(f"{name:<42}{s['predicted']:>10}{s['correct']:>9}"
              f"{s['precision']:>8.1%}{s['recall']:>8.1%}{s['f1']:>8.1%}")

    total = wc.all_pairs(cols)
    print(f"\n{'blocker':<52}{'RR':>8}{'recall':>8}")
    for label, cand in wc.blockers(cols).items():
        kept = {p for p in cand if p[0] in keep and p[1] in keep}
        rec = len(kept & reference) / len(reference)
        rows.append({"method": f"Blocker: {label}", "predicted": len(cand),
                     "correct": len(kept & reference), "precision": "", "recall": rec, "f1": "",
                     "RR": 1 - len(cand) / total})
        print(f"{label:<52}{1 - len(cand) / total:>8.1%}{rec:>8.1%}")

    best = "Mean of value matchers + type blocker"
    pred = {p for p in preds[best] if p[0] in keep and p[1] in keep}
    per = defaultdict(Counter)
    for a, b in reference:
        per[concept[a]]["reference"] += 1
        per[concept[a]]["found"] += (a, b) in pred
    for a, b in pred:
        for k in {concept[a], concept[b]}:
            per[k]["predicted"] += 1
            per[k]["correct"] += concept[a] == concept[b]
    print(f"\nper concept, {best} (precision over predicted pairs with a column of the concept)")
    print(f"{'concept':<32}{'reference':>10}{'P':>8}{'R':>8}")
    for k, c in sorted(per.items(), key=lambda x: -x[1]["reference"])[:args.concepts]:
        p = c["correct"] / c["predicted"] if c["predicted"] else 0.0
        print(f"{k:<32}{c['reference']:>10}{p:>8.1%}{c['found'] / c['reference']:>8.1%}")

    out = args.run / "evaluation_ground_truth.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["method", "predicted", "correct", "precision", "recall", "f1", "RR"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
