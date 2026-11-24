#!/usr/bin/env python3
"""Ground truth for the 100 WDC Product tables: one concept label per column.

Two columns of different tables correspond when they carry the same concept.
Labels follow the *meaning of the values*, not the schema.org header; the header
only gives the first draft. Several columns of one table may share a concept when
they hold the same information (url.name = name), so the reference allows 1:n.

    python tools/wdc_gt.py import-xlsx SchemaMatching_GroundTruth_KI_vorbewertet.xlsx
    python tools/wdc_gt.py build           # -> ground_truth/wdc_product/columns.csv, concepts.csv
    .venv/bin/python tools/wdc_gt.py audit # value checks -> datasets-local/wdc/ground_truth/audit.csv

Layers, applied in this order (files in ground_truth/wdc_product/):

1. draft_ai.csv      AI pre-labels per raw column (sheet Attributgruppen_1448 of the
                     xlsx; derived from header paths only, never verified).
2. concept_rules.csv group-level decisions: AI group -> concept (synonyms).
3. identifiers       code: a column of an identifier concept (sku, mpn, gtin*, isbn,
                     productid) whose values are valid GTIN/EAN/UPC/ISBN check-digit
                     codes becomes `gtin`, whatever its header says; an mpn or
                     productid equal to the table's sku column becomes `sku`.
4. overrides.csv     column-level decisions after looking at the values; concept
                     `_exclude` removes a column from the reference.

complex_rules.csv lists n:m correspondences between concepts (price <-> lowprice +
highprice); `build` expands them to table pairs in complex.csv. audit_accepted.csv
records audit flags that were looked at and are fine, so `audit` lists only open ones.

Columns are those of tools/wdc_prepare.py clean (split columns such as offers.price /
offers[].price joined). Columns without any value are kept in columns.csv with
evaluated=0: no matcher can find them.

Standard library only, except `audit` (uses tools/wdc_candidates.py, needs numpy).
"""
from __future__ import annotations

import argparse
import csv
import itertools
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wdc_prepare as prep  # noqa: E402

csv.field_size_limit(sys.maxsize)

ROOT = Path(__file__).resolve().parent.parent
GT = ROOT / "ground_truth" / "wdc_product"
TABLES = ROOT / "vorbereitete_tabellen"
AUDIT = ROOT / "datasets-local" / "wdc" / "ground_truth" / "audit.csv"
EXCLUDE = "_exclude"
ID_CONCEPTS = {"sku", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "isbn", "productid"}
GTIN_SHARE = 0.8
COPY_SHARE = 0.9  # an mpn/productid equal to the table's sku in this share of rows is that sku
# Concepts whose value should change from product to product; one value in every
# row means the column is not what its label says (site name, home page URL, ...).
VARYING = {"name", "description", "sku", "mpn", "gtin", "productid", "url", "image",
           "price", "brand"}  # share of values that must be valid GTIN/ISBN codes


# ------------------------------------------------------------------ helpers --

def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [r for r in csv.DictReader(fh) if not next(iter(r.values()), "").startswith("#")]


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def gtin_valid(code: str) -> bool:
    """GTIN-8/12/13/14 (EAN, UPC, ISBN-13) by check digit, or ISBN-10."""
    if len(code) == 10 and re.fullmatch(r"\d{9}[\dX]", code):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(code))
        return total % 11 == 0
    if len(code) not in (8, 12, 13, 14) or not code.isdigit():
        return False
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def as_code(value: str) -> str:
    v = re.sub(r"^(isbn|ean|upc|gtin\d*)\s*:?\s*", "", value.strip(), flags=re.I)
    return re.sub(r"[\s\-]", "", v).upper()


def is_gtin(value: str) -> bool:
    """Valid code as written, or with leading zeros a spreadsheet dropped (UPC 11 digits)."""
    code = as_code(value)
    if gtin_valid(code):
        return True
    return code.isdigit() and 7 <= len(code) < 14 and any(
        gtin_valid(code.zfill(n)) for n in (8, 12, 13, 14) if n > len(code))


