#!/usr/bin/env python3
"""Expand the raw WDC Schema.org Product tables (JSON Lines, gzip) into flat CSV files.

Step 0 of docs/wdc-product-matching-report.md: produces vorbereitete_tabellen/
from the WDC Schema.org Table Corpus 2023, class Product, top 100 web sites
(`Product_<site>_October2023.json.gz`, one JSON object per line).

    python tools/wdc_expand.py expand                # raw corpus -> vorbereitete_tabellen/
    python tools/wdc_expand.py check                 # verify vorbereitete_tabellen/ is reproduced
    python tools/wdc_expand.py expand --rows 0 --output datasets-local/wdc/expanded_all

The raw corpus is expected in datasets-local/wdc/raw/Product_top100/ (git-ignored,
about 790 MB); download it from https://webdatacommons.org/structureddata/schemaorgtables/2023/.

Flattening rules (they reproduce the committed vorbereitete_tabellen/ byte for byte):

- `row_id` and `page_url` are dropped.
- Nested objects become dotted paths: {"offers": {"price": 1}} -> `offers.price`.
- A list of scalars keeps its path with a `[]` suffix: {"sku": ["a", "b"]} -> `sku[]`.
- A list of objects is transposed: one column per inner key, `offers[].price`, holding
  the values of all elements in order; nested lists inside the elements are
  concatenated and missing or null values are skipped.
- A list value is written as a JSON array (`["a", "b"]`); a list with exactly one
  value is written as that value, an empty list as an empty cell.
- null becomes an empty cell; booleans `true`/`false`; numbers as Python prints them.
- The columns are the sorted union of the paths found in the *whole* file (option
  `--columns file`), the rows are the first `--rows` records (`--sample first`). Columns that are
  empty in those rows are therefore kept; tools/wdc_prepare.py drops them.
- UTF-8 with BOM, LF line endings, minimal quoting.

The first records of a file are not a fair sample: in 34 of the 100 sites they repeat
page URLs (the first 100 kozaleathers.com rows come from only 3 pages).
`--sample random --seed N` draws a uniform sample instead; write it to another
directory, since vorbereitete_tabellen/ and all results in the report use the first rows.

Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import random
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "datasets-local" / "wdc" / "raw" / "Product_top100"
DEFAULT_OUTPUT = ROOT / "vorbereitete_tabellen"
DROPPED = ("row_id", "page_url")
SUFFIX = ".json.gz"


@dataclass
class Expanded:
    name: str
    data: bytes
    rows: int
    columns: int
    warnings: list[str] = field(default_factory=list)


def flatten(value, path: str, out: dict, issues: Counter) -> None:
    """Add the leaf values below `value` to `out` (path -> scalar or list); nulls are skipped."""
    if isinstance(value, dict):
        for key, inner in value.items():
            if "." in key or key.endswith("[]"):
                issues[f"key {key!r} contains '.' or '[]'; its column name is ambiguous"] += 1
            flatten(inner, f"{path}.{key}" if path else key, out, issues)
    elif isinstance(value, list):
        objects = [e for e in value if isinstance(e, dict)]
        if objects and len(objects) == len(value):
            transposed: dict[str, list] = {}
            for element in objects:
                leaves: dict = {}
                flatten(element, "", leaves, issues)
                for key, leaf in leaves.items():
                    transposed.setdefault(key, []).extend(leaf if isinstance(leaf, list) else [leaf])
            for key, leaves in transposed.items():
                base, is_list = (key[:-2], "[]") if key.endswith("[]") else (key, "")
                put(out, f"{path}[].{base}{is_list}", leaves, issues)
        else:
            if objects:
                issues[f"{path}[]: list mixes objects and scalars; objects written as JSON"] += 1
            put(out, f"{path}[]", value, issues)
    elif value is not None:
        put(out, path, value, issues)


def put(out: dict, path: str, value, issues: Counter) -> None:
    if path in out:
        issues[f"{path}: two values map to the same column; the last one is kept"] += 1
    out[path] = value


def cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        if len(value) == 1:
            return cell(value[0])
        return json.dumps(value, ensure_ascii=False) if value else ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def record_paths(record: dict, issues: Counter) -> dict:
    out: dict = {}
    flatten({k: v for k, v in record.items() if k not in DROPPED}, "", out, issues)
    return out


def expand_file(path: Path, rows: int = 100, columns: str = "file",
                sample: str = "first", seed: int = 0) -> Expanded:
    """Expand one `<table>.json.gz`; rows <= 0 keeps all records.

    sample="first" keeps the first `rows` records; sample="random" draws `rows`
    records uniformly (reservoir sampling with `seed`) and keeps their file order.
    """
    name = path.name[: -len(SUFFIX)]
    issues: Counter = Counter()
    all_paths: set[str] = set()
    kept: list[tuple[int, dict]] = []
    rng = random.Random(f"{seed}:{name}")
    seen = 0
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                issues[f"line {number}: invalid JSON ({exc.msg}); skipped"] += 1
                continue
            if not isinstance(record, dict):
                issues[f"line {number}: not a JSON object; skipped"] += 1
                continue
            if rows <= 0 or len(kept) < rows:
                slot = len(kept)
            elif sample == "random":
                slot = rng.randrange(seen + 1)
                slot = slot if slot < rows else None
            else:
                slot = None
            seen += 1
            if slot is None and columns == "rows" and sample == "first":
                break
            if slot is None and columns == "rows":
                continue
            paths = record_paths(record, issues)
            if columns == "file":
                all_paths.update(paths)
            if slot is not None:
                if slot == len(kept):
                    kept.append((number, paths))
                else:
                    kept[slot] = (number, paths)
    kept.sort(key=lambda item: item[0])
    kept_paths = [paths for _, paths in kept]
    if columns == "rows":
        all_paths = {p for paths in kept_paths for p in paths}
    if 0 < rows and len(kept) < rows:
        issues[f"only {len(kept)} records, fewer than --rows {rows}"] += 1
    header = sorted(all_paths)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for paths in kept_paths:
        writer.writerow([cell(paths.get(h)) for h in header])
    data = ("\ufeff" + buffer.getvalue()).encode("utf-8")
    warnings = [f"{msg} (x{n})" if n > 1 else msg for msg, n in sorted(issues.items())]
    return Expanded(name, data, len(kept), len(header), warnings)


def _expand(args) -> Expanded:
    return expand_file(*args)


def expand_all(input_dir: Path, rows: int, columns: str, jobs: int,
               sample: str = "first", seed: int = 0):
    files = sorted(input_dir.glob(f"*{SUFFIX}"))
    if not files:
        sys.exit(f"no *{SUFFIX} files in {input_dir}")
    work = [(f, rows, columns, sample, seed) for f in files]
    if jobs == 1:
        yield from map(_expand, work)
    else:
        with ProcessPoolExecutor(max_workers=jobs or None) as pool:
            yield from pool.map(_expand, work)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["expand", "check"],
                        help="expand: write CSV files; check: compare with existing CSV files")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="directory of *.json.gz")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="CSV directory")
    parser.add_argument("--rows", type=int, default=100, help="records per table, 0 = all (default 100)")
    parser.add_argument("--columns", choices=["file", "rows"], default="file",
                        help="column set from the whole file (default, as vorbereitete_tabellen/) "
                             "or only from the kept rows (no all-empty columns, faster)")
    parser.add_argument("--sample", choices=["first", "random"], default="first",
                        help="first records (default, as vorbereitete_tabellen/) or a uniform "
                             "random sample; the first records often come from very few pages")
    parser.add_argument("--seed", type=int, default=0, help="seed for --sample random")
    parser.add_argument("--jobs", type=int, default=0, help="parallel processes, 0 = all cores")
    args = parser.parse_args(argv)

    if args.command == "expand":
        args.output.mkdir(parents=True, exist_ok=True)
    differing = missing = 0
    for result in expand_all(args.input, args.rows, args.columns, args.jobs, args.sample, args.seed):
        target = args.output / f"{result.name}.csv"
        status = ""
        if args.command == "expand":
            target.write_bytes(result.data)
        elif not target.exists():
            missing += 1
            status = "  MISSING"
        elif target.read_bytes() != result.data:
            differing += 1
            status = "  DIFFERS"
        print(f"{result.name}: {result.rows} rows, {result.columns} columns{status}")
        for warning in result.warnings:
            print(f"  warning: {warning}")
    if args.command == "check":
        if differing or missing:
            print(f"check failed: {differing} differing, {missing} missing in {args.output}")
            return 1
        print(f"check passed: every table in {args.output} is reproduced exactly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
