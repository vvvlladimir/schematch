# Plan: Matching the columns of 100 web-shop product tables

Results are summarized in the [report](wdc-product-matching-report.md).

This document describes how we match the columns of the 100 product tables in
`vorbereitete_tabellen/` with Schematch, and how search-space reduction
("Suchraumreduzierung") from the Data Engineering course is applied. It is written
in plain language: every step says what we do, why, and what the result is.

## 1. Goal

The folder `vorbereitete_tabellen/` contains 100 CSV tables, one per web shop
(`Product_<host>_October2023.csv`). They come from the WDC Schema.org Table
Corpus 2023, class Product. Each table has 100 products, but every shop stores
its data differently: one table has 3 columns, another 86, and the same kind of
information looks different from shop to shop.

What we want at the end:

1. For every pair of shops, a list saying "this column = that column".
2. One **shared Product schema** (name, price, currency, brand, rating, ...)
   that every column of every shop is linked to.
3. An honest measurement of how well this works, and how much work the
   candidate selection saved.

### Facts about the data

| Fact | Value |
|---|---|
| Tables | 100 |
| Rows per table | 100 |
| Columns per table | 3 to 86 |
| Columns in total | 1,448 (234 of them completely empty) |
| Distinct header names | 236 (123 occur in only one table) |
| Naive search space (all cross-table column pairs) | 1,031,828 |
| True column pairs (both columns non-empty) | 29,890 |

Headers are flattened schema.org property paths such as `offers.price`,
`brand.name` or `aggregaterating.ratingvalue`. Values are in many languages
(Korean, Russian, Polish, Swedish, ...) and currencies.

## 2. The big picture

The work is a pipeline. Each step takes the result of the previous one:

```
100 CSV files
  → 1. Clean the data
  → 2. Build the correct answers (ground truth)
  → 3. Hide the column names
  → 4. Select candidates (search-space reduction)
  → 5. Convert to the Schematch format
  → 6. Compare precisely (Schematch matchers)
  → 7. Combine the scores (combiner)
  → 8. Choose the final pairs (selector)
  → 9. Build the shared schema (clustering)
  → 10. Evaluate and report
```

The main idea from the course material: **first remove the obvious non-matches
cheaply, then compare the rest precisely and expensively.** Steps 6 to 8
follow the lecture architecture *matcher → combiner → selector*.

### Running example: the price column

We follow one example through all steps. The price column looks like this in
different shops:

| Shop | Value |
|---|---|
| 10x10.co.kr | `15900` |
| banggood.com | `["31.70", "31.16", "24.18"]` |
| fleetfeet.com | `2.4E1` (scientific notation for 24) |
| woot.com | `$116.45` |

A person sees immediately that these are all prices. A program does not,
unless the data is prepared first.

## 3. The steps in detail

### Step 0. Set up the environment

**What:** install Maven (`brew install maven`), build Schematch and run the
bundled example in `examples/data/demo/` (see [Getting started](getting-started.md)).

**Why:** make sure the project works on this machine before we put our data into it.

**Done when:** the example has run and a result appears under `results/`.

### Step 1. Clean the data

**What:** bring values into one consistent form without changing their meaning:

- remove the invisible byte-order mark (BOM) at the start of each file;
- unpack lists: `["31.70", "31.16"]` becomes the separate values `31.70` and `31.16`;
- read numbers in any notation: `2.4E1` = 24, `$116.45` = 116.45;
- treat `undefined`, `null` and empty strings as "no value";
- never modify the original files; write cleaned copies separately.

**Why:** in the prototype, candidate selection without cleaning kept only
**56%** of the true pairs. After cleaning it kept **94%**. This is the most
important and the cheapest step.

**Example:** after cleaning, all four price columns contain plain numbers.

### Step 2. Build the correct answers (ground truth)

**What:** the headers in this data are almost standard (schema.org markup), so
we can derive the correct answers from them:

- `offers.price`, `offers[].price` and `offers.price[]` mean the same thing: "price";
- two columns from different shops with the same normalized name are a true pair.

Some cases need a **manual decision**, which we make together and write down
as a rule:

