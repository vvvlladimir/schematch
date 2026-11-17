# Report: Schema matching of 100 web-shop product tables

This report summarizes the matching of the 100 product tables in
`vorbereitete_tabellen/` with Schematch, with a focus on search-space reduction
("Suchraumreduzierung") from the Data Engineering course. The step-by-step plan,
commands and intermediate decisions are in the
[matching plan](wdc-product-matching-plan.md).

## 1. Summary

- **Task:** find which columns of 100 shops mean the same thing, and build one
  shared Product schema. Column names were hidden for every measured result.
- **Search-space reduction works and pays twice.** Two-pass type blocking keeps
  98.6% of the true column pairs and removes 71.5% of all comparisons. Applied
  before the final decision, it also *raises* quality (F1 0.506 → 0.571),
  because it removes pairs that matchers wrongly scored high.
- **Simple matchers suffice; a language model is not needed.** The best result
  comes from six fast Schematch value matchers plus a small "role profile"
  matcher of nine numbers per column (F1_H 0.582, MRR 0.847 on 200 shop pairs).
  The role profile alone is the strongest single matcher.
- **Formats are solved, meaning is not.** Currency (100% recall), availability
  (95%) and dates (96%) are found almost always. Brand (29%) and product IDs
  (sku 26%, mpn 0%) are the hard cases.
- **Shared schema:** 4,950 pairwise decisions were turned into 65 consistent
  attributes at about the same quality (F1 0.583 vs 0.594 pairwise). The
  holistic step did not fix brand: by values alone, brands look like categories
  or colours.
- **The ground truth was corrected.** A plausibility audit flagged 37 columns;
  18 had a header that did not describe their values (site names, artists,
  product titles under `brand`) and were relabelled or excluded. With the
  corrected answers the filter keeps 99.4% of the true pairs and the pairwise
  F1 over all shop pairs rises from 0.594 to 0.608. Unless noted otherwise, the
  numbers in sections 4–7 are those of the header-based version 1 (section 6).

## 2. Data

The tables come from the WDC Schema.org Table Corpus 2023, class Product: one
table per web shop, 100 products each, values in many languages and currencies.
Column headers are flattened schema.org property paths (`offers.price`,
`brand.name`).

| | Raw | After cleaning |
|---|---|---|
| Tables | 100 | 100 |
| Columns | 1,448 | 1,243 |
| Empty columns | 234 | 119 |
| Non-empty columns | 1,214 | 1,124 |
| Header names / shared attributes | 236 | 155 (58 in only one shop) |
| Cross-table column pairs | 1,031,828 (all columns) | 624,076 (non-empty) |
| True pairs | – | 29,268 |

### Cleaning

| Rule | Cells / columns |
|---|---|
| JSON list cells (`["31.70", "31.16"]`) keep their first element | 3,341 cells |
| `undefined`, `null`, `n/a`, … become empty | 233 cells |
| Numbers normalized (`2.4E1` → 24, `$116.45` → 116.45) in mostly numeric columns | 1,489 cells |
| Columns split by JSON shape (`offers.price` / `offers[].price`) joined when they never overlap | 205 columns |

Cleaning was decisive: on the raw data, type blocking kept only 56% of the true
pairs; on cleaned data, 94% before and 98.6% after the rule fixes of section 4.

### Ground truth

Because the headers are schema.org paths, the correct answers are derived from
them, with four rules agreed on 2026-11-17:

1. `brand` = `brand.name`.
2. `sku` = `offers.sku` = `url.sku`; `productid` and `mpn` stay separate.
3. `offers.X` = `X` for offer and product properties (price, currency,
   availability, condition, category, gtin, mpn, …).
4. `price`, `lowprice`, `highprice` are separate; their pairs (3,469) are
   reported apart as related 1:n pairs.

Since the ground truth comes from the names, a matcher that reads names finds
the answer trivially (Levenshtein on names: F1_H 0.754, MRR 0.992). All reported
results therefore use **hidden names**: table and column names replaced by
salted hashes and columns permuted (`transform_dataset.py opaque-tables` and
`permute`).

## 3. Pipeline

```
clean → ground truth → hide names → candidate selection → Schematch matchers
      → combiner → selector → shared schema (clustering) → evaluation
```

This follows the course architecture *Schemaelemente → Vorfilter/Top-k → teure
Matcher → Combiner → Selector → Mapping* and the duplicate-detection phases
(preparation, candidate generation, matching, decision, clustering).

