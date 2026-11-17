#!/usr/bin/env python3
"""Combine Schematch score matrices and apply the candidate filter.

Steps 4 and 7 of docs/wdc-product-matching-plan.md, applied to the raw matrices
of a Schematch run (`<run>/paper_evaluation/input.jsonl`).

    python tools/wdc_combine.py results/<run> --corpus opaque

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

csv.field_size_limit(sys.maxsize)
ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "datasets-local" / "wdc" / "scenarios"

COMBINATIONS = {
    "Mean(all value matchers)": None,  # None = every first-line matcher in the run
    "Mean(CharDistribution, KS, PatternProfile)": [
        "CharDistributionMatcher", "KolmogorovSmirnovMatcher", "PatternProfileMatcher"],
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


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run", type=Path, help="results/<run> directory of a Schematch run")
    p.add_argument("--corpus", default="opaque", help="scenario corpus the run used")
    args = p.parse_args()

    records_path = args.run / "paper_evaluation" / "input.jsonl"
    by_scenario = defaultdict(list)
    with records_path.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            by_scenario[(r["dataset"], r["scenario"])].append(r)

    out_dir = args.run / "wdc_combined"
    out_dir.mkdir(exist_ok=True)
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

        def emit(name, scores):
            rec = {k: v for k, v in ref.items() if k not in ("scores", "matcher", "baseline", "stage", "wall_ms")}
            rec.update(stage="WdcCombiner", matcher=name, baseline=name, wall_ms=0.0, scores=scores)
            new_records.append(rec)

        for name, members in COMBINATIONS.items():
            ms = [norm[m] for m in (members or norm)]
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