- Are `brand` and `brand.name` the same?
- Are `sku`, `offers.sku` and `productid` the same?
- The 123 names that occur in only one table are checked by hand.

**Result:** a file `table, column, shared attribute` and about **29,890 true
column pairs** (counting only non-empty columns).

**Why:** without correct answers we cannot measure whether a method works.

### Step 3. Hide the column names

**What:** replace column names with meaningless codes (`offers.price` →
`c7f3a1…`) and shuffle the column order. Schematch already has
`tools/transform_dataset.py` with the `opaque-tables` and `permute` modes
(see [Datasets](datasets.md)).

**Why:** our correct answers were built from the names. If the names stay
visible, a method can simply "look them up" and score 100%, which proves
nothing. The course calls this mistake "schema matching only via labels".
Without names, a method has to understand a column **from its content**.

A second run **with names** is useful as a comparison: how much do the names help?

### Step 4. Select candidates (search-space reduction)

This is the central part, taken directly from the course material.

**Problem:** with 1,448 columns, comparing every column with every column of
the other shops gives **1,031,828 pairs**. Precise methods, especially neural
ones, would take a very long time.

#### 4.1 Sort columns into groups (blocking)

For each column we look at its values and decide its type:

| Group | How we recognize it | Example |
|---|---|---|
| Numbers | only digits, dots, commas (also `2.4E1`, `$5`) | price, rating |
| Dates | looks like `2024-10-04` | "price valid until" |
| Currency | three capital letters | `USD`, `EUR` |
| Links | starts with `http` | image, product page |
| Fixed values | contains `schema.org/` | availability, condition |
| Codes | letters and digits, no spaces | SKU, article number |
| Text | everything else | name, description |
| Empty | no values | not compared |

We only compare columns **within the same group**. A price is never compared
with a description.

#### 4.2 Second pass (multi-pass blocking)

Some attributes come in different types. One shop's SKU is `4925541` (a
number), another's is `A1-2345` (a code). So we run a second grouping in which
numbers and codes share a group, and take the **union** of the candidates from
both passes. This loses fewer true pairs.

#### 4.3 Keep only the best (top-k)

Inside a group, for each column we compute a cheap similarity from simple
features: average value length, share of digits, share of letters, number of
distinct values. From every other table we keep only the **3 most similar
columns**.

#### 4.4 Measure the quality of the selection

Two numbers from the course:

- **Pair Completeness (PC):** how many true pairs we **kept**. We want this close to 100%.
  `PC = true pairs among candidates / all true pairs`
- **Reduction Ratio (RR):** how much work we **saved**.
  `RR = 1 − number of candidates / number of all possible pairs`

Prototype results, with column names hidden:

| Variant | Candidates | PC (kept) | RR (saved) |
|---|---|---|---|
| Type groups, one pass | 192,453 | 91.0% | 81.3% |
| Type groups, two passes | 212,120 | 94.4% | 79.4% |
| Two passes + top-5 per table | 202,295 | 94.2% | 80.4% |
| Two passes + top-3 per table | 171,300 | 92.3% | 83.4% |
| Two passes + top-2 per table | 136,291 | 88.4% | 86.8% |
| Two passes + top-1 per table | 82,123 | 76.8% | 92.0% |

We choose the variant by deciding what matters more: not losing pairs, or saving time.

#### 4.5 Analyze the losses

We look at which true pairs were lost and why. Currently these are mainly
price, brand and currency columns with unusual formats, for example
`77,572 (税込) (税抜価格 ¥70,520)`. For these we add cleaning rules (back to
step 1) and measure again.

#### Methods we tested and rejected

These are also a result for the report:

| Method | PC | RR | Why it fails here |
|---|---|---|---|
| Inverted index on value tokens | 66.6% | 85.5% | different shops sell different products in different languages, so values share few words |
| MinHash + LSH on value tokens | 13.1% | 99.4% | same reason; only useful for fixed-value columns such as currency or availability |
| Size filtering (distinct-count ratio) | 83.3% | 85.7% | loses too many true pairs |
| Label 3-gram blocking | 100% | 87.2% | meaningless here, because the ground truth itself comes from the labels |

### Step 5. Convert to the Schematch format

