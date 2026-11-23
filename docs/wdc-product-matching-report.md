# Report: Schema matching of 100 web-shop product tables

Matching the 100 product tables in `vorbereitete_tabellen/` with Schematch,
following the Data Engineering practical (Einführung, WS 2026/27): search-space
reduction, pairwise matching and clustering (step 1 of the "Vorgehensweise").

## 1. Summary

- **Data:** 100 web-shop tables from the WDC Schema.org Table Corpus 2023
  (class Product), 1,124 non-empty columns, 624,076 cross-table column pairs.
- **No ground truth.** As proposed on slide 13, all matchers were run on all
  pairs (4,950 shop pairs, about 30 seconds); the correspondences each matcher
  selects serve as the reference for evaluating the blockers.
- **Blockers:** eight blockers from the course ideas were compared. Two-pass
  type blocking removes 71.5% of all pairs and keeps 91–99% of the
  correspondences of four of the seven matchers (95.8% of the combined
  matcher). It suits KolmogorovSmirnov less (74%) and PMF and ValueShapeRole
  not at all (38–43%). Token-based blockers (inverted index, MinHash/LSH) are
  unsuitable here.
- **Clustering:** the pairwise correspondences of the combined matcher, with
  two-pass type blocking as prefilter, form 59 clusters, i.e. attributes of a
  shared Product schema.

## 2. Data and cleaning

One table per web shop, 100 products each, values in many languages and
currencies. Column headers are flattened schema.org property paths
(`offers.price`, `brand.name`).

| | Raw | After cleaning |
|---|---|---|
| Tables | 100 | 100 |
| Columns | 1,448 | 1,243 |
| Empty columns | 234 | 119 |
| Non-empty columns | 1,214 | 1,124 |
| Cross-table pairs of non-empty columns | – | 624,076 |

| Cleaning rule | Cells / columns |
|---|---|
| JSON list cells (`["31.70", "31.16"]`) keep their first element | 3,341 cells |
| `undefined`, `null`, `n/a`, … become empty | 233 cells |
| Numbers normalized (`2.4E1` → 24, `$116.45` → 116.45) in mostly numeric columns | 1,489 cells |
| Columns split by JSON shape (`offers.price` / `offers[].price`) joined when they never overlap | 205 columns |

Empty columns are left out: there is nothing to match from.

## 3. Pipeline

```
clean → scenarios (all shop pairs) → Schematch matchers → blocker evaluation
                                                        → clustering (shared schema)
```

| Step | Tool |
|---|---|
| Cleaning | `tools/wdc_prepare.py` |
| Scenarios, Schematch configuration and run | `tools/wdc_scenarios.py` |
| Blockers and their evaluation | `tools/wdc_candidates.py` |
| Reading matcher outputs, mutual top-1 | `tools/wdc_matches.py` |
| Clustering | `tools/wdc_schema.py` |

**Matchers** (fast Schematch first-line matchers, no Python server, no model):
one label matcher (`LevenshteinMatcher`, on the column names) and six instance
matchers (`PMFMatcher`, `KolmogorovSmirnovMatcher`, `JensenShannonValueMatcher`,
`ValueShapeRoleMatcher`, `CharDistributionMatcher`, `PatternProfileMatcher`).
The **combined matcher** is the mean of the seven min-max normalized score
matrices. A matcher's **correspondences** are its mutual top-1 pairs per shop
pair: both columns are each other's best partner with a positive score.

| Matcher | Correspondences |
|---|---|
| CharDistribution | 24,180 |
| JensenShannonValue | 7,928 |
| KolmogorovSmirnov | 27,224 |
| Levenshtein | 30,240 |
| PMF | 47,083 |
| PatternProfile | 39,712 |
| ValueShapeRole | 19,798 |
| Combined (mean) | 28,818 |

## 4. Search-space reduction

**Evaluation (slide 13).** The blockers act as a prefilter for the matchers:
they should remove only pairs that are unlikely to match. The correspondences
a matcher finds on *all* pairs are its reference:

- **PC** (pair completeness) = correspondences of the matcher kept by the
  blocker / all correspondences of the matcher;
- **RR** (reduction ratio) = 1 − candidates / all 624,076 pairs.

A blocker suits a matcher when PC is close to 100% and RR is high. Blockers
look only at values, never at column names.

| Blocker | RR | CharDist | JS | KS | Leven. | PMF | Pattern | VShape | **Mean** |
|---|---|---|---|---|---|---|---|---|---|
| Type, one pass | 75.4% | 86.7% | 98.9% | 71.5% | 88.5% | 33.9% | 96.5% | 40.3% | 92.5% |
| **Type, two passes** | **71.5%** | 91.1% | 99.4% | 74.0% | 92.6% | 38.0% | 98.6% | 43.4% | **95.8%** |
| Type, three passes | 67.0% | 91.3% | 99.4% | 79.2% | 93.0% | 48.7% | 98.7% | 48.1% | 96.0% |
| Three passes + top-3 per table | 74.6% | 90.8% | 98.1% | 76.1% | 90.3% | 40.2% | 93.8% | 41.8% | 95.1% |
| Three passes + top-1 per table | 88.0% | 82.1% | 89.7% | 64.0% | 75.1% | 25.8% | 69.8% | 27.0% | 84.1% |
| Three passes + size filter | 75.7% | 84.3% | 87.0% | 71.3% | 82.8% | 48.4% | 70.8% | 39.9% | 90.1% |
| Inverted index on value tokens | 81.1% | 61.7% | 77.5% | 54.6% | 61.0% | 32.4% | 32.0% | 29.6% | 64.4% |
| MinHash + LSH (16 bands × 4 rows) | 99.1% | 15.0% | 34.5% | 8.8% | 11.7% | 3.2% | 8.7% | 4.3% | 12.7% |

