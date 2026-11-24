# Ground truth: WDC Product tables

Reference correspondences for the 100 web-shop tables in `vorbereitete_tabellen/`
(first 100 records per shop). Every column gets one **concept**; two columns of
different tables correspond when they have the same concept. Built with
`tools/wdc_gt.py`.

## Principle

Labels follow the **meaning of the values**, not the schema.org header. Headers
only gave the first draft. Where a site marked something up under the wrong
property, the label follows the values. Examples: `brand` holding the platform's
name becomes `seller`, `productid` holding EAN codes becomes `gtin`, and
`description` repeating `name` becomes `name`.

Several columns of one table may share a concept when they hold the same
information (`url.name` = `name`). The reference therefore allows 1:n within a
table pair. Even a perfect matcher restricted to mutual top-1 cannot reach 100%
recall on these.

## Result

| | |
|---|---|
| Columns (after `wdc_prepare.py clean`) | 1,243 |
| Evaluated (non-empty, not excluded) | 1,110 |
| Empty in the 100 rows / excluded as broken | 119 / 14 |
| Concepts / in two or more tables | 82 / 46 |
| Corresponding column pairs across tables | 32,687 |
| Complex correspondences (price ↔ lowprice + highprice) | 1,235 |

## Files

| File | Content | Edited by |
|---|---|---|
| `draft_ai.csv` | AI pre-labels per raw column (sheet `Attributgruppen_1448` of `SchemaMatching_GroundTruth_KI_vorbewertet.xlsx`, based only on header paths) | `wdc_gt.py import-xlsx` |
| `concept_rules.csv` | Group-level decisions: AI group → concept | hand |
| `overrides.csv` | Column-level decisions after looking at the values | hand |
| `complex_rules.csv` | n:m rules between concepts | hand |
| `audit_accepted.csv` | Audit flags that were reviewed and are fine | hand |
| `columns.csv` | **The ground truth**: table, column, concept, source of the label, reason | `wdc_gt.py build` |
| `concepts.csv` | Concepts with their tables and column names | `wdc_gt.py build` |
| `complex.csv` | Complex correspondences per table pair | `wdc_gt.py build` |

Labels are applied in this order: AI draft, `concept_rules.csv`, then two value
rules in code, then `overrides.csv`. The value rules are: identifier columns
whose values are ≥ 80% valid GTIN/EAN/UPC/ISBN codes become `gtin`; an `mpn` or
`productid` equal to the shop's `sku` in ≥ 90% of rows becomes `sku`. The
`source` column of `columns.csv` names the layer that decided each label, and
`reason` gives the reason.

## Decisions (2026-11-24)

- `ratingcount` = `reviewcount`; `minprice`/`maxprice` = `lowprice`/`highprice`;
  `manufacturer` = `producer` = `brand`; `offers.name` = `name`.
- Identifiers by value type: `gtin` (GTIN/EAN/UPC/ISBN), `sku` (shop article
  number), `mpn` (manufacturer number), `productid` (other ids).
- `url.*` is the product object nested under `url`, so `url.name` = `name`, etc.
- Platform or site names in `brand` (CafePress, dhgate, iStock, …) and
  restaurants (skipthedishes) are `seller`. Own-brand shops (Burberry, PUMA, …)
  stay `brand`.
- `author` = `creator` (artists, designers, musicians, authors).
- `availabilityends` / `validthrough` = `pricevaliduntil`; `availabilitystarts` = `validfrom`.
- `price`, `lowprice` and `highprice` are separate concepts. `complex.csv` adds
  `price` ↔ `lowprice` + `highprice` (+ `price`) for every pair where one shop
  has no price range and the other has one.
- Constant shop-level ratings stay `ratingvalue`. Placeholders and broken markup
  are `_exclude` (see `overrides.csv`). Some of these decisions were taken
  earlier, on 2026-11-17.

## Verification and limits

- `wdc_gt.py audit` checks every evaluated column against its concept. It looks
  at value type, columns that should vary but are constant, concept-specific
  expectations, and copies inside a table. All 35 flags were reviewed: they are
  either in `audit_accepted.csv` or fixed through rules or overrides. No flags
  are open.
- Of the 1,110 evaluated labels, 158 were set by a decision: 100 by concept
  rules, 26 by value rules and 32 by overrides. The other 952 come unchanged
  from the AI draft. A random sample of 60 of those, checked against their
  values, had no errors. The error rate of the AI-derived labels is therefore
  probably below about 5% (rule of three).
- The labels were reviewed by one reviewer with an AI assistant, not by two
  independent people. For a scientific claim, have a second person label a
  random sample of columns and report the agreement (Cohen's κ).
- The labels describe the first 100 records per shop. With another sample
  (`wdc_expand.py --sample random`), rebuild and audit again: columns that are
  empty or filled change.
- The labels come partly from header names. Score label-based matchers
  (Levenshtein) only with that caveat.

## Rebuild

```shell
python tools/wdc_gt.py build           # after editing a hand-edited file
.venv/bin/python tools/wdc_gt.py audit # should report 0 open flags
```