**What:** Schematch always compares **source tables with target tables**
inside a *scenario*. For each pair of shops we create a folder:

```
scenario_A_vs_B/
  source/A.csv
  target/B.csv
  ground_truth/A___B.csv   ← 0/1 matrix: which column of A equals which column of B
```

**Which shop pairs:** there are 4,950 pairs, which is a lot. Options:

- **for experiments:** a sample of about 200 pairs (small and large tables,
  different languages);
- **for the final schema:** all pairs, but expensive methods only score the
  candidates from step 4.

**Result:** the scenario folders and a `datasets.yaml` file that tells
Schematch what to run.

**Note:** Schematch's own candidate step works on table pairs
(`TablePairsGenerator`, currently `NaiveTablePairsGenerator`, which takes all
pairs). Our column-level filter from step 4 has to be added by us: either as a
mask applied to the score matrices, or as a filter in front of the expensive matchers.

### Step 6. Compare precisely (matchers)

**What:** run Schematch methods on the candidate pairs. Each method returns a
score between 0 and 1: "how similar are these two columns". See
[Matchers](matchers.md).

**Fast methods** (Java, no extra setup):

- value distributions (`KolmogorovSmirnovMatcher`, `PMFMatcher`): prices
  between 10 and 500 look like prices, not like ratings between 1 and 5;
- value shape (`ValueShapeRoleMatcher`): which characters, what length;
- shared values (`JensenShannonValueMatcher`): good for currency and
  availability, where everyone uses `USD` or `InStock`.

**Slow methods** (Python server `seed_server.py`, models needed; see
[Python matchers](python-matchers.md)):

- **SBERT with a multilingual model:** understands the **meaning** of text in
  different languages. "Увлажнитель воздуха" and "humidifier" are similar to
  it. This is the key method for product names and descriptions.
- an LLM for the hardest cases, if needed.

**Example:** the prices `15900` (KRW) and `31.70` (USD) differ in size, but
both columns are numbers with a similar spread, next to a currency column.
Distribution and shape methods give them a high score.

**Where the savings happen:** fast methods could even score all pairs, because
the tables are small. The slow ones (SBERT, LLM) score **only the candidates**
from step 4. This is where the selection saves hours.

### Step 7. Combine the scores (combiner)

**What:** each column pair now has several scores from different methods. We
merge them into one, for example a (weighted) average. Schematch has ready-made
ensembles.

**Why:** every method has a weak spot. Distributions do not understand text;
SBERT is poor at telling numbers apart. Together they are more reliable.

### Step 8. Choose the final pairs (selector)

**What:** for each pair of shops we decide which column matches which:

- **main mode 1:1:** each column has at most one partner, chosen with the
  Hungarian method (`HungarianBonusMatcher`), as in the lecture;
- **threshold:** if the best score is too low, there is no pair. Not every
  column has a partner: a shop without ratings has no rating column.

**Careful with 1:n** (the course warns about this): one shop has a single price
`offers.price`, another has two, `lowprice` and `highprice`. A strict 1:1 rule
gets this wrong. Such cases are flagged and checked by hand.

### Step 9. Build the shared schema (clustering)

**What:** combine the pairwise results into groups:

- if price A = price B and price B = price C, all three go into one group "Price";
- each group becomes one attribute of the shared schema.

**Contradictions:** sometimes A = B and B = C, but A ≠ C. Then we look at the
strength of the links and cut the weak ones. This is the "clustering" phase of
the lecture's pipeline.

**Result:** the shared Product schema and a mapping table:

```
shared attribute | 10x10.co.kr          | banggood.com            | ...
Price            | offers.price         | offers[].price          |
Currency         | offers.pricecurrency | offers[].pricecurrency  |
Brand            | brand.name           | brand                   |
```

We write this step ourselves (a small Python script), because Schematch works
on pairs only.

### Step 10. Evaluate and report

We measure on two levels, as the course requires.

**Candidate selection (step 4):**

- PC: how many true pairs survived until the precise comparison;
- RR: how many comparisons we saved;
- run time with and without selection.

**Final result (steps 8 and 9):**

- **Precision:** of the pairs we found, how many are correct;
- **Recall:** of all true pairs, how many we found;
- **F1:** the combined score.