def gtin_share(values: list[str]) -> float:
    return sum(is_gtin(v) for v in values) / len(values) if values else 0.0


def equal_share(a: list[str], b: list[str]) -> tuple[float, int]:
    """Share of rows where both cells are filled and equal; and the number of such rows."""
    both = [(x, y) for x, y in zip(a, b) if x and y]
    return (sum(x == y for x, y in both) / len(both) if both else 0.0), len(both)


# ------------------------------------------------------------------ columns --

def load_tables(tables_dir: Path = TABLES):
    """Yield (table id, {clean column: [raw columns]}, {clean column: cells}) per table,
    cleaned exactly as tools/wdc_prepare.py clean does; cells keep empty strings so
    the columns of one table stay aligned by row."""
    for path in sorted(tables_dir.glob("*.csv")):
        header, body = prep.read_table(path)
        cleaned = prep.clean_table(header, body, Counter())
        new_header, new_body = prep.merge_split_columns(header, cleaned, Counter())
        kept = set(new_header)
        by_norm = defaultdict(list)
        for h in new_header:
            by_norm[prep.normalize_header(h)].append(h)
        raw_of = defaultdict(list)
        for h in header:
            target = h if h in kept else by_norm[prep.normalize_header(h)][0]
            raw_of[target].append(h)
        cells = {h: [r[i] for r in new_body] for i, h in enumerate(new_header)}
        yield prep.table_id(path), dict(raw_of), cells


# -------------------------------------------------------------------- build --

def build(gt_dir: Path = GT, tables_dir: Path = TABLES) -> tuple[list[dict], list[str]]:
    draft = {(r["table"], r["column"]): r["ai_group"] for r in read_csv(gt_dir / "draft_ai.csv")}
    rules = {r["ai_group"]: r for r in read_csv(gt_dir / "concept_rules.csv")}
    overrides = {(r["table"], r["column"]): r for r in read_csv(gt_dir / "overrides.csv")}
    used_overrides: set = set()
    problems: list[str] = []
    rows = []
    for table, raw_of, cells in load_tables(tables_dir):
        labels = {}
        for column, raws in raw_of.items():
            groups = sorted({draft.get((table, r), "") for r in raws})
            if "" in groups:
                problems.append(f"{table}.{column}: no AI label for {raws}")
            ai = draft.get((table, column)) or next((g for g in groups if g), "")
            if len(groups) > 1:
                problems.append(f"{table}.{column}: merged raw columns have AI labels {groups}")
            concept, source, reason = ai, "ai", ""
            if ai in rules:
                concept, source, reason = rules[ai]["concept"], "rule", rules[ai]["reason"]
            vals = [v for v in cells[column] if v]
            if concept in ID_CONCEPTS and vals:
                share = gtin_share(vals)
                if share >= GTIN_SHARE:
                    if concept != "gtin":
                        concept, source = "gtin", "identifier"
                        reason = f"{share:.0%} of values are valid GTIN/ISBN codes"
                elif concept == "gtin":
                    problems.append(f"{table}.{column}: labelled gtin but only {share:.0%} valid codes")
            labels[column] = [ai, concept, source, reason]
        # mpn / productid that only repeat the shop's own article number are that sku
        skus = [c for c, lab in labels.items() if lab[1] == "sku"]
        for column, lab in labels.items():
            if lab[1] in ("mpn", "productid"):
                for sku in skus:
                    share, n = equal_share(cells[column], cells[sku])
                    if n >= 5 and share >= COPY_SHARE:
                        lab[1:] = ["sku", "identifier", f"{share:.0%} equal to {sku} in {n} rows"]
                        break
        for column, (ai, concept, source, reason) in labels.items():
            if (table, column) in overrides:
                o = overrides[(table, column)]
                used_overrides.add((table, column))
                concept, source, reason = o["concept"], "override", o["reason"]
            non_empty = sum(1 for v in cells[column] if v)
            rows.append({"table": table, "column": column, "raw_columns": " | ".join(raw_of[column]),
                         "ai_group": ai, "concept": concept, "source": source,
                         "non_empty": non_empty,
                         "evaluated": int(bool(non_empty) and concept != EXCLUDE), "reason": reason})
    for key in overrides.keys() - used_overrides:
        problems.append(f"override for unknown column {key[0]}.{key[1]}")
    for ai in rules.keys() - set(draft.values()):
        problems.append(f"concept rule for unknown AI group {ai}")
    return rows, problems


