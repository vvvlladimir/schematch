#!/usr/bin/env python3
"""Role profile matcher: compare columns by the role they play in their own table.

A language-independent instance + structure matcher (course: matcher classes of
Rahm & Bernstein; "use types, structure and instances"). It needs no model and
no column names. Every column is described by a few numbers:

  distinct      share of distinct values (brands and categories repeat, names do not)
  words         log of the mean number of words (names ~6, descriptions ~36)
  length        log of the mean number of characters
  inside        share of rows where the value occurs inside a longer value of
                another column of the same row (a brand inside the product name)
  contains      share of rows where the value contains a shorter value of another
                column of the same row (a description containing the name)
  numeric       share of values that are numbers
  in_0_5        share of numeric values between 0 and 5 (ratings)
  integer       share of numeric values that are integers (counts, IDs)
  magnitude     log10 of the median numeric value

Two columns are similar when their standardized profiles are close:
similarity = exp(-distance^2 / (2 * dims)).

    python tools/wdc_roles.py datasets-local/wdc/clean/<table>.csv   # print profiles
"""
from __future__ import annotations

import csv
import hashlib
import math
import statistics
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)

FEATURES = ["distinct", "words", "length", "inside", "contains",
            "numeric", "in_0_5", "integer", "magnitude"]
MIN_CONTAIN_LEN = 3  # ignore very short values ("1", "-", "a") in containment checks

_cache: dict[str, dict[tuple, list[float]]] = {}


def _number(v: str) -> float | None:
    try:
        x = float(v)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def table_profiles(path: Path) -> dict[tuple, list[float]]:
    """Raw (unstandardized) profile per column, keyed by the column's value tuple,
    so the result does not depend on column names or column order."""
    data = path.read_bytes()
    key = hashlib.sha1(data).hexdigest()
    if key in _cache:
        return _cache[key]
    rows = list(csv.reader(data.decode("utf-8").splitlines(keepends=True)))
    header, body = rows[0], rows[1:]
    width = len(header)
    body = [r + [""] * (width - len(r)) for r in body]
    lowered = [[v.strip().lower() for v in r] for r in body]
    out = {}
    for c in range(width):
        column = tuple(r[c] for r in body)
        values = [v for v in column if v]
        if not values:
            continue
        inside = contains = n = 0
        for r in lowered:
            v = r[c]
            if not v:
                continue
            n += 1
            others = [o for k, o in enumerate(r) if k != c and len(o) >= MIN_CONTAIN_LEN]
            if len(v) >= MIN_CONTAIN_LEN and any(len(o) > len(v) and v in o for o in others):
                inside += 1
            if any(len(o) < len(v) and o in v for o in others):
                contains += 1
        nums = [x for x in (_number(v) for v in values) if x is not None]
        out[column] = [
            len(set(values)) / len(values),
            math.log1p(statistics.mean(len(v.split()) for v in values)),
            math.log1p(statistics.mean(len(v) for v in values)),
            inside / n,
            contains / n,
            len(nums) / len(values),
            sum(0 <= x <= 5 for x in nums) / len(nums) if nums else 0.0,
            sum(x == int(x) for x in nums) / len(nums) if nums else 0.0,
            math.log10(abs(statistics.median(nums)) + 1) if nums else 0.0,
        ]
    _cache[key] = out
    return out


class RoleMatcher:
    """Standardizes profiles over all tables it is given, then scores column pairs."""

    def __init__(self, table_paths: list[Path]):
        profiles = [p for path in table_paths for p in table_profiles(path).values()]
        dims = len(FEATURES)
        self.mu = [statistics.mean(p[k] for p in profiles) for k in range(dims)]
        self.sd = [statistics.pstdev(p[k] for p in profiles) or 1.0 for k in range(dims)]

    def _z(self, p: list[float]) -> list[float]:
        return [(x - m) / s for x, m, s in zip(p, self.mu, self.sd)]

    def label_profiles(self, path: Path) -> dict[str, list[float]]:
        """'table.column' label -> standardized profile, for one scenario table."""
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        header, body = rows[0], rows[1:]
        by_values = table_profiles(path)
        out = {}
        for c, name in enumerate(header):
            column = tuple(r[c] if c < len(r) else "" for r in body)
            if column in by_values:
                out[f"{path.stem}.{name}"] = self._z(by_values[column])
        return out

    @staticmethod
    def similarity(a: list[float] | None, b: list[float] | None) -> float:
        if a is None or b is None:
            return 0.0
        d2 = sum((x - y) ** 2 for x, y in zip(a, b))
        return math.exp(-d2 / (2 * len(a)))


def main() -> None:
    for arg in sys.argv[1:]:
        path = Path(arg)
        with path.open(encoding="utf-8", newline="") as fh:
            header = next(csv.reader(fh))
        profiles = table_profiles(path)
        with path.open(encoding="utf-8", newline="") as fh:
            body = list(csv.reader(fh))[1:]
        print(f"{'column':<32}" + "".join(f"{f:>10}" for f in FEATURES))
        for c, name in enumerate(header):
            column = tuple(r[c] if c < len(r) else "" for r in body)
            if column in profiles:
                print(f"{name[:31]:<32}" + "".join(f"{x:>10.2f}" for x in profiles[column]))


if __name__ == "__main__":
    main()