**Reported separately:**

- frequent attributes (name, price) versus rare ones (123 of them): rare
  attributes are where methods really differ;
- runs with and without column names: how much the names help;
- the most common errors and their causes.

## 4. Deliverables

1. A script that cleans the data and builds the ground truth.
2. A candidate-selection script with PC/RR measurement, using the course methods.
3. Scenarios in the Schematch format and the run configuration.
4. Matcher outputs and the final column pairs.
5. **The shared Product schema** covering all 100 shops.
6. A report: numbers, tables, error analysis, which course methods fit and
   which did not, and why.

## 5. Risks and how we handle them

| Risk | Mitigation |
|---|---|
| Messy formats (prices with text, mixed currencies) | add cleaning rules, measure again |
| Candidate selection loses true pairs | second pass, larger k, loss analysis |
| Column names give away the answer | hide the names (step 3) |
| 1:n cases (low/high price) | flag separately, check by hand |
| SBERT needs models and memory | start with fast methods, add SBERT later |
| Disputed correct answers (`brand` vs `brand.name`) | decide together and record the rule |

## 6. Order of work

1. **Steps 0–2:** the project runs, the data is clean, the ground truth is
   ready. Needs a decision on the disputed names.
2. **Steps 3–4:** candidate selection with honest PC/RR numbers. This is the
   core of the search-space reduction topic.
3. **Steps 5–6:** first Schematch run on a sample of 200 pairs, fast methods only.
4. **Steps 6–8:** add SBERT, the combiner and the selector.
5. **Steps 9–10:** the shared schema and the report.

Every stage ends with a checkable result, so it is always clear where we are
and what works.

## 7. Status and decisions

### Step 0: done

Maven 3.10.1 was installed with Homebrew. It brings its own JDK 27; build with
JDK 21 instead:

```shell
export JAVA_HOME=$(/usr/libexec/java_home -v 21)
mvn test compile dependency:build-classpath -Dmdep.outputFile=target/cp.txt
python tools/run_schematch.py
```

Maven 3.10 defaults to Surefire 3.6, which refuses to run because JUnit 4.11
arrives transitively on the test classpath. `pom.xml` therefore pins
`maven-surefire-plugin` 3.2.5. With that, 166 tests run (0 failures, 8 skipped)
and the bundled demo produces its matrix.

### Steps 1 and 2: implemented in `tools/wdc_prepare.py`

```shell
python tools/wdc_prepare.py clean        # step 1: cleaned copies + cleaning_report.csv
python tools/wdc_prepare.py headers      # header_review.csv for manual review
python tools/wdc_prepare.py groundtruth  # step 2: column_attributes.csv, attributes.csv
```

Outputs go to `datasets-local/wdc/` (git-ignored). Input files are not modified.

**Cleaning rules (step 1):**

- BOM removed from the first header.
- JSON list cells (`["31.70", "31.16"]`) keep their first non-empty element, so
  each row still describes one product. 3,341 cells.
- `undefined`, `null`, `none`, `nan`, `n/a`, `na`, `-` become empty. 233 cells.
- Numbers are normalized only in columns where at least 80% of the values parse
  as numbers, so codes in ID columns are never rewritten. Currency symbols and
  thousands separators are removed, a decimal comma becomes a dot, and
  scientific notation is expanded (`2.4E1` → `24`). Plain notation keeps its
  digits (`31.70`, `003489`). 1,489 cells.
- Columns that WDC split by JSON shape (`offers.price` / `offers[].price`) are
  joined into one column, but only when no row fills more than one of them.
  205 columns were joined; no group overlapped.
- Prices with free text, such as `77,572 (税込) (税抜価格 ¥70,520)`, are left
  unchanged for now (see step 4.5).

**Ground truth rules (step 2), agreed on 2026-11-17:**

1. `brand` and `brand.name` are one attribute, "brand".
2. `sku`, `offers.sku` and `url.sku` are one attribute. `productid` and `mpn`
   stay separate attributes.