def complex_correspondences(rows: list[dict], gt_dir: Path = GT) -> list[dict]:
    """Expand complex_rules.csv to table pairs.

    A rule `price <-> lowprice+highprice` means: when table A has the left concepts but
    none of the right ones, and table B has all right concepts, then A's left columns
    correspond to B's right columns together with B's own left columns, if any (in
    these tables a row fills either price or the range, never both).
    """
    path = gt_dir / "complex_rules.csv"
    if not path.exists():
        return []
    columns = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if int(r["evaluated"]):
            columns[r["table"]][r["concept"]].append(r["column"])
    out = []
    for rule in read_csv(path):
        left, right = rule["left"].split("+"), rule["right"].split("+")
        for a in sorted(columns):
            if not all(c in columns[a] for c in left) or any(c in columns[a] for c in right):
                continue
            for b in sorted(columns):
                if b == a or not all(c in columns[b] for c in right):
                    continue
                cols_a = [c for k in left for c in columns[a][k]]
                cols_b = [c for k in left + right for c in columns[b].get(k, [])]
                out.append({"table_a": a, "columns_a": " + ".join(cols_a),
                            "table_b": b, "columns_b": " + ".join(cols_b),
                            "kind": f"{len(cols_a)}:{len(cols_b)}",
                            "rule": f"{rule['left']} <-> {rule['right']}"})
    return out


def positive_pairs(rows: list[dict]) -> set[tuple[tuple[str, str], tuple[str, str]]]:
    by_concept = defaultdict(list)
    for r in rows:
        if int(r["evaluated"]):
            by_concept[r["concept"]].append((r["table"], r["column"]))
    pairs = set()
    for members in by_concept.values():
        for a, b in itertools.combinations(sorted(members), 2):
            if a[0] != b[0]:
                pairs.add((a, b))
    return pairs


def cmd_build(args) -> int:
    rows, problems = build(args.gt, args.tables)
    write_csv(args.gt / "columns.csv", rows,
              ["table", "column", "raw_columns", "ai_group", "concept", "source",
               "non_empty", "evaluated", "reason"])
    evaluated = [r for r in rows if r["evaluated"]]
    concepts = defaultdict(list)
    for r in evaluated:
        concepts[r["concept"]].append(r)
    summary = [{"concept": c, "tables": len({m["table"] for m in ms}), "columns": len(ms),
                "column_names": " | ".join(f"{n} ({k})" for n, k in
                                           Counter(m["column"] for m in ms).most_common())}
               for c, ms in sorted(concepts.items(), key=lambda x: (-len({m["table"] for m in x[1]}), x[0]))]
    write_csv(args.gt / "concepts.csv", summary, ["concept", "tables", "columns", "column_names"])
    complex_rows = complex_correspondences(rows, args.gt)
    write_csv(args.gt / "complex.csv", complex_rows,
              ["table_a", "columns_a", "table_b", "columns_b", "kind", "rule"])
    sources = Counter(r["source"] for r in rows)
    print(f"{len(rows)} columns, {len(evaluated)} evaluated "
          f"({sum(1 for r in rows if not r['non_empty'])} empty, "
          f"{sum(1 for r in rows if r['concept'] == EXCLUDE)} excluded)")
    print(f"{len(concepts)} concepts, {sum(1 for c in summary if c['tables'] >= 2)} in two or more tables")
    print(f"{len(positive_pairs(rows))} corresponding column pairs across tables")
    print(f"{len(complex_rows)} complex (1:n) correspondences between table pairs")
    print("labels by source:", dict(sources))
    for p in problems:
        print("problem:", p)
    print(f"written to {args.gt}")
    return 1 if problems else 0


