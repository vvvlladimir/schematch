#!/usr/bin/env python3
"""Build the shared Product schema from pairwise Schematch results (step 9).

Reads the first-line matrices of a Schematch run over all 4,950 shop pairs,
combines them as in tools/wdc_combine.py (six value matchers + RoleProfile,
candidate filter), and groups the columns of all 100 shops into clusters.
Each cluster is one attribute of the shared schema.

    .venv/bin/python tools/wdc_schema.py results/<run> --corpus all-opaque --readable all-readable

Clustering: average-linkage agglomerative clustering on distance = 1 - score.
Columns of the same table get distance 1 (they are never compared, so they
merge only through strong links to other shops). The tree is cut at several
distance thresholds; every cut is scored against the ground truth with
pair-wise precision/recall/F1 and compared with the pairwise decisions
(mutual top-1 per shop pair).

Column names are used only to score the result and to print readable
cluster members; matching itself sees hidden names.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wdc_candidates as wc  # noqa: E402
import wdc_combine as wcomb  # noqa: E402
import wdc_roles  # noqa: E402

csv.field_size_limit(sys.maxsize)
ROOT = Path(__file__).resolve().parent.parent
WDC = ROOT / "datasets-local" / "wdc"
THRESHOLDS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
MERGE_THRESHOLDS = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9]
CLUSTER_MERGE_THRESHOLDS = [0.5, 0.4, 0.3, 0.2]
FOCUS = ["name", "description", "price", "pricecurrency", "brand", "sku", "availability",
         "itemcondition", "pricevaliduntil", "aggregaterating.ratingvalue", "category", "mpn"]


class Columns:
    """All non-empty columns of the cleaned tables, with attribute, type and role profile."""

    def __init__(self):
        attrs = {}
        with (WDC / "ground_truth" / "column_attributes.csv").open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                attrs[(r["table"], int(r["column_index"]))] = r["shared_attribute"]
        self.ids = []          # index -> (table, column name)
        self.attribute = []
        self.type = []
        self.table_index = {}
        self.by_values = {}    # (table, value tuple) -> [column index, ...]
        raw_profiles = []
        for path in sorted((WDC / "clean").glob("*.csv")):
            table = path.stem
            with path.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.reader(fh))
            header, body = rows[0], rows[1:]
            profiles = wdc_roles.table_profiles(path)
            for c, name in enumerate(header):
                column = tuple(r[c] if c < len(r) else "" for r in body)
                values = [v for v in column if v]
                if not values or attrs[(table, c)] == "_exclude":
                    continue
                idx = len(self.ids)
                self.ids.append((table, name))
                self.attribute.append(attrs[(table, c)])
                self.type.append(wc.column_type(values))
                self.table_index.setdefault(table, []).append(idx)
                self.by_values.setdefault((wcomb.wc_safe(table), column), []).append(idx)
                raw_profiles.append(profiles[column])
        p = np.array(raw_profiles)
        sd = p.std(axis=0)
        sd[sd == 0] = 1.0
        self.profile = (p - p.mean(axis=0)) / sd
        self.table_of = np.array([self.ids[i][0] for i in range(len(self.ids))])

    def __len__(self):
        return len(self.ids)


def read_matrix(path: Path):
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    targets = rows[0][1:]
    sources = [r[0] for r in rows[1:]]
    scores = np.array([[float(x) for x in r[1:]] for r in rows[1:]], dtype=float)
    return sources, targets, scores


def scenario_columns(cols: Columns, corpus: str, scenario: str, side: str, safe_table: str):
    """Map hidden 'table.column' labels of one side to global column indices by values."""
    path = next((WDC / "scenarios" / corpus / "wdc" / scenario / side).glob("*.csv"))
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    pools = {}
    out = {}
    for c, name in enumerate(header):
        column = tuple(r[c] if c < len(r) else "" for r in body)
        key = (safe_table, column)
        pool = pools.setdefault(key, list(cols.by_values[key]))
        out[f"{path.stem}.{name}"] = pool.pop(0)
    return out


def compatible(a: str, b: str) -> bool:
    return a == b or wc.PASS2.get(a, a) == wc.PASS2.get(b, b)


def minmax(m: np.ndarray) -> np.ndarray:
    lo, hi = m.min(), m.max()
    return np.zeros_like(m) if hi - lo <= 1e-12 else (m - lo) / (hi - lo)


def pair_scores(true_pairs: set, predicted: set) -> dict:
    tp = len(true_pairs & predicted)
    p = tp / len(predicted) if predicted else 0.0
    r = tp / len(true_pairs) if true_pairs else 0.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0,
            "predicted": len(predicted), "tp": tp}


def cluster_pairs(labels: np.ndarray, cols: Columns) -> set:
    members = defaultdict(list)
    for i, c in enumerate(labels):
        members[c].append(i)
    out = set()
    for ids in members.values():
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                i, j = ids[x], ids[y]
                if cols.table_of[i] != cols.table_of[j]:
                    out.add((i, j) if i < j else (j, i))
    return out


def constrained_merge(edges, cols: Columns) -> np.ndarray:
    """Greedy merge along edges (score, i, j), strongest first. Two clusters are
    joined only if they share no shop: one column per shop and attribute."""
    n = len(cols)
    parent = list(range(n))
    tables = [{cols.table_of[i]} for i in range(n)]

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
    return np.array([find(i) for i in range(n)])


def merge_clusters(labels: np.ndarray, S: np.ndarray, cols: Columns, threshold: float) -> np.ndarray:
    """Second pass: join whole clusters with no shop in common, best average
    similarity first, while that average is at least `threshold`."""
    groups = {}
    for i, c in enumerate(labels):
        groups.setdefault(c, []).append(i)
    groups = {k: np.array(v) for k, v in groups.items()}
    shops = {k: set(cols.table_of[v]) for k, v in groups.items()}
    while True:
        best, pair = threshold, None
        keys = list(groups)
        for x in range(len(keys)):
            a = keys[x]
            for b in keys[x + 1:]:
                if shops[a] & shops[b]:
                    continue
                avg = S[np.ix_(groups[a], groups[b])].mean()
                if avg >= best:
                    best, pair = avg, (a, b)
        if pair is None:
            break
        a, b = pair
        groups[a] = np.concatenate([groups[a], groups.pop(b)])
        shops[a] |= shops.pop(b)
    out = np.empty(len(labels), dtype=int)
    for k, ids in groups.items():
        out[ids] = k
    return out


def recall_by_attribute(true_pairs: set, predicted: set, cols: Columns) -> dict:
    total, found = Counter(), Counter()
    for i, j in true_pairs:
        a = cols.attribute[i]
        total[a] += 1
        found[a] += (i, j) in predicted
    return {a: found[a] / total[a] for a in total}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path)
    ap.add_argument("--corpus", default="all-opaque")
    ap.add_argument("--readable", default="all-readable")
    ap.add_argument("--method", help="method name for the written schema (default: best F1)")
    args = ap.parse_args()

    cols = Columns()
    n = len(cols)
    scen_tables = {}
    with (WDC / "scenarios" / args.readable / "scenarios.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            scen_tables[r["scenario"]] = (r["source_table"], r["target_table"])

    run_dirs = [d for d in args.run.iterdir() if d.is_dir() and d.name.startswith("wdc-")]
    if len(run_dirs) != 1:
        sys.exit(f"expected one wdc-* dataset directory in {args.run}")
    S = np.zeros((n, n))
    pairwise = set()
    mutual_edges = []
    done = 0
    for scen_dir in sorted(run_dirs[0].iterdir()):
        out = scen_dir / "_outputs" / "MatchingStepLine1"
        files = sorted(out.glob("*.csv"))
        if not files:
            continue
        src_t, tgt_t = scen_tables[scen_dir.name]
        src_map = scenario_columns(cols, args.corpus, scen_dir.name, "source", wcomb.wc_safe(src_t))
        tgt_map = scenario_columns(cols, args.corpus, scen_dir.name, "target", wcomb.wc_safe(tgt_t))
        mats, sources, targets = [], None, None
        for f in files:
            s, t, m = read_matrix(f)
            if sources is None:
                sources, targets = s, t
            elif s != sources or t != targets:
                sys.exit(f"label order differs between matchers in {out}")
            mats.append(minmax(m))
        gi = np.array([src_map[s] for s in sources])
        gj = np.array([tgt_map[t] for t in targets])
        diff = cols.profile[gi][:, None, :] - cols.profile[gj][None, :, :]
        role = np.exp(-(diff ** 2).sum(axis=2) / (2 * cols.profile.shape[1]))
        mats.append(role)
        combined = np.mean(mats, axis=0)
        mask = np.array([[compatible(cols.type[a], cols.type[b]) for b in gj] for a in gi])
        combined = np.where(mask, combined, 0.0)
        S[np.ix_(gi, gj)] = combined
        S[np.ix_(gj, gi)] = combined.T
        for i, j in wcomb.mutual_top1(combined.tolist()):
            a, b = gi[i], gj[j]
            pairwise.add((a, b) if a < b else (b, a))
            mutual_edges.append((combined[i, j], a, b))
        done += 1
    print(f"{done} shop pairs read, {n} columns")

    by_attr = defaultdict(list)
    for i, a in enumerate(cols.attribute):
        by_attr[a].append(i)
    true_pairs = set()
    for ids in by_attr.values():
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                i, j = ids[x], ids[y]
                if cols.table_of[i] != cols.table_of[j]:
                    true_pairs.add((i, j))
    print(f"true pairs {len(true_pairs)}\n")

    D = 1.0 - S
    np.fill_diagonal(D, 0.0)
    Z = linkage(squareform(D, checks=False), method="average")

    results = [("pairwise mutual top-1", None, pairwise)]
    for t in THRESHOLDS:
        labels = fcluster(Z, t=t, criterion="distance")
        results.append((f"average linkage, cut {t}", labels, cluster_pairs(labels, cols)))
    labels = constrained_merge(mutual_edges, cols)
    results.append(("one-per-shop merge, mutual top-1", labels, cluster_pairs(labels, cols)))
    base = labels
    for t in CLUSTER_MERGE_THRESHOLDS:
        labels = merge_clusters(base, S, cols, t)
        results.append((f"  + join clusters, avg >= {t}", labels, cluster_pairs(labels, cols)))
    iu = np.triu_indices(n, k=1)
    for t in MERGE_THRESHOLDS:
        keep = S[iu] >= t
        edges = list(zip(S[iu][keep], iu[0][keep], iu[1][keep]))
        labels = constrained_merge(edges, cols)
        results.append((f"one-per-shop merge, score >= {t}", labels, cluster_pairs(labels, cols)))

    print(f"{'method':<38}{'clusters':>9}{'P':>7}{'R':>7}{'F1':>7}")
    scored = []
    for name, labels, pred in results:
        sc = pair_scores(true_pairs, pred)
        k = len(set(labels)) if labels is not None else "-"
        scored.append((name, labels, pred, sc))
        print(f"{name:<38}{k:>9}{sc['precision']:>7.3f}{sc['recall']:>7.3f}{sc['f1']:>7.3f}")

    if args.method:
        chosen = next(x for x in scored if x[0] == args.method)
    else:
        chosen = max(scored[1:], key=lambda x: x[3]["f1"])
    name, labels, pred, sc = chosen
    print(f"\nchosen: {name} (F1 {sc['f1']:.3f})")

    rec_pw = recall_by_attribute(true_pairs, pairwise, cols)
    rec_cl = recall_by_attribute(true_pairs, pred, cols)
    print(f"\n{'attribute':<30}{'pairwise R':>11}{'clusters R':>11}")
    for a in FOCUS:
        if a in rec_cl:
            print(f"{a:<30}{rec_pw[a]:>11.0%}{rec_cl[a]:>11.0%}")

    out_dir = args.run / "wdc_schema"
    out_dir.mkdir(exist_ok=True)
    with (out_dir / "clustering_report.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["method", "clusters", "precision", "recall", "f1", "predicted_pairs", "true_positives"])
        for nm, lb, _, s in scored:
            w.writerow([nm, len(set(lb)) if lb is not None else "", round(s["precision"], 4),
                        round(s["recall"], 4), round(s["f1"], 4), s["predicted"], s["tp"]])

    members = defaultdict(list)
    for i, c in enumerate(labels):
        members[c].append(i)
    rows = []
    for c, ids in members.items():
        attrs = Counter(cols.attribute[i] for i in ids)
        top, cnt = attrs.most_common(1)[0]
        rows.append({
            "cluster": 0, "shops": len({cols.table_of[i] for i in ids}), "columns": len(ids),
            "majority_attribute": top, "purity": round(cnt / len(ids), 3),
            "attributes": "; ".join(f"{a} ({k})" for a, k in attrs.most_common()),
            "members": "; ".join(f"{cols.ids[i][0]}:{cols.ids[i][1]}" for i in sorted(ids)),
        })
    rows.sort(key=lambda r: (-r["shops"], r["majority_attribute"]))
    for k, r in enumerate(rows, 1):
        r["cluster"] = k
    with (out_dir / "global_schema.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    big = [r for r in rows if r["shops"] >= 5]
    print(f"\nshared schema: {len(rows)} clusters, {len(big)} with at least 5 shops, "
          f"{sum(r['shops'] == 1 for r in rows)} single columns")
    print(f"{'#':>3} {'shops':>5} {'purity':>7}  majority attribute -> other attributes")
    for r in big[:25]:
        others = r["attributes"].split("; ")[1:4]
        print(f"{r['cluster']:>3} {r['shops']:>5} {r['purity']:>7.0%}  {r['majority_attribute']}"
              + (f"  [{'; '.join(others)}]" if others else ""))
    print(f"\nWritten to {out_dir}")


if __name__ == "__main__":
    main()