3. `offers.X` and `X` are one attribute for offer and product properties that
   schema.org allows on both: price, pricecurrency, lowprice, highprice,
   pricevaliduntil, availability, itemcondition, category, offercount, mpn,
   gtin/gtin8/12/13/14, isbn. Names of other objects (`offers.name`,
   `url.name`, `brand.name` as a product name) stay separate.
4. `price`, `lowprice` and `highprice` are three attributes. Pairs between them
   are counted as related 1:n pairs and reported apart, not as true matches.

Some tables still contain two columns of the same attribute, for example `sku`
and `offers.sku`. Both count as correct partners (n:1).

**Numbers after cleaning:**

| Fact | Value |
|---|---|
| Columns | 1,243 (119 empty) |
| Non-empty columns | 1,124 |
| Shared attributes | 155 (58 occur in only one table) |
| Cross-table column pairs (non-empty) | 624,076 |
| True pairs | 29,268 |
| Related 1:n pairs (price/lowprice/highprice) | 3,469 |

The PC/RR numbers in the step 4 description above come from an early prototype
on the raw data. The measured numbers on the cleaned tables follow below.

### Step 3: deferred to step 5

Candidate selection never reads column names, only values, so names do not
need to be hidden to measure it. Names are hidden with `opaque-tables` when the
Schematch scenarios are written (step 5), because the matchers do see them.

### Step 4: implemented in `tools/wdc_candidates.py`

```shell
python tools/wdc_candidates.py evaluate --analyze type2 --write type2
```

Outputs in `datasets-local/wdc/candidates/`: `reduction_report.csv` (PC/RR of
every method), `lost_<method>.csv` (every lost true pair with sample values) and
`candidates.csv` (the selected candidate pairs).

PC and RR are computed over non-empty columns only: 624,076 cross-table pairs,
29,268 true pairs. "PC 1:n" is the share of related price/lowprice/highprice
pairs that survive.

| Method | Candidates | PC | RR | PC 1:n |
|---|---|---|---|---|
| Type blocking, one pass | 153,704 | 92.8% | 75.4% | 100% |
| **Type blocking, two passes (chosen)** | **177,736** | **98.6%** | **71.5%** | **100%** |
| Type blocking, three passes | 206,139 | 98.8% | 67.0% | 100% |
| Three passes + top-5 per table | 195,018 | 98.2% | 68.8% | 98.9% |
| Three passes + top-3 per table | 158,356 | 95.5% | 74.6% | 91.2% |
| Three passes + top-2 per table | 124,164 | 91.5% | 80.1% | 76.1% |
| Three passes + top-1 per table | 74,783 | 79.7% | 88.0% | 56.0% |
| Inverted index on value tokens | 118,066 | 61.9% | 81.1% | 36.6% |
| MinHash + LSH (b=16, r=4) | 5,351 | 12.3% | 99.1% | 0.1% |
| Three passes + size filter | 151,719 | 87.9% | 75.7% | 67.5% |

**Why two passes:** the third pass adds 0.2 points of PC for 28,000 more
candidates. Top-k on the cheap value profile costs about 3 points of PC for
every 3 points of RR. That trade is not worth it before the precise matchers
run; a top-k on real matcher scores can follow later.

**Loss analysis (step 4.5).** The first run on cleaned data kept 94.2%. Three
fixes to the column-type rules raised it to 98.6%:

| Lost before | Cause | Fix |
|---|---|---|
| availability, itemcondition | `InStock`, `in_stock`, `NewCondition` without the `schema.org/` prefix were typed as text | recognize the schema.org enumeration members in any spelling |
| pricevaliduntil | dates such as `2023/10/12` and `Oct. 21, 2023` | broader date patterns |
| sku, mpn, productid | long codes (`artworkid[…]-productid[…]`, hashes) were typed as text | codes up to 80 characters, including `[ ] : \|` |
| brand | three-letter brands (`ESR`) were typed as currency | currency only for ISO 4217 codes |

The remaining 420 lost pairs are mostly data errors or free text that no type
rule should catch: an `sku` that holds the shop's home page URL, an image given
as a URL in one shop and as a file name in another, or a condition written as
`New` / `Новый`.

### Steps 3 and 5: implemented in `tools/wdc_scenarios.py`

