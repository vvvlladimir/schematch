# Plan: Matching the columns of 100 web-shop product tables

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

## 7. Prototype

The numbers in step 4 come from a standard-library Python prototype that has
not been added to the repository yet. It will be added as part of deliverable 2.