| Step | Tool |
|---|---|
| Cleaning, ground truth | `tools/wdc_prepare.py` |
| Ground-truth audit and manual corrections | `tools/wdc_gt_audit.py`, `tools/wdc_gt_overrides.csv` |
| Candidate selection, PC/RR | `tools/wdc_candidates.py` |
| Schematch scenarios, hiding names, runs | `tools/wdc_scenarios.py` |
| Role profile matcher | `tools/wdc_roles.py` |
| Combiner, filter, selector, error analysis | `tools/wdc_combine.py` |
| Shared schema | `tools/wdc_schema.py` |

## 4. Search-space reduction

Search space: all 624,076 cross-table pairs of non-empty columns. Metrics from
the course: **Pair Completeness** PC = true pairs among candidates / all true
pairs, and **Reduction Ratio** RR = 1 − candidates / all pairs. Every method
looks only at values, never at names.

| Course method | Candidates | PC | RR | Verdict |
|---|---|---|---|---|
| Blocking on column type, one pass | 153,704 | 92.8% | 75.4% | good start |
| **Multi-pass blocking (2 passes)** | **177,736** | **98.6%** | **71.5%** | **chosen** |
| Multi-pass blocking (3 passes) | 206,139 | 98.8% | 67.0% | +0.2 PC for 28,000 pairs |
| 3 passes + top-3 per table | 158,356 | 95.5% | 74.6% | trades PC for RR 1:1 |
| 3 passes + top-1 per table | 74,783 | 79.7% | 88.0% | loses too much |
| Size filter (distinct-count ratio ≥ 0.1) | 151,719 | 87.9% | 75.7% | loses too much |
| Inverted index on value tokens | 118,066 | 61.9% | 81.1% | unsuitable |
| MinHash + LSH (16 bands × 4 rows) | 5,351 | 12.3% | 99.1% | unsuitable |

**Blocking key.** Each column gets a type from the majority of its values:
number, date, currency (ISO 4217 code), URL, schema.org enumeration, code, text.
The second pass merges number and code, because the same attribute (sku) is
numeric in one shop and alphanumeric in another.

**Loss analysis** raised PC from 94.2% to 98.6% with four rule fixes:
enumeration values without the `schema.org/` prefix (`in_stock`), more date
formats (`2023/10/12`, `Oct. 21, 2023`), longer codes (`artworkid[…]`), and
currency only for real ISO codes (the brand "ESR" had been typed as currency).
The remaining 420 lost pairs are data errors (an sku holding the shop's home
page URL) or free text (`New`, `Новый`).

**Why token-based methods fail here.** Inverted index and MinHash/LSH look for
shared tokens. Different shops sell different products in different languages,
so their columns share almost no values, even when the columns mean the same.
This is the course's warning about approximate filters, observed on real data.

**Methods not applied.** Prefix filtering needs a Jaccard threshold on token
sets, which, as shown, carries no signal here. Sorted neighbourhood and suffix
arrays address record keys with typos, not column pairs.

## 5. Matching, combining, selecting

Experiments use 200 randomly sampled shop pairs (seed 20261117): 24,973 column
pairs, 1,150 true pairs, at least one true pair per scenario. The candidate
filter keeps 98.2% of the true pairs and removes 71.8% of the cells, matching
the full-data numbers. Each Schematch run over the 200 pairs takes about 5
seconds.

Metrics (computed by `tools/evaluate_paper.py`):