```shell
python tools/wdc_scenarios.py build      # 200 sampled shop pairs (seed 20261117), readable names
python tools/wdc_scenarios.py hide       # transform_dataset.py opaque-tables + permute
python tools/wdc_scenarios.py config fast-opaque --corpus opaque --matchers fast
python tools/wdc_scenarios.py run fast-opaque --jvm-arg=-Xmx6g
```

- One scenario per shop pair: `source/<shop>.csv`, `target/<shop>.csv`,
  `ground_truth/<shop>___<shop>.csv` (0/1 matrix from the ground truth rules).
  Empty columns are dropped, because they cannot be matched from values.
- The 200 pairs hold 24,973 column pairs and 1,150 true pairs (about 5.75 per
  scenario); every scenario has at least one true pair. The 133 related 1:n
  price pairs are listed in `related_pairs.csv`, not in the ground truth.
- `hide` writes a second corpus with salted table and column names and a
  permuted column order.
- `config` writes a configuration directory under `datasets-local/wdc/config/`.
  `run` puts it first on the classpath, so `src/main/resources` stays unchanged.
- The Java Precision/Recall metrics accept only 0/1 matrices, so raw scores are
  evaluated with `tools/evaluate_paper.py` (`paperEvaluation`, see
  [Evaluation](evaluation.md)). It needs NumPy/SciPy; the configuration uses
  `.venv/bin/python` (Python 3.13 with `requirements.txt`; the pinned NumPy has
  no build for Python 3.14).

**First results, fast Java value matchers, 200 scenarios, about 5 s per run:**

| Matcher | F1_H names hidden | F1_H names visible | MRR names hidden |
|---|---|---|---|
| CharDistributionMatcher | 0.435 | 0.435 | 0.753 |
| KolmogorovSmirnovMatcher | 0.379 | 0.391 | 0.681 |
| PatternProfileMatcher | 0.365 | 0.376 | 0.619 |
| JensenShannonValueMatcher | 0.259 | 0.259 | 0.448 |
| PMFMatcher | 0.152 | 0.186 | 0.498 |
| ValueShapeRoleMatcher | 0.136 | 0.139 | 0.441 |
| LevenshteinMatcher (labels) | — | 0.754 | — |

F1_H is the F1 of a Hungarian 1:1 assignment; MRR is the mean reciprocal rank
of the correct partner per source column. Value matchers give the same MRR with
and without names; the small F1_H differences come from tie-breaking after the
column permutation. The label matcher confirms that visible names give the
answer away, so the hidden-name corpus is the one to report.

### Steps 4, 7 and 8 on Schematch output: `tools/wdc_combine.py`

```shell
.venv/bin/python tools/wdc_combine.py results/<run> --corpus opaque
```

The script reads the raw matrices of a run (`paper_evaluation/input.jsonl`),
adds combined and filtered matrices, and evaluates everything again with
`tools/evaluate_paper.py`. Output: `results/<run>/wdc_combined/`.

- **Combiner:** the mean of min-max normalized matrices, per scenario.
- **Candidate filter:** two-pass type blocking from `tools/wdc_candidates.py`.
  Types are computed from the values of the scenario tables, so the filter also
  works on the hidden-name corpus. Non-candidate cells are set to 0.
- On the 200 scenarios the filter keeps 1,129 of 1,150 true pairs (PC 98.2%)
  and removes 17,926 of 24,973 cells (RR 71.8%).

**Results, names hidden:**

| Method | F1_H | F1_M | MRR |
|---|---|---|---|
| **Mean of all six value matchers + candidate filter** | **0.571** | **0.592** | **0.841** |
| Mean of CharDistribution, KS, PatternProfile + filter | 0.527 | 0.551 | 0.821 |
| Mean of all six value matchers | 0.506 | 0.566 | 0.812 |
| CharDistributionMatcher + filter | 0.498 | 0.504 | 0.789 |
| CharDistributionMatcher (best single matcher) | 0.435 | 0.467 | 0.753 |

The candidate filter does more than save work: it removes pairs of
incompatible types that the matchers wrongly scored high, so quality rises as
well. Combining all six matchers beats the three best ones; the weaker matchers
still contribute independent evidence.

