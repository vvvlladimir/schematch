#!/usr/bin/env python3
"""Build Schematch scenarios for all pairs of WDC Product tables and run Schematch.

Step 2 of docs/wdc-product-matching-report.md. Needs tools/wdc_prepare.py clean.

    python tools/wdc_scenarios.py build             # one scenario per shop pair (4,950)
    python tools/wdc_scenarios.py config            # Schematch configuration, evaluation off
    python tools/wdc_scenarios.py run --jvm-arg=-Xmx8g

There is no ground truth: Schematch runs in inference mode and only writes the
score matrices of the first-line matchers (`_outputs/MatchingStepLine1`).
Everything is written below datasets-local/wdc/ (git-ignored). The active
configuration in src/main/resources is not touched: `run` puts the generated
configuration directory first on the classpath.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from itertools import combinations
from pathlib import Path

csv.field_size_limit(sys.maxsize)

ROOT = Path(__file__).resolve().parent.parent
WDC = ROOT / "datasets-local" / "wdc"
CORPUS = WDC / "pairs"
CONFIG = WDC / "config"
DATASET = "wdc"

# Fast Java matchers: one label matcher and six instance (value) matchers.
MATCHERS = [
    ("LevenshteinMatcher", "similarity.label", None),
    ("PMFMatcher", "similarity.instance", {"distance": ["L2"]}),
    ("KolmogorovSmirnovMatcher", "similarity.instance", None),
    ("JensenShannonValueMatcher", "similarity.instance", None),
    ("ValueShapeRoleMatcher", "similarity.instance", None),
    ("CharDistributionMatcher", "similarity.instance", None),
    ("PatternProfileMatcher", "similarity.instance", None),
]


def safe_name(table: str) -> str:
    """10x10.co.kr -> 10x10_co_kr (no dots, never the '___' pair separator)."""
    return re.sub(r"_+", "_", re.sub(r"[^0-9A-Za-z]", "_", table)).strip("_")


def load_tables() -> dict[str, dict]:
    """Cleaned tables without their empty columns (nothing to match there)."""
    tables = {}
    for path in sorted((WDC / "clean").glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        header, body = rows[0], rows[1:]
        keep = [c for c in range(len(header)) if any(c < len(r) and r[c] for r in body)]
        if len(set(header[c] for c in keep)) != len(keep):
            sys.exit(f"duplicate column names in {path.name}")
        tables[path.stem] = {
            "name": safe_name(path.stem),
            "header": [header[c] for c in keep],
            "rows": [[r[c] if c < len(r) else "" for c in keep] for r in body],
        }
    return tables


def write_csv(path: Path, header, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def cmd_build(args) -> None:
    if CORPUS.exists():
        sys.exit(f"{CORPUS} exists; remove it to rebuild")
    tables = load_tables()
    summary = []
    for a, b in combinations(sorted(tables), 2):
        ta, tb = tables[a], tables[b]
        scen = CORPUS / DATASET / f"{ta['name']}__{tb['name']}"
        write_csv(scen / "source" / f"{ta['name']}.csv", ta["header"], ta["rows"])
        write_csv(scen / "target" / f"{tb['name']}.csv", tb["header"], tb["rows"])
        (scen / "metadata").mkdir(parents=True, exist_ok=True)
        (scen / "metadata" / "profile-status.properties").write_text(
            "".join(f"{side}.{p}=not_profiled\n" for side in ("source", "target")
                    for p in ("fd", "ucc", "ind", "dc")), encoding="utf-8")
        summary.append([scen.name, a, b, len(ta["header"]), len(tb["header"])])
    write_csv(CORPUS / "scenarios.csv",
              ["scenario", "source_table", "target_table", "source_columns", "target_columns"], summary)
    print(f"{len(summary)} scenarios written to {CORPUS / DATASET}")
    print(f"  column pairs {sum(r[3] * r[4] for r in summary)}")


def yaml_docs(entries) -> str:
    parts = []
    for name, package, params in entries:
        doc = f'---\nname: "{name}"\npackageName: "{package}"\n'
        if params:
            doc += "params:\n" + "".join(f"  {k}: {json.dumps(v)}\n" for k, v in params.items())
        parts.append(doc)
    return "".join(parts)


def cmd_config(args) -> None:
    corpus = CORPUS / DATASET
    if not corpus.is_dir():
        sys.exit(f"missing corpus {corpus}; run build first")
    CONFIG.mkdir(parents=True, exist_ok=True)
    res = ROOT / "src" / "main" / "resources"
    general = (res / "general.yaml").read_text(encoding="utf-8")
    overrides = {
        "defaultDatasetBasePath": '"data"', "readDependencies": "False",
        "saveOutputFirstLineMatchers": "True",
        "runSecondLineMatchers": "False",
        "runSimMatrixBoostingOnFirstLineMatchers": "False",
        "runSimMatrixBoostingOnSecondLineMatchers": "False",
        "readCacheFirstLineMatchers": "False",
        # No ground truth: inference only.
        "evaluateFirstLineMatchers": "False", "evaluateSecondLineMatchers": "False",
        "evaluateSimMatrixBoostingOnFirstLineMatchers": "False",
        "evaluateSimMatrixBoostingOnSecondLineMatchers": "False",
        "paperEvaluation": "False",
    }
    for key, value in overrides.items():
        general, n = re.subn(rf"(?m)^{key}\s*:.*$", f"{key}: {value}", general)
        if n == 0:
            general += f"\n{key}: {value}\n"
    (CONFIG / "general.yaml").write_text(general, encoding="utf-8")
    rel = os.path.relpath(corpus, ROOT / "data")
    (CONFIG / "datasets.yaml").write_text(f'---\nname: "{DATASET}"\npath: "{rel}"\n', encoding="utf-8")
    (CONFIG / "first_line_matchers.yaml").write_text(yaml_docs(MATCHERS), encoding="utf-8")
    (CONFIG / "second_line_matchers.yaml").write_text("# none\n", encoding="utf-8")
    for name in ("metrics.yaml", "first_line_tokenizers.yaml"):
        if (res / name).is_file():
            shutil.copy(res / name, CONFIG / name)
    print(f"configuration written to {CONFIG}")


def cmd_run(args) -> None:
    cp_file = ROOT / "target" / "cp.txt"
    if not CONFIG.is_dir() or not cp_file.is_file():
        sys.exit("run config first and build Schematch (target/cp.txt)")
    classpath = os.pathsep.join([str(CONFIG), str(ROOT / "target" / "classes"),
                                 cp_file.read_text(encoding="utf-8-sig").strip()])
    java = shutil.which("java", path=os.path.join(os.environ.get("JAVA_HOME", ""), "bin")) or "java"
    cmd = [java, *args.jvm_arg, "-cp", classpath, "de.uni_marburg.schematch.Main"]
    sys.exit(subprocess.call(cmd, cwd=ROOT))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="write one scenario per shop pair")
    sub.add_parser("config", help="write the Schematch configuration directory")
    r = sub.add_parser("run", help="run Schematch with the generated configuration")
    r.add_argument("--jvm-arg", action="append", default=[])
    args = p.parse_args()
    {"build": cmd_build, "config": cmd_config, "run": cmd_run}[args.command](args)


if __name__ == "__main__":
    main()
