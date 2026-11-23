#!/usr/bin/env python3
"""Build Schematch scenarios for the WDC Product tables and run Schematch on them.

Step 5 of docs/wdc-product-matching-plan.md. Needs the outputs of
tools/wdc_prepare.py (clean + groundtruth).

    python tools/wdc_scenarios.py build                  # 200 sampled shop pairs, readable names
    python tools/wdc_scenarios.py hide                   # opaque names + permuted columns + keys
    python tools/wdc_scenarios.py keys                   # (re)write keys of an existing opaque corpus
    python tools/wdc_scenarios.py config fast-opaque --corpus opaque
    python tools/wdc_scenarios.py run fast-opaque        # needs a compiled Schematch

All 4,950 shop pairs for step 9 (tools/wdc_schema.py):

    python tools/wdc_scenarios.py build --pairs 0 --name all-readable
    python tools/wdc_scenarios.py hide --name all-readable
    python tools/wdc_scenarios.py config all-fast --corpus all-opaque --no-eval
    python tools/wdc_scenarios.py run all-fast --jvm-arg=-Xmx8g

Everything is written below datasets-local/wdc/ (git-ignored). The active
configuration in src/main/resources is not touched: `run` puts the generated
configuration directory first on the classpath.

Keys: `hide` also writes scenarios/keys/<opaque corpus>.csv, the exact map from
each hidden column (scenario, side, hidden table, hidden column, position) to its
readable table, column and position. It lies outside the opaque corpus, so
Schematch never sees it; tools/wdc_combine.py and tools/wdc_schema.py use it to
print readable names and to score per attribute.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dataset_support import transforms  # noqa: E402

csv.field_size_limit(sys.maxsize)

ROOT = Path(__file__).resolve().parent.parent
WDC = ROOT / "datasets-local" / "wdc"
SCENARIOS = WDC / "scenarios"
KEYS = SCENARIOS / "keys"
KEY_FIELDS = ["scenario", "side", "hidden_table", "hidden_column", "position",
              "table", "column", "readable_position"]
CONFIGS = WDC / "config"
DATASET = "wdc"
DEFAULT_SEED = 20261117
RELATED = [{"price", "lowprice", "highprice"}]

# Fast, Java-only, value-based matchers: no Python server, no model.
FAST_VALUE_MATCHERS = [
    ("PMFMatcher", "similarity.instance", {"distance": ["L2"]}),
    ("KolmogorovSmirnovMatcher", "similarity.instance", None),
    ("JensenShannonValueMatcher", "similarity.instance", None),
    ("ValueShapeRoleMatcher", "similarity.instance", None),
    ("CharDistributionMatcher", "similarity.instance", None),
    ("PatternProfileMatcher", "similarity.instance", None),
]
# Label baseline; only meaningful on the readable corpus.
LABEL_MATCHERS = [
    ("LevenshteinMatcher", "similarity.label", None),
]
MATCHER_SETS = {
    "fast": FAST_VALUE_MATCHERS,
    "fast+label": FAST_VALUE_MATCHERS + LABEL_MATCHERS,
}


def safe_name(table: str) -> str:
    """10x10.co.kr -> 10x10_co_kr (no dots, never the '___' pair separator)."""
    return re.sub(r"_+", "_", re.sub(r"[^0-9A-Za-z]", "_", table)).strip("_")


def load_tables() -> dict[str, dict]:
    attrs = defaultdict(dict)
    with (WDC / "ground_truth" / "column_attributes.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            attrs[r["table"]][int(r["column_index"])] = r["shared_attribute"]
    tables = {}
    for path in sorted((WDC / "clean").glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        header, body = rows[0], rows[1:]
        # Drop empty columns and columns excluded from scoring (tools/wdc_gt_overrides.csv).
        keep = [c for c in range(len(header))
                if any(c < len(r) and r[c] for r in body) and attrs[path.stem][c] != "_exclude"]
        if len(set(header[c] for c in keep)) != len(keep):
            sys.exit(f"duplicate column names in {path.name}")
        tables[path.stem] = {
            "name": safe_name(path.stem),
            "header": [header[c] for c in keep],
            "rows": [[r[c] if c < len(r) else "" for c in keep] for r in body],
            "attributes": [attrs[path.stem][c] for c in keep],
            "dropped_empty": len(header) - len(keep),  # empty or excluded
        }
    return tables


def write_csv(path: Path, header, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if header is not None:
            w.writerow(header)
        w.writerows(rows)


def related(a: str, b: str) -> bool:
    return a != b and any(a in g and b in g for g in RELATED)


def cmd_build(args) -> None:
    out = SCENARIOS / args.name
    if out.exists():
        sys.exit(f"{out} exists; remove it to rebuild")
    tables = load_tables()
    ids = sorted(tables)
    all_pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]
    rng = random.Random(args.seed)
    pairs = all_pairs if args.pairs == 0 else sorted(rng.sample(all_pairs, args.pairs))

    summary, related_rows = [], []
    for a, b in pairs:
        ta, tb = tables[a], tables[b]
        scen = out / DATASET / f"{ta['name']}__{tb['name']}"
        write_csv(scen / "source" / f"{ta['name']}.csv", ta["header"], ta["rows"])
        write_csv(scen / "target" / f"{tb['name']}.csv", tb["header"], tb["rows"])
        matrix = [[int(x == y) for y in tb["attributes"]] for x in ta["attributes"]]
        write_csv(scen / "ground_truth" / f"{ta['name']}___{tb['name']}.csv", None, matrix)
        (scen / "metadata").mkdir(parents=True, exist_ok=True)
        (scen / "metadata" / "profile-status.properties").write_text(
            "".join(f"{side}.{p}=not_profiled\n" for side in ("source", "target")
                    for p in ("fd", "ucc", "ind", "dc")), encoding="utf-8")
        n_true = sum(map(sum, matrix))
        for i, x in enumerate(ta["attributes"]):
            for j, y in enumerate(tb["attributes"]):
                if related(x, y):
                    related_rows.append([scen.name, ta["header"][i], tb["header"][j]])
        summary.append([scen.name, a, b, len(ta["header"]), len(tb["header"]),
                        len(ta["header"]) * len(tb["header"]), n_true])
    write_csv(out / "scenarios.csv", ["scenario", "source_table", "target_table", "source_columns",
                                       "target_columns", "column_pairs", "true_pairs"], summary)
    write_csv(out / "related_pairs.csv", ["scenario", "source_column", "target_column"], related_rows)
    (out / "build.json").write_text(json.dumps({
        "seed": args.seed, "pairs": len(pairs), "of_all_pairs": len(all_pairs),
        "empty_columns_dropped": sum(t["dropped_empty"] for t in tables.values()),
        "list_cell_policy": "first element (tools/wdc_prepare.py)",
    }, indent=2), encoding="utf-8")
    print(f"{len(pairs)} scenarios written to {out / DATASET}")
    print(f"  column pairs {sum(r[5] for r in summary)}, true pairs {sum(r[6] for r in summary)}, "
          f"related 1:n pairs {len(related_rows)}")
    print(f"  scenarios without any true pair: {sum(1 for r in summary if r[6] == 0)}")


def hidden_name(readable: str) -> str:
    """readable -> opaque, all-readable -> all-opaque"""
    return readable.replace("readable", "opaque") if "readable" in readable else f"{readable}-opaque"


def cmd_hide(args) -> None:
    src = SCENARIOS / args.name
    tmp = SCENARIOS / f"_{args.name}_hidden_tmp"
    dst = SCENARIOS / hidden_name(args.name)
    if dst.exists():
        sys.exit(f"{dst} exists; remove it to rebuild")
    shutil.rmtree(tmp, ignore_errors=True)
    tool = [sys.executable, str(ROOT / "tools" / "transform_dataset.py")]
    subprocess.run(tool + ["opaque-tables", "--input", str(src), "--output", str(tmp),
                           "--variant", "on", "--seed", str(args.seed)], check=True)
    subprocess.run(tool + ["permute", "--input", str(tmp), "--output", str(dst)], check=True)
    shutil.rmtree(tmp)
    print(f"opaque corpus written to {dst}")
    write_keys(args.name, args.seed)


def key_path(opaque: str) -> Path:
    return KEYS / f"{opaque}.csv"


def build_keys(readable: str, seed) -> list[list]:
    """Recompute the hidden names with the salts `hide` used; positions come from the
    opaque headers. Every hidden column must decode to exactly one readable column
    with identical values, otherwise this fails."""
    src_root, dst_root = SCENARIOS / readable / DATASET, SCENARIOS / hidden_name(readable) / DATASET
    scen_names = sorted(d.name for d in src_root.iterdir() if d.is_dir())
    if scen_names != sorted(d.name for d in dst_root.iterdir() if d.is_dir()):
        sys.exit(f"{src_root} and {dst_root} hold different scenarios")
    rows = []
    for name in scen_names:
        for side in transforms.SIDES:
            _, tmap, cmap, cols = transforms.side_maps(src_root / name, DATASET, side, str(seed))
            (table, columns), = cols.items()
            decode = {cmap[c]: c for c in columns}
            hidden = dst_root / name / side / f"{tmap[table]}.csv"
            if not hidden.is_file():
                sys.exit(f"{hidden} missing: wrong --seed?")
            h_header, h_cols = read_columns(hidden)
            r_header, r_cols = read_columns(src_root / name / side / f"{table}.csv")
            if sorted(h_header) != sorted(decode):
                sys.exit(f"{hidden}: hidden names do not decode with this seed")
            for pos, hc in enumerate(h_header):
                column = decode[hc]
                rpos = r_header.index(column)
                if h_cols[pos] != r_cols[rpos]:
                    sys.exit(f"{hidden}: values of {column} differ")
                rows.append([name, side, tmap[table], hc, pos, table, column, rpos])
    return rows


def read_columns(path: Path):
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    return header, [tuple(r[c] if c < len(r) else "" for r in body) for c in range(len(header))]


def write_keys(readable: str, seed) -> None:
    rows = build_keys(readable, seed)
    path = key_path(hidden_name(readable))
    write_csv(path, KEY_FIELDS, rows)
    print(f"{len(rows)} keys written to {path}")


def cmd_keys(args) -> None:
    write_keys(args.name, args.seed)


def yaml_docs(entries) -> str:
    parts = []
    for name, package, params in entries:
        doc = f'---\nname: "{name}"\npackageName: "{package}"\n'
        if params:
            doc += "params:\n" + "".join(f"  {k}: {json.dumps(v)}\n" for k, v in params.items())
        parts.append(doc)
    return "".join(parts)


def cmd_config(args) -> None:
    corpus = SCENARIOS / args.corpus / DATASET
    if not corpus.is_dir():
        sys.exit(f"missing corpus {corpus}")
    out = CONFIGS / args.name
    out.mkdir(parents=True, exist_ok=True)
    res = ROOT / "src" / "main" / "resources"
    general = (res / "general.yaml").read_text(encoding="utf-8")
    overrides = {
        "defaultDatasetBasePath": '"data"', "readDependencies": "False",
        "saveOutputFirstLineMatchers": "True",
        "runSecondLineMatchers": "False",
        "runSimMatrixBoostingOnFirstLineMatchers": "False",
        "runSimMatrixBoostingOnSecondLineMatchers": "False",
        "readCacheFirstLineMatchers": "False",
        # The Java Precision/Recall metrics accept only 0/1 matrices. Raw scores
        # are evaluated by tools/evaluate_paper.py (docs/evaluation.md).
        "evaluateFirstLineMatchers": "False", "evaluateSecondLineMatchers": "False",
        "evaluateSimMatrixBoostingOnFirstLineMatchers": "False",
        "evaluateSimMatrixBoostingOnSecondLineMatchers": "False",
        "paperEvaluation": "False" if args.no_eval else "True",
        "paperEvaluationPython": json.dumps(args.python),
        "paperEvaluationScript": '"tools/evaluate_paper.py"',
    }
    for key, value in overrides.items():
        general, n = re.subn(rf"(?m)^{key}\s*:.*$", f"{key}: {value}", general)
        if n == 0:
            general += f"\n{key}: {value}\n"
    (out / "general.yaml").write_text(general, encoding="utf-8")
    rel = os.path.relpath(corpus, ROOT / "data")
    (out / "datasets.yaml").write_text(f'---\nname: "{DATASET}-{args.corpus}"\npath: "{rel}"\n', encoding="utf-8")
    (out / "first_line_matchers.yaml").write_text(yaml_docs(MATCHER_SETS[args.matchers]), encoding="utf-8")
    (out / "second_line_matchers.yaml").write_text("# none\n", encoding="utf-8")
    for name in ("metrics.yaml", "first_line_tokenizers.yaml"):
        if (res / name).is_file():
            shutil.copy(res / name, out / name)
    print(f"configuration written to {out} ({args.matchers} matchers on {args.corpus} corpus)")


def cmd_run(args) -> None:
    conf = CONFIGS / args.name
    cp_file = ROOT / "target" / "cp.txt"
    if not conf.is_dir() or not cp_file.is_file():
        sys.exit("create the configuration first and build Schematch (target/cp.txt)")
    classpath = os.pathsep.join([str(conf), str(ROOT / "target" / "classes"),
                                 cp_file.read_text(encoding="utf-8-sig").strip()])
    java = shutil.which("java", path=os.path.join(os.environ.get("JAVA_HOME", ""), "bin")) or "java"
    cmd = [java, *args.jvm_arg, "-cp", classpath, "de.uni_marburg.schematch.Main"]
    sys.exit(subprocess.call(cmd, cwd=ROOT))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="write readable scenarios for sampled shop pairs")
    b.add_argument("--name", default="readable", help="corpus name below datasets-local/wdc/scenarios")
    b.add_argument("--pairs", type=int, default=200, help="number of shop pairs; 0 = all 4,950")
    b.add_argument("--seed", type=int, default=DEFAULT_SEED)
    h = sub.add_parser("hide", help="opaque table/column names and permuted column order")
    h.add_argument("--name", default="readable", help="readable corpus to hide")
    h.add_argument("--seed", type=int, default=DEFAULT_SEED)
    k = sub.add_parser("keys", help="write the hidden -> readable key file of an existing opaque corpus")
    k.add_argument("--name", default="readable", help="readable corpus the opaque one was made from")
    k.add_argument("--seed", type=int, default=DEFAULT_SEED)
    c = sub.add_parser("config", help="write a Schematch configuration directory")
    c.add_argument("name")
    c.add_argument("--corpus", default="opaque", help="corpus name, e.g. opaque or all-opaque")
    c.add_argument("--matchers", choices=sorted(MATCHER_SETS), default="fast")
    c.add_argument("--no-eval", action="store_true",
                   help="skip the raw-score evaluation (for the all-pairs corpus used by step 9)")
    c.add_argument("--python", default=".venv/bin/python",
                   help="interpreter with NumPy/SciPy for the raw-score evaluation")
    r = sub.add_parser("run", help="run Schematch with a generated configuration")
    r.add_argument("name")
    r.add_argument("--jvm-arg", action="append", default=[])
    args = p.parse_args()
    {"build": cmd_build, "hide": cmd_hide, "keys": cmd_keys, "config": cmd_config, "run": cmd_run}[args.command](args)


if __name__ == "__main__":
    main()