**Selector (step 8):** for the best method, mutual top-1 and partial Hungarian
(penalty 0.3) both reach F1 0.592: precision 0.61, recall 0.60. A plain 1:1
Hungarian assignment reaches F1 0.571 (precision 0.52, recall 0.66), because
it also assigns columns that have no partner.

### Error analysis per attribute

```shell
.venv/bin/python tools/wdc_combine.py results/<run> --analyze "Mean(all value matchers) + candidate filter"
```

Hidden column labels are mapped back to readable names by identical column
values (values were not changed by `hide`). Pairs are selected by mutual top-1.
Output: `results/<run>/wdc_combined/errors_by_attribute.csv`.

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
| productid | 8 | 12% | 6% |
| mpn | 9 | 0% | 0% |

By column type, recall is 100% for currency, 96% for dates, 86% for
enumerations, but only 51% for text and 42% for IDs and numbers.

Most frequent wrong pairs: name → brand, name → description, description →
name, category → brand (text with different meaning); mpn → sku, productid →
sku, sku → mpn (codes); rating → price, reviewcount → sku (numbers).

**Conclusions:**

1. Columns with a fixed format (currency, date, availability) are solved by the
   value matchers. Nothing to gain there.
2. The weak spot is **text** (name, description, brand, category). The value
   matchers see only "short text vs long text"; they cannot tell a brand from a
   product name. This is where a multilingual SBERT model is expected to help.
3. **Product IDs** (sku, mpn, productid) are confused with each other. All of
   them are codes, and shops use them inconsistently, so neither format nor
   meaning separates them well. SBERT is not expected to help much here.
4. **Numbers** (price vs rating vs review count) could be separated by value
   ranges; ratings lie between 0 and 5 or 0 and 100.
5. Some reported errors are ties between two columns with identical values in
   the same table (for example `name` and `url.name`); both are selected and
   only one counts as correct. Precision is therefore slightly understated.

### Simpler alternative to a language model: role profiles (`tools/wdc_roles.py`)

The course material does not use language models. Its matcher classes are
labels, types/constraints, structure and instances. Since names are hidden, the
idea is to describe each column by the **role it plays in its own table**,
which needs no model and works in every language:

| Feature | Meaning | Median: brand / category / name / description |
|---|---|---|
| distinct | share of distinct values | 0.50 / 0.48 / 1.00 / 1.00 |
| words | mean number of words | 1.6 / 1.8 / 6.3 / 36.2 |
| inside | value occurs inside a longer value of another column in the same row | 45% / 9% / 6% / 0% |
| contains | value contains a shorter value of another column in the same row | 0% / 0% / 4% / 36% |

Further features: mean length, share of numbers, share of numbers in [0, 5]
(ratings), share of integers, magnitude of the median number. Features are
standardized over all tables; similarity = exp(-distance² / (2·dims)).

**Results (200 scenarios, names hidden):**

| Method | F1_H | F1_M | MRR |
|---|---|---|---|
| **Mean(6 value matchers + RoleProfile) + filter** | **0.582** | 0.582 | **0.847** |
| Mean(6 value matchers + 2× RoleProfile) + filter | 0.582 | 0.584 | 0.844 |
| Mean(6 value matchers) + filter | 0.571 | 0.592 | 0.841 |
| 50% value matchers + 50% RoleProfile + filter | 0.568 | 0.579 | 0.841 |
| RoleProfile + filter (alone) | 0.502 | 0.515 | 0.816 |
| CharDistributionMatcher + filter (best Schematch matcher alone) | 0.498 | 0.504 | 0.789 |

- Alone, RoleProfile is the strongest single matcher, with no model and only
  nine numbers per column.
- In the combination it adds about one point of F1_H and MRR. The Schematch
  matchers already capture length and repetition indirectly, so much of the
  signal overlaps.

**Where the brand pairs are really lost.** Rank of the correct partner per
source column (Mean of 6 + RoleProfile + filter):

| Attribute | Rank 1 | Top 3 |
|---|---|---|
| name | 77% | 99% |
| description | 76% | 97% |
| price | 72% | 89% |
| brand | 61% | 88% |
| sku | 48% | 86% |
| all others | 87% | 96% |