# --------------------------------------------------------------- import-xlsx --

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def xlsx_sheet(path: Path, name: str) -> list[list[str]]:
    """Rows of one worksheet, read with the standard library."""
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    for sheet in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", NS):
        if sheet.get("name") != name:
            continue
        target = rels[sheet.get(f"{{{NS['r']}}}id")].lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        rows = []
        for row in ET.fromstring(z.read(target)).iter(f"{{{NS['m']}}}row"):
            cells = {}
            for c in row.findall("m:c", NS):
                col = 0
                for ch in re.match(r"[A-Z]+", c.get("r")).group():
                    col = col * 26 + ord(ch) - 64
                v = c.find("m:v", NS)
                if c.get("t") == "s" and v is not None:
                    cells[col - 1] = shared[int(v.text)]
                elif c.get("t") == "inlineStr":
                    cells[col - 1] = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
                else:
                    cells[col - 1] = v.text if v is not None else ""
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
        return rows
    raise SystemExit(f"sheet {name!r} not found in {path}")


def cmd_import_xlsx(args) -> int:
    rows = xlsx_sheet(args.xlsx, "Attributgruppen_1448")
    header = rows[0]
    out = []
    for r in rows[1:]:
        d = dict(zip(header, r))
        out.append({"table": prep.table_id(Path(d["Datei"].removesuffix(".gz").removesuffix(".json") + ".csv")),
                    "column": d["Original_Attributpfad"], "ai_group": d["KI_Konzeptgruppe"],
                    "ai_rule": d["Gruppierungsregel"], "ai_risk": d["Pruefrisiko"]})
    write_csv(args.gt / "draft_ai.csv", out, ["table", "column", "ai_group", "ai_rule", "ai_risk"])
    print(f"{len(out)} AI labels from {args.xlsx.name} -> {args.gt / 'draft_ai.csv'}")
    return 0


# -------------------------------------------------------------------- audit --

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
PERSON = re.compile(r"^[A-Z][\w'\-]+, [A-Z]")   # "Byrd, Charlie"
VARIANT = re.compile(r"^\S{1,4} / \S")          # "M / Turquoise Blue"


def _words(v: str) -> float:
    """Word count; CJK text has no spaces, so about 4 characters count as a word."""
    return max(len(v.split()), len(CJK.findall(v)) / 4)


def concept_checks(concept: str, values: list[str]) -> list[str]:
    """Expectations for single concepts; returns why a column looks wrong."""
    n = len(values)
    distinct = len(set(values)) / n
    words = sum(map(_words, values)) / n
    length = sum(map(len, values)) / n
    nums = []
    for v in values:
        try:
            nums.append(float(v))
        except ValueError:
            pass
    numeric = len(nums) / n
    out = []
    if concept == "name":
        if sum(bool(VARIANT.match(v)) for v in values) / n > 0.5:
            out.append("values look like size/colour variants")
        if distinct < 0.6 and n >= 10:
            out.append(f"many repeated values (distinct {distinct:.2f})")
        if words <= 1.5:
            out.append(f"very short (avg {words:.1f} words)")
    elif concept == "brand":
        if words > 3 or length > 30:
            out.append(f"long values (avg {words:.1f} words, {length:.0f} chars)")
        if sum(bool(PERSON.match(v)) for v in values) / n > 0.3:
            out.append("values look like person names ('Last, First')")
        if distinct > 0.95 and n >= 20:
            out.append(f"almost no repeated values (distinct {distinct:.2f})")
    elif concept == "description":
        if words < 3:
            out.append(f"very short for a description (avg {words:.1f} words)")
    elif concept in ("price", "lowprice", "highprice", "aggregaterating.reviewcount",
                     "offercount", "aggregaterating.bestrating", "aggregaterating.worstrating"):
        if numeric < 0.8:
            out.append(f"only {numeric:.0%} numbers")
    elif concept == "aggregaterating.ratingvalue":
        if numeric >= 0.8 and nums and max(nums) > 100:
            out.append(f"values up to {max(nums):g}, too large for a rating")
    return out