- **F1_H:** F1 of a 1:1 Hungarian assignment.
- **F1_M:** F1 of mutual top-1 (both columns are each other's best).
- **MRR:** mean reciprocal rank of the correct partner per source column.

| Method | F1_H | F1_M | MRR |
|---|---|---|---|
| ValueShapeRoleMatcher | 0.136 | 0.129 | 0.441 |
| PMFMatcher | 0.152 | 0.213 | 0.498 |
| JensenShannonValueMatcher | 0.259 | 0.260 | 0.448 |
| PatternProfileMatcher | 0.365 | 0.352 | 0.619 |
| KolmogorovSmirnovMatcher | 0.379 | 0.414 | 0.681 |
| CharDistributionMatcher (best Schematch matcher) | 0.435 | 0.467 | 0.753 |
| CharDistributionMatcher + filter | 0.498 | 0.504 | 0.789 |
| RoleProfile + filter | 0.502 | 0.515 | 0.816 |
| Mean of 6 Schematch matchers | 0.506 | 0.566 | 0.812 |
| Mean of 6 Schematch matchers + filter | 0.571 | **0.592** | 0.841 |
| **Mean of 6 Schematch matchers + RoleProfile + filter** | **0.582** | 0.582 | **0.847** |

**Combiner.** The mean of min-max normalized matrices. The six matchers err
differently, so the mean beats every single one; even the weak ones add
independent evidence (mean of all six > mean of the best three: 0.571 vs 0.527).

**Candidate filter after matching.** Setting non-candidate cells to 0 raises
F1_H from 0.506 to 0.571. Search-space reduction here is not only a cost
measure; it also removes impossible pairs such as price ↔ description.

**Role profile matcher.** The course matchers are labels, types/constraints,
structure and instances; no language model. With names hidden, each column is
described by the role it plays in its own table: share of distinct values,
words, length, whether its value occurs inside another column of the same row
(a brand inside the product name), whether it contains another column (a
description containing the name), and number features (share of numbers, share
in [0, 5] for ratings, integers, magnitude). Medians over all shops:

| | distinct | words | inside another column | contains another column |
|---|---|---|---|---|
| brand | 0.50 | 1.6 | 45% | 0% |
| category | 0.48 | 1.8 | 9% | 0% |
| name | 1.00 | 6.3 | 6% | 4% |
| description | 1.00 | 36.2 | 0% | 36% |

It is language-independent, needs no download, and is the best single matcher.
In the combination it adds about one point, because the Schematch matchers
already capture length and repetition indirectly.

**Selector.** For the combination without RoleProfile, mutual top-1 and partial
Hungarian (penalty 0.3) reach F1 0.592 (precision 0.61, recall 0.60); plain
Hungarian reaches 0.571 (precision 0.52, recall 0.66), because it assigns
partners also to columns that have none.

## 6. Error analysis

Per attribute (6 matchers + filter, mutual top-1, 200 shop pairs):

| Attribute | True pairs | Recall | Precision |
|---|---|---|---|
| pricecurrency | 144 | 100% | 100% |
| pricevaliduntil | 23 | 96% | 73% |
| availability | 93 | 95% | 98% |
| name | 193 | 67% | 70% |
| price | 148 | 55% | 72% |
| itemcondition | 33 | 55% | 69% |
| description | 159 | 54% | 87% |
| brand | 139 | 29% | 64% |
| sku | 117 | 26% | 57% |
| category | 10 | 20% | 11% |
| mpn | 9 | 0% | 0% |

Rank of the correct partner (6 matchers + RoleProfile + filter):

| Attribute | Rank 1 | Top 3 |
|---|---|---|
| name | 77% | 99% |
| description | 76% | 97% |
| price | 72% | 89% |
| brand | 61% | 88% |
| sku | 48% | 86% |
| all others | 87% | 96% |

Findings:

1. **Fixed formats are solved** (currency, dates, enumerations).
2. **Brand** is ranked first in 61% of the cases but kept by mutual top-1 in
   only about half of them: the brand column of the other shop often prefers a
   different column. The ranking is reasonable; the decision is hard.
3. **Product IDs** (sku, mpn, productid) are codes used inconsistently across
   shops. Neither format nor role separates them; arguably neither could a
   person without the names.
4. **Numbers** are sometimes confused (rating ↔ price, review count ↔ sku).
5. Some counted errors are ties between two identical columns of the same table
   (for example `name` and `url.name`); precision is slightly understated.
6. **Some "errors" are errors of the ground truth**, see below.

### Errors found in the ground truth

The ground truth trusts the header each shop declared. Shops misuse
schema.org markup, so a header does not always describe its values. Example: if
shop A has `brand` = Apple, Sony, shop B has only `name` with the values Apple,
Sony, and shop C has `name` with people's names, the header-based ground truth
says B.`name` = C.`name` and A.`brand` ≠ B.`name`. Both statements are wrong.

**How the suspects were found.** `tools/wdc_gt_audit.py` compares every
non-empty column with what its attribute should look like: a brand is short and
repeats, a product name is unique and has several words, a price is a number, a
currency is an ISO code, a rating is at most 100, and the value type matches
the type most shops use for the attribute. It flagged 37 of 1,124 columns
(`datasets-local/wdc/ground_truth/audit.csv`). Each one was inspected by hand.
The check only flags; a person decides.

**Decisions (recorded in `tools/wdc_gt_overrides.csv`, applied by
`wdc_prepare.py groundtruth`):**

| Shop | Column | Values | Decision |
|---|---|---|---|
| tower.com | `brand` | "Byrd, Charlie", "Getz, Stan" (musicians) | new attribute `creator` |
| pixels.com | `brand.name` | "Lars Lentz - Website" (artists) | `creator` |
| threadless.com | `brand.name` | "HIDENbehindAroc's Shop" (artists' shops) | `creator` |
| creativemarket.com | `brand.name` | "Valerie Bodnar" (designers) | `creator` |
| udg.mx | `brand.name` | identical to `name` in all 100 rows | relabel to `name` |
| revolve.com | `description` | "dress", "top", "jean" | relabel to `category` |
| kozaleathers.com | `name` | "M / Turquoise Blue" (size/colour) | new attribute `variant` |
| pp.ua | `brand.name`, `mpn`, `sku` | the same long product titles in all three | exclude |
| elliman.com | `brand.name` | "MLS Limousine" (listing service) | exclude |
| eltiempo.com | `name`, `description`, `sku`, `productid` | one constant value for the whole news site | exclude |
| semana.com | `name`, `sku`, `productid` | one constant value for the whole news site | exclude |

In total 18 columns: 7 relabelled, 11 excluded from every evaluation.

**Kept as they are:**

- *Borderline brands:* made-in-china.com (factory names) and skipthedishes.com
  ("KFC (230 Queen St. E)") count as brand.
- *Header correct, flagged by mistake:* iso.org (ISO is the brand of its
  standards), edmunds.com (car makes), mercadolibre.com.ar,
  shoplightspeed.com, unity.com, slq.qld.gov.au (real brands), docomo.ne.jp
  (Japanese titles), dan.com (the products are domain names), thredup.com and
  broadbandnow.com (simple names that repeat).
- *Right attribute, unusual format* (a cleaning issue, not a ground-truth
  error): ratings such as "4,7 рейтинг основан на…", prices with text
  (omega.com, spb.ru), dates such as "Sat Aug 13 15:03:15 PDT 2022",
  relative URLs, condition written as "New".

**Effect on the results (version 1 = header-based, version 2 = corrected):**

| | Version 1 | Version 2 |
|---|---|---|
| Evaluated columns | 1,124 | 1,113 |
| True pairs | 29,268 | 28,118 |
| Candidate filter PC (two passes) | 98.6% | **99.4%** |
| 200 pairs: Mean(6) + RoleProfile + filter, F1_H / F1_M / MRR | 0.582 / 0.582 / 0.847 | 0.575 / 0.589 / **0.857** |
| 200 pairs: Mean(6) + filter, F1_H / F1_M / MRR | 0.571 / 0.592 / 0.841 | 0.570 / 0.597 / 0.851 |
| All 4,950 pairs, pairwise mutual top-1, F1 | 0.594 | **0.608** |
| All pairs, brand recall (pairwise) | 31% | **36%** |
| Shared schema (one-per-shop merge), F1 | 0.583 | 0.581 |
| Shared schema, brand recall | 25% | 29% |

- Part of the "lost" candidate pairs were ground-truth errors: with the
  corrected answers the filter loses only 0.6% of the true pairs.
- On all shop pairs, F1 rises from 0.594 to 0.608 and brand recall from 31% to
  36%. Some of the brand errors were indeed errors of the markup.
- On the 200-pair sample the overall F1 barely moves, and the brand recall of
  the best combination even drops (29% → 26%) because the sample contains few
  of the corrected shops. The differences on the sample are within its noise.
- The main conclusion stands: brand remains the hard case. Correcting the
  ground truth explains only a small part of it.

## 7. Shared schema

All 4,950 shop pairs were scored (Schematch: about 30 seconds) and combined into
a 1,124 × 1,124 column similarity matrix. Pair-wise P/R/F1 over all 29,268 true
pairs:

| Method | Clusters | P | R | F1 |
|---|---|---|---|---|
| Pairwise decisions only (mutual top-1) | – | 0.607 | 0.582 | 0.594 |
| **One-per-shop merge along mutual top-1 edges** | **65** | **0.631** | 0.542 | **0.583** |
| + join whole clusters (average ≥ 0.4) | 51 | 0.626 | 0.543 | 0.581 |
| Average linkage, cut at 0.5 | 133 | 0.448 | 0.542 | 0.491 |

**One-per-shop merge** joins the strongest mutual pairs first and refuses any
merge that would put two columns of the same shop into one cluster. It has no
tuned parameter. Average linkage without this rule chains unrelated attributes.

Largest clusters (purity = share of the majority attribute):

| Shops | Purity | Attribute | Mixed with |
|---|---|---|---|
| 90 | 87% | name | brand, url.name, offers.name |
| 84 | 100% | pricecurrency | – |
| 81 | 80% | description | name, url.description, review body |
| 69 | 100% | availability | – |
| 60 | 88% | price | highprice, rating |
| 58 | 59% | brand | category, name, manufacturer |
| 58 | 57% | sku | highprice, mpn, lowprice |
| 28 | 93% | itemcondition | – |
| 25 | 96% | ratingvalue | – |

The holistic step gives a readable, consistent schema at the same quality as
the pairwise decisions, but it did **not** recover brand (pairwise recall 31%,
clusters 25%). Brand columns end up in several clusters whose average
similarity is low, so they are not joined. The limiting factor is the evidence
in the values, not the selection method.

## 8. Lessons in terms of the course

| Course statement | Observation here |
|---|---|
| A true match lost in candidate generation cannot be recovered. | PC was the first thing to measure; 1.4% of true pairs are lost for good. |
| Measure PC and RR, not only run time. | Run time was never the problem (seconds); PC decided between methods. |
| Prefer a union of several small rules over one coarse rule. | Two-pass blocking: +5.8 PC over one pass. |
| Data preparation comes first. | Cleaning moved PC from 56% to 94%. |
| Approximate filters (LSH) can lose matches. | MinHash/LSH kept 12% of the true pairs. |
| Schema matching only via labels is a trap. | Labels give the answer away here, so they were hidden. |
| Use types, structure and instances. | Type blocking, role profile (structure within the table) and value matchers carry the result. |
| 1:1 selection can be wrong for 1:n cases. | price/lowprice/highprice were reported apart; two sku columns in one shop are n:1. |
| Keep several candidates for uncertain cases and let an expert confirm. | Top-3 contains 86–99% of the true partners, versus 48–77% at rank 1. |

## 9. Limitations

- The ground truth is derived from schema.org headers with four agreed rules;
  other rules (for example merging all IDs) give other numbers.
- Sections 4–7 report the header-based ground truth (version 1); section 6
  compares it with the corrected version 2. The audit rules flag only what
  they check for; errors of other kinds may remain.
- List cells keep only their first element.
- Matcher experiments use one random sample of 200 shop pairs; the candidate
  filter numbers agree with the full data (98.2% vs 98.6% PC).
- Method comparisons in step 9 report several settings; only the chosen method
  has no tuned parameter.
- Only fast Java value matchers and the role profile were used. Neural matchers
  (SBERT, LLMs) were deliberately left out.

## 10. Recommendations

1. For integration in practice, present the **top-3 candidates** for brand,
   sku, mpn and productid and let a person confirm; accept mutual top-1
   automatically for currency, availability, dates and condition.
2. Use **external knowledge** for brands (a brand dictionary) if they must be
   matched automatically; values alone are not enough.
3. Separate **numbers by range** (ratings in [0, 5] or [0, 100], counts as
   integers) to reduce rating ↔ price confusions.
4. If names may be used in production, a label matcher combined with the value
   matchers will dominate; the hidden-name results show what remains when
   names are missing or meaningless.

## 11. Reproduction

From the repository root, with JDK 21 (`export JAVA_HOME=$(/usr/libexec/java_home -v 21)`),
a built Schematch (`mvn test compile dependency:build-classpath -Dmdep.outputFile=target/cp.txt`)
and `.venv` with `requirements.txt` (Python 3.13):

```shell
python tools/wdc_prepare.py clean
python tools/wdc_prepare.py groundtruth                  # version 2: with tools/wdc_gt_overrides.csv
# python tools/wdc_prepare.py groundtruth --no-overrides # version 1: headers only
python tools/wdc_gt_audit.py                             # flags suspicious columns for review
python tools/wdc_candidates.py evaluate --analyze type2 --write type2

python tools/wdc_scenarios.py build
python tools/wdc_scenarios.py hide
python tools/wdc_scenarios.py config fast-opaque --corpus opaque --matchers fast
python tools/wdc_scenarios.py run fast-opaque --jvm-arg=-Xmx6g
.venv/bin/python tools/wdc_combine.py results/<run> --corpus opaque
.venv/bin/python tools/wdc_combine.py results/<run> --analyze "Mean(all value matchers) + candidate filter"

python tools/wdc_scenarios.py build --pairs 0 --name all-readable
python tools/wdc_scenarios.py hide --name all-readable
python tools/wdc_scenarios.py config all-fast --corpus all-opaque --no-eval
python tools/wdc_scenarios.py run all-fast --jvm-arg=-Xmx8g
.venv/bin/python tools/wdc_schema.py results/<run> --corpus all-opaque --readable all-readable
```

For version 2 the corrected runs used the corpus names `v2-readable` and
`v2-all-readable` (`--name`, and `--corpus v2-opaque --readable v2-readable` for
`wdc_combine.py`). All outputs are written to `datasets-local/wdc/` and
`results/` (both git-ignored).
