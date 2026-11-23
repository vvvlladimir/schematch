"""Read the score matrices of a Schematch run over the WDC shop pairs.

Shared by tools/wdc_candidates.py (blocker evaluation) and tools/wdc_schema.py
(clustering). A run directory is results/<run>/wdc/<scenario>/_outputs/MatchingStepLine1/
with one CSV per matcher; rows are source columns, columns are target columns,
labels are 'table.column'.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

csv.field_size_limit(sys.maxsize)

MEAN = "Mean of all matchers"


def read_matrix(path: Path):
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    targets = rows[0][1:]
    sources = [r[0] for r in rows[1:]]
    scores = np.array([[float(x) for x in r[1:]] for r in rows[1:]], dtype=float)
    return sources, targets, scores


def minmax(m: np.ndarray) -> np.ndarray:
    lo, hi = m.min(), m.max()
    return np.zeros_like(m) if hi - lo <= 1e-12 else (m - lo) / (hi - lo)


def mutual_top1(m: np.ndarray) -> list[tuple[int, int]]:
    """Cells (i, j) with a positive score that is the best of row i and of column j; ties kept."""
    if m.size == 0:
        return []
    best = (m >= m.max(axis=1, keepdims=True) - 1e-12) & (m >= m.max(axis=0, keepdims=True) - 1e-12) & (m > 1e-9)
    return list(zip(*np.nonzero(best)))


def matcher_name(path: Path) -> str:
    """'PMFMatcher(distance=L2).csv' -> 'PMFMatcher'"""
    return path.stem.split("(", 1)[0]


def scenarios(run: Path):
    """Yield (scenario, source labels, target labels, {matcher: matrix}) for every shop pair,
    including the mean of the min-max normalized matrices under MEAN."""
    root = run / "wdc"
    if not root.is_dir():
        sys.exit(f"{root} missing: expected a run of tools/wdc_scenarios.py")
    for scen in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted((scen / "_outputs" / "MatchingStepLine1").glob("*.csv"))
        if not files:
            continue
        mats, sources, targets = {}, None, None
        for f in files:
            s, t, m = read_matrix(f)
            if sources is None:
                sources, targets = s, t
            elif s != sources or t != targets:
                sys.exit(f"label order differs between matchers in {scen}")
            mats[matcher_name(f)] = m
        mats[MEAN] = np.mean([minmax(m) for m in mats.values()], axis=0)
        yield scen.name, sources, targets, mats