**Blockers.**

- **Type blocking:** each column gets a type from the majority of its first
  100 values: number, date, currency (ISO 4217 code), URL, schema.org
  enumeration, code, text. Only columns of the same type are compared. The
  second pass also compares number with code (an SKU is numeric in one shop
  and alphanumeric in another); the third also code with text.
- **Top-k:** after three-pass blocking, keep for each column its k nearest
  columns per other table by nine value features (length, share of digits,
  letters, spaces, punctuation, upper case, distinct values, words).
- **Size filter:** drop pairs whose distinct-value counts differ by more than
  a factor of ten.
- **Inverted index / MinHash + LSH:** pairs of columns that share value tokens.

**Findings.**

1. **Two-pass type blocking is the best compromise** for the combined matcher:
   95.8% of its correspondences at 71.5% reduction. A third pass adds 0.2 PC
   for 4.5 points of RR.
2. **Which blocker suits which matcher** (the question of slide 13): type
   blocking suits JensenShannon, PatternProfile, CharDistribution and
   Levenshtein (91–99%). It does not suit PMF and ValueShapeRole: most of
   their correspondences pair columns of different types (e.g. a number with
   a text). Without ground truth it stays open whether those are true
   correspondences the blocker loses or wrong ones it rightly removes.
3. **Top-1 and the size filter** buy RR with PC roughly one to one; top-1
   reaches 88% RR but loses 16% of the combined matcher's correspondences.
4. **Token-based blockers fail.** Different shops sell different products in
   different languages, so columns with the same meaning share almost no
   values. Inverted index and MinHash/LSH therefore lose most correspondences.

## 5. Clustering

The combined matcher with two-pass type blocking as prefilter (pairs the
blocker removes get score 0) gives 28,688 pairwise correspondences over the
4,950 shop pairs. They are clustered like duplicates in entity matching, with
one rule: **one column per shop and cluster**. Correspondences are joined
strongest first; a join that would put two columns of the same shop into one
cluster is refused.

Result: **59 clusters** (33 with at least five shops, 16 single columns),
written to `results/<run>/global_schema.csv`. Largest clusters:

| Shops | Column names in the cluster |
|---|---|
| 97 | name (94), review.name, brand.name, image.name |
| 91 | description (89), review.description, offers.url |
| 84 | offers.pricecurrency (83), pricecurrency |
| 80 | offers.price (74), aggregaterating.ratingvalue, offers.lowprice, … |
| 70 | offers.availability (70) |
| 64 | sku (59), offers.price (2), … |
| 61 | brand.name (38), brand (14), manufacturer.name, … |
| 42 | aggregaterating.reviewcount (27), aggregaterating.ratingcount (4), … |
| 34 | offers.pricevaliduntil (33), offers.validfrom |
| 33 | brand.name (14), brand (6), inlanguage, offers.category, … |
| 32 | mpn (18), gtin13 (4), offers.gtin12 (3), gtin12 (3), … |
| 29 | aggregaterating.ratingvalue (27), … |

Clusters of fixed formats (currency, availability, validity date) are clean.
Brand is split over two clusters, and the product identifiers (sku, mpn, gtin)
mix, because by their values alone they are hard to tell apart.

## 6. Limitations and open points

- The reference for PC is each matcher's own decisions, not the truth: PC
  measures how much a blocker changes a matcher's result, not whether that
  result is correct. Mutual top-1 always picks a partner when one has a
  positive score, so weak matchers produce many doubtful correspondences.
- Only the WDC Product corpus was used, not WikiDBs, SchemaPile or
  SportsTables.
- Only 1:1 correspondences are derived; 1:n and n:m correspondences
  (e.g. `price` vs `lowprice`/`highprice`) are not.
- Step 2 of the "Vorgehensweise" (dependencies between corresponding columns:
  co-occurrence, encodings, merges, units, naming rules) is not done.
- Blocking and clustering are Python tools around Schematch, not part of the
  Java framework.
- Not tried from the course ideas: blockers on column names, on joinable
  attributes, on entropy, on embeddings of value sets; sorted neighbourhood
  and canopy clustering.

## 7. Reproduction

From the repository root, with JDK 21 (`export JAVA_HOME=$(/usr/libexec/java_home -v 21)`),
a built Schematch (`mvn -DskipTests compile dependency:build-classpath -Dmdep.outputFile=target/cp.txt`)
and `.venv` with `requirements.txt`:

```shell
python tools/wdc_prepare.py clean
python tools/wdc_scenarios.py build
python tools/wdc_scenarios.py config
python tools/wdc_scenarios.py run --jvm-arg=-Xmx8g
.venv/bin/python tools/wdc_candidates.py evaluate results/<run>
.venv/bin/python tools/wdc_schema.py results/<run>
```

Outputs are written to `datasets-local/wdc/` and `results/` (both git-ignored).
