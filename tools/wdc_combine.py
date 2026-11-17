#!/usr/bin/env python3
"""Combine Schematch score matrices and apply the candidate filter.

Steps 4 and 7 of docs/wdc-product-matching-plan.md, applied to the raw matrices
of a Schematch run (`<run>/paper_evaluation/input.jsonl`).

    python tools/wdc_combine.py results/<run> --corpus opaque
    python tools/wdc_combine.py results/<run> --analyze "Mean(all value matchers) + candidate filter"

For every scenario it adds records for:
  * the mean of several min-max normalized matrices (combiner);
  * the same with non-candidate cells set to 0 (candidate filter from
    tools/wdc_candidates.py: two-pass type blocking, computed from the values
    of the scenario tables, never from their column names);
  * the best single matcher with the candidate filter.
Then it runs tools/evaluate_paper.py on all records (original + new) and prints
a summary. Run it with the interpreter that has NumPy/SciPy (.venv/bin/python).
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wdc_candidates as wc  # noqa: E402
import wdc_roles  # noqa: E402

csv.field_size_limit(sys.maxsize)
ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "datasets-local" / "wdc" / "scenarios"

ROLE = "RoleProfile"
SCHEMATCH_MATCHERS = ["PMFMatcher", "KolmogorovSmirnovMatcher", "JensenShannonValueMatcher",
                      "ValueShapeRoleMatcher", "CharDistributionMatcher", "PatternProfileMatcher"]
COMBINATIONS = {
    "Mean(all value matchers)": SCHEMATCH_MATCHERS,
    "Mean(CharDistribution, KS, PatternProfile)": [
        "CharDistributionMatcher", "KolmogorovSmirnovMatcher", "PatternProfileMatcher"],
    "Mean(all value matchers + RoleProfile)": SCHEMATCH_MATCHERS + [ROLE],
    "Mean(all value matchers + 2x RoleProfile)": SCHEMATCH_MATCHERS + [ROLE, ROLE],
    # Equal weight: half the six Schematch matchers, half RoleProfile.
    "50% value matchers + 50% RoleProfile": SCHEMATCH_MATCHERS + [ROLE] * len(SCHEMATCH_MATCHERS),
    "RoleProfile": [ROLE],
}
BEST_SINGLE = "CharDistributionMatcher"


def short(matcher: str) -> str:
    return matcher.split("(", 1)[0]


def minmax(m: list[list[float]]) -> list[list[float]]:
    flat = [x for row in m for x in row]
    lo, hi = min(flat), max(flat)
    if hi - lo <= 1e-12:
        return [[0.0] * len(row) for row in m]
    return [[(x - lo) / (hi - lo) for x in row] for row in m]


def mean(ms: list[list[list[float]]]) -> list[list[float]]:
    return [[sum(m[i][j] for m in ms) / len(ms) for j in range(len(ms[0][0]))]
            for i in range(len(ms[0]))]


def column_types(scenario_dir: Path) -> dict[str, str]:
    """label 'table.column' -> value type, read from the scenario's own tables."""
    types = {}
    for side in ("source", "target"):
        for path in (scenario_dir / side).glob("*.csv"):
            with path.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.reader(fh))
            header, body = rows[0], rows[1:]
            for c, name in enumerate(header):
                values = [r[c] for r in body if c < len(r) and r[c]]
                types[f"{path.stem}.{name}"] = wc.column_type(values) if values else "empty"
    return types


def candidate_mask(src_labels, tgt_labels, types) -> list[list[int]]:
    def ok(a, b):
        return a == b or wc.PASS2.get(a, a) == wc.PASS2.get(b, b)
    return [[int(ok(types[s], types[t])) for t in tgt_labels] for s in src_labels]


def masked(m, mask):
    return [[x if k else 0.0 for x, k in zip(row, krow)] for row, krow in zip(m, mask)]


# --------------------------------------------------------- error analysis --

def read_table(path: Path):
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    cols = [tuple(r[c] if c < len(r) else "" for r in body) for c in range(len(header))]
    return header, cols


def readable_names(corpus: str, scenario: str, readable: str = "readable") -> dict[str, str]:
    """Map 'table.column' labels of a hidden-name scenario to readable column names
    by identical column values (names were hashed, values were not touched)."""
    if corpus == readable:
        return {}
    out = {}
    for side in ("source", "target"):
        hidden = next((SCENARIOS / corpus / "wdc" / scenario / side).glob("*.csv"))
        plain = next((SCENARIOS / readable / "wdc" / scenario / side).glob("*.csv"))
        hh, hc = read_table(hidden)
        ph, pc = read_table(plain)
        pool = defaultdict(list)
        for name, values in zip(ph, pc):
            pool[values].append(name)
        for name, values in zip(hh, hc):
            out[f"{hidden.stem}.{name}"] = pool[values].pop(0)
    return out