Brand is ranked first in 61% of the cases, but mutual top-1 keeps only about
half of those. Most misses are "matched to nothing": the brand column of the
target table prefers a different source column, so the pair is not mutual. The
weak point for brand is therefore the **selector**, not the matcher. Two
remedies follow the course material:

1. **Top-k with confirmation:** keep the best three candidates for uncertain
   columns and let a person confirm. That covers 86–99% of the true pairs.
2. **Holistic clustering (step 9):** a brand column that matches the brand
   columns of many other shops is a brand, even if one pairwise decision fails.

### Step 9: shared schema over all 100 shops (`tools/wdc_schema.py`)

```shell
python tools/wdc_scenarios.py build --pairs 0 --name all-readable    # 4,950 shop pairs
python tools/wdc_scenarios.py hide --name all-readable               # -> all-opaque
python tools/wdc_scenarios.py config all-fast --corpus all-opaque --no-eval
python tools/wdc_scenarios.py run all-fast --jvm-arg=-Xmx8g           # about 30 s
.venv/bin/python tools/wdc_schema.py results/<run> --corpus all-opaque --readable all-readable
```

The script combines the matrices of every shop pair exactly as the best method
of steps 7/8 (six value matchers + RoleProfile, candidate filter), writes all
scores into one 1,124 × 1,124 column similarity matrix and groups the columns.
Output in `results/<run>/wdc_schema/`: `clustering_report.csv` and
`global_schema.csv` (one row per cluster: shops, purity, attributes, members).

Scores are pair-wise precision/recall/F1 over all 29,268 true pairs:

| Method | Clusters | P | R | F1 |
|---|---|---|---|---|
| Pairwise decisions only (mutual top-1 per shop pair) | – | 0.607 | 0.582 | **0.594** |
| **One-per-shop merge along mutual top-1 edges (chosen)** | **65** | **0.631** | 0.542 | 0.583 |
| + join whole clusters, average score ≥ 0.4 | 51 | 0.626 | 0.543 | 0.581 |
| One-per-shop merge, all scores ≥ 0.6 | 206 | 0.664 | 0.500 | 0.570 |
| One-per-shop merge, all scores ≥ 0.8 | 669 | 0.877 | 0.266 | 0.409 |
| Average linkage, cut at 0.5 | 133 | 0.448 | 0.542 | 0.491 |
| Average linkage, cut at 0.7 | 13 | 0.183 | 0.873 | 0.303 |

- **Average linkage fails:** without a hard rule it merges several columns of
  the same shop and chains unrelated attributes (sku with price and review
  counts).
- **One-per-shop merge** joins the strongest mutual pairs first and refuses any
  merge that would put two columns of the same shop into one cluster. It has no
  tuned parameter, so its score is not optimistic. It turns 4,950 pairwise
  decisions into 65 consistent attributes at about the same F1.
- **The holistic step did not fix brand** (pairwise recall 31%, clusters 25%).
  Brand columns of different shops are split over several clusters, and their
  average similarity across clusters is low, so the second pass does not join
  them. The limit is the evidence in the values, not the selector: by values
  alone, brands look like categories, colours or languages (short repeated
  words).

**The resulting schema.** 36 of the 65 clusters cover at least 5 shops. The
largest ones (purity = share of the majority attribute):

| Cluster | Shops | Purity | Majority attribute | Mixed with |
|---|---|---|---|---|
| 1 | 90 | 87% | name | brand, url.name, offers.name |
| 2 | 84 | 100% | pricecurrency | – |
| 3 | 81 | 80% | description | name, url.description, review.reviewbody |
| 4 | 69 | 100% | availability | – |
| 5 | 60 | 88% | price | highprice, rating |
| 6 | 58 | 59% | brand | category, name, manufacturer |
| 7 | 58 | 57% | sku | highprice, mpn, lowprice |
| 9 | 34 | 62% | pricevaliduntil | review/publication dates |
| 14 | 28 | 93% | itemcondition | – |
| 16 | 25 | 96% | aggregaterating.ratingvalue | – |

## 8. Prototype

The early prototype was replaced by `tools/wdc_candidates.py`.