def cmd_audit(args) -> int:
    import wdc_candidates as wc

    labels = {(r["table"], r["column"]): r for r in read_csv(args.gt / "columns.csv")}
    cols, tables = [], []
    for table, _, cells in load_tables(args.tables):
        tables.append((table, cells))
        for column, col_cells in cells.items():
            vals = [v for v in col_cells if v]
            lab = labels[(table, column)]
            if vals and lab["concept"] != EXCLUDE:
                cols.append({"table": table, "column": column, "concept": lab["concept"],
                             "values": vals, "type": wc.column_type(vals)})
    flags = []

    def flag(kind, c, detail, other=""):
        top = " | ".join(v[:40] for v, _ in Counter(c["values"]).most_common(3))
        flags.append({"kind": kind, "table": c["table"], "column": c["column"],
                      "concept": c["concept"], "detail": detail, "other": other, "samples": top})

    by_concept = defaultdict(list)
    for c in cols:
        by_concept[c["concept"]].append(c)
    for concept, cs in by_concept.items():
        if len({c["table"] for c in cs}) < 5:
            continue
        major = Counter(wc.PASS2.get(c["type"], c["type"]) for c in cs).most_common(1)[0][0]
        for c in cs:
            if wc.PASS2.get(c["type"], c["type"]) != major:
                flag("type", c, f"type {c['type']}, most columns of the concept: {major}")
    for c in cols:
        for why in concept_checks(c["concept"], c["values"]):
            flag("value", c, why)
    for c in cols:
        if c["concept"] in VARYING and len(c["values"]) >= 20 and len(set(c["values"])) == 1:
            flag("constant", c, f"one value in all {len(c['values'])} rows")
    # same table, (almost) the same values, different concepts
    index = {(c["table"], c["column"]): c for c in cols}
    for t, cells in tables:
        header = [h for h in cells if (t, h) in index]
        for a, b in itertools.combinations(header, 2):
            ca, cb = index[(t, a)], index[(t, b)]
            if ca["concept"] == cb["concept"]:
                continue
            both = [(x, y) for x, y in zip(cells[a], cells[b]) if x and y]
            if len(both) >= 5:
                eq = sum(x == y for x, y in both) / len(both)
                if eq >= 0.9:
                    flag("duplicate", ca, f"{eq:.0%} equal in {len(both)} rows",
                         f"{b} [{cb['concept']}]")
    accepted_path = args.gt / "audit_accepted.csv"
    accepted = {(r["kind"], r["table"], r["column"]): r["reason"]
                for r in (read_csv(accepted_path) if accepted_path.exists() else [])}
    seen = {(f["kind"], f["table"], f["column"]) for f in flags}
    for key in accepted.keys() - seen:
        print(f"note: accepted flag no longer raised: {key}")
    flags = [f for f in flags if (f["kind"], f["table"], f["column"]) not in accepted]
    flags.sort(key=lambda f: (f["kind"], f["concept"], f["table"]))
    write_csv(args.output, flags, ["kind", "table", "column", "concept", "detail", "other", "samples"])
    print(f"{len(cols)} columns checked, {len(accepted)} flags accepted in {accepted_path.name}, "
          f"{len(flags)} open -> {args.output}")
    for f in flags:
        print(f"[{f['kind']}] {f['table']}.{f['column']} [{f['concept']}]: {f['detail']}\n     {f['samples']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--gt", type=Path, default=GT, help="ground-truth directory")
    p.add_argument("--tables", type=Path, default=TABLES, help="vorbereitete_tabellen directory")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("import-xlsx", help="import AI pre-labels from the xlsx")
    s.add_argument("xlsx", type=Path)
    sub.add_parser("build", help="write columns.csv and concepts.csv")
    s = sub.add_parser("audit", help="flag columns whose values contradict their concept")
    s.add_argument("--output", type=Path, default=AUDIT)
    args = p.parse_args(argv)
    return {"import-xlsx": cmd_import_xlsx, "build": cmd_build, "audit": cmd_audit}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