def mutual_top1(scores):
    """Pairs (i, j) where j is a best target of i and i a best source of j; ties kept."""
    rows = [max(r) for r in scores]
    cols = [max(scores[i][j] for i in range(len(scores))) for j in range(len(scores[0]))]
    return {(i, j) for i, r in enumerate(scores) for j, x in enumerate(r)
            if x > 1e-9 and x >= rows[i] - 1e-12 and x >= cols[j] - 1e-12}


def analyze(args) -> None:
    attr_of = {}
    with (ROOT / "datasets-local" / "wdc" / "ground_truth" / "column_attributes.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            attr_of[(wc_safe(r["table"]), r["column"])] = r["shared_attribute"]
    scen_tables = {}
    with (SCENARIOS / args.readable / "scenarios.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            scen_tables[r["scenario"]] = (wc_safe(r["source_table"]), wc_safe(r["target_table"]))

    path = args.run / "wdc_combined" / "input.jsonl"
    stats = defaultdict(lambda: {"true": 0, "found": 0, "predicted": 0, "correct": 0})
    confusion = defaultdict(int)
    missed_to = defaultdict(int)
    type_stats = defaultdict(lambda: {"true": 0, "found": 0})
    n = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r["matcher"] != args.analyze:
                continue
            n += 1
            names = readable_names(args.corpus, r["scenario"], args.readable)
            st, tt = scen_tables[r["scenario"]]
            sa = [attr_of[(st, names.get(l, l.split(".", 1)[1]))] for l in r["source_labels"]]
            ta = [attr_of[(tt, names.get(l, l.split(".", 1)[1]))] for l in r["target_labels"]]
            scen_dir = SCENARIOS / args.corpus / "wdc" / r["scenario"]
            types = column_types(scen_dir)
            pred = mutual_top1(r["scores"])
            gt = r["ground_truth"]
            for i, row in enumerate(gt):
                for j, g in enumerate(row):
                    if g:
                        stats[sa[i]]["true"] += 1
                        key = wc.PASS2.get(types[r["source_labels"][i]], types[r["source_labels"][i]])
                        type_stats[key]["true"] += 1
                        if (i, j) in pred:
                            stats[sa[i]]["found"] += 1
                            type_stats[key]["found"] += 1
                        else:
                            best = [t for (a, t) in pred if a == i]
                            missed_to[(sa[i], ta[best[0]] if best else "(nothing)")] += 1
            for i, j in pred:
                stats[sa[i]]["predicted"] += 1
                if gt[i][j]:
                    stats[sa[i]]["correct"] += 1
                else:
                    confusion[(sa[i], ta[j])] += 1
    if not n:
        sys.exit(f"no records for method {args.analyze!r}; run the combine step first")

    out = args.run / "wdc_combined" / "errors_by_attribute.csv"
    rows = []
    for a, s in stats.items():
        if not s["true"] and not s["predicted"]:
            continue
        rows.append({"attribute": a, "true_pairs": s["true"], "found": s["found"],
                     "recall": round(s["found"] / s["true"], 3) if s["true"] else "",
                     "predicted": s["predicted"],
                     "precision": round(s["correct"] / s["predicted"], 3) if s["predicted"] else ""})
    rows.sort(key=lambda x: -x["true_pairs"])
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print(f"{args.analyze}, mutual top-1, {n} scenarios (source-side attribute)\n")
    print(f"{'attribute':<30}{'true':>6}{'found':>7}{'recall':>8}{'pred':>6}{'prec':>7}")
    for x in rows[:args.show]:
        rec = f"{x['recall']:.0%}" if x["recall"] != "" else "-"
        pre = f"{x['precision']:.0%}" if x["precision"] != "" else "-"
        print(f"{x['attribute'][:29]:<30}{x['true_pairs']:>6}{x['found']:>7}{rec:>8}{x['predicted']:>6}{pre:>7}")
    print("\nby column type (recall):")
    for t, s in sorted(type_stats.items(), key=lambda kv: -kv[1]["true"]):
        print(f"  {t:<12} {s['found']:>4}/{s['true']:<4} {s['found'] / s['true']:.0%}")
    print("\nmost frequent wrong pairs (source attribute -> predicted target attribute):")
    for (a, b), c in sorted(confusion.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {c:>4}  {a} -> {b}")
    print("\nmost frequent misses (true source attribute -> what it was matched to instead):")
    for (a, b), c in sorted(missed_to.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {c:>4}  {a} -> {b}")
    print(f"\nWritten to {out}")


def wc_safe(table: str) -> str:
    import re
    return re.sub(r"_+", "_", re.sub(r"[^0-9A-Za-z]", "_", table)).strip("_")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run", type=Path, help="results/<run> directory of a Schematch run")
    p.add_argument("--corpus", default="opaque", help="scenario corpus the run used")
    p.add_argument("--readable", default="readable", help="readable twin of that corpus")
    p.add_argument("--analyze", metavar="METHOD",
                   help="error analysis per attribute for METHOD (after a combine run)")
    p.add_argument("--show", type=int, default=25)
    args = p.parse_args()
    if args.analyze:
        analyze(args)
        return

    records_path = args.run / "paper_evaluation" / "input.jsonl"
    by_scenario = defaultdict(list)
    with records_path.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            by_scenario[(r["dataset"], r["scenario"])].append(r)

    out_dir = args.run / "wdc_combined"
    out_dir.mkdir(exist_ok=True)
    corpus_dir = SCENARIOS / args.corpus / "wdc"
    roles = wdc_roles.RoleMatcher(sorted(corpus_dir.glob("*/*/*.csv")))
    new_records = []
    kept_true = all_true = kept_cells = all_cells = 0
    for (dataset, scenario), recs in sorted(by_scenario.items()):
        first = [r for r in recs if r["stage"] == "MatchingStepLine1"]
        ref = first[0]
        types = column_types(SCENARIOS / args.corpus / "wdc" / scenario)
        mask = candidate_mask(ref["source_labels"], ref["target_labels"], types)
        for mrow, grow in zip(mask, ref["ground_truth"]):
            for k, g in zip(mrow, grow):
                all_cells += 1
                kept_cells += k
                all_true += g
                kept_true += k and g
        norm = {short(r["matcher"]): minmax(r["scores"]) for r in first}
        prof = {}
        for side in ("source", "target"):
            for path in (corpus_dir / scenario / side).glob("*.csv"):
                prof.update(roles.label_profiles(path))
        norm[ROLE] = [[roles.similarity(prof.get(s), prof.get(t)) for t in ref["target_labels"]]
                      for s in ref["source_labels"]]

        def emit(name, scores):
            rec = {k: v for k, v in ref.items() if k not in ("scores", "matcher", "baseline", "stage", "wall_ms")}
            rec.update(stage="WdcCombiner", matcher=name, baseline=name, wall_ms=0.0, scores=scores)
            new_records.append(rec)

        for name, members in COMBINATIONS.items():
            ms = [norm[m] for m in members]
            combined = mean(ms)
            emit(name, combined)
            emit(f"{name} + candidate filter", masked(combined, mask))
        emit(f"{BEST_SINGLE} + candidate filter", masked(norm[BEST_SINGLE], mask))

    combined_path = out_dir / "input.jsonl"
    with combined_path.open("w", encoding="utf-8") as fh:
        with records_path.open(encoding="utf-8") as src:
            for line in src:
                fh.write(line)
        for r in new_records:
            fh.write(json.dumps(r) + "\n")

    print(f"Candidate filter on {len(by_scenario)} scenarios: keeps {kept_true}/{all_true} true pairs "
          f"(PC {kept_true / all_true:.1%}), removes {all_cells - kept_cells}/{all_cells} cells "
          f"(RR {1 - kept_cells / all_cells:.1%})")
    report = out_dir / "report"
    subprocess.run([sys.executable, str(ROOT / "tools" / "evaluate_paper.py"),
                    "--records", str(combined_path), "--out", str(report)], check=True,
                   stdout=subprocess.DEVNULL)
    with (report / "summary.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    rows.sort(key=lambda r: -float(r["F1_H"]))
    print(f"\n{'matcher':<62}{'F1_H':>7}{'F1_M':>7}{'MRR':>7}{'R@GT':>7}")
    for r in rows:
        print(f"{r['matcher'][:61]:<62}{float(r['F1_H']):>7.3f}{float(r['F1_M']):>7.3f}"
              f"{float(r['MRR_exp']):>7.3f}{float(r['R_exp']):>7.3f}")
    print(f"\nReport written to {report}")


if __name__ == "__main__":
    main()
