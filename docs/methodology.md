# Methodology

Run as at **2026-11-08** (GLEIF publish date). Every figure here is rendered from `outputs/audit_record.json`.

## Sources

| Source | Edition | Format | Licence / terms |
|---|---|---|---|
| CBI register of authorised UCITS | 2026-10-16 | PDF | public register, https://registers.centralbank.ie/downloadspage.aspx |
| CBI registers of AIFs: ICAV, DIC, Unit Trust, CCF, ILP | 2026-10-27, 2026-09-02, 2026-09-08, 2026-09-02, 2026-10-14 | PDF | as above |
| CBI register of authorised and registered AIFMs | 2026-10-13 | PDF | as above |
| CBI register of authorised UCITS management companies | 2026-10-13 | PDF | as above |
| GLEIF Golden Copy Level 1 (LEI-CDF) | `20261108-0800-gleif-goldencopy-lei2-golden-copy.csv.zip` | CSV in zip | CC0 1.0 |
| GLEIF Golden Copy Level 2 relationships (RR-CDF) | `20261108-0800-gleif-goldencopy-rr-golden-copy.csv.zip` | CSV in zip | CC0 1.0 |

File sizes and SHA-256 hashes are in `data/manifest.json`.

**A note on the CBI source.** The CBI's *Monthly List of Authorised Funds* lists only the funds authorised in that month, not the full population. This project therefore uses the full registers, which list every currently authorised fund and sub-fund with its authorisation date.

**GLEIF route.** The full Level 1 Golden Copy is too large to upload, so `scripts/gleif_ie_filter.py` streams it locally. It scanned 3,488,216 Level 1 and 492,776 Level 2 rows, and kept:
- 14,637 Irish fund LEIs (legal jurisdiction or legal address IE, category FUND);
- 24,228 other Irish entities, used to catch funds registered under a different category;
- 20,290 relationships touching an Irish fund;
- 492 linked managers and parents.

## Step 1: data layer

**PDF extraction.** For each page, the extractor locates the column starts from the header row and assigns each word to a column by its x position, so an empty cell does not shift values. Each row is classified as follows:
- **Umbrella or standalone vs sub-fund.**
  - UCITS, DIC, Unit Trust and ILP registers: umbrella and standalone rows carry a long date (`05 Jul 1989`), sub-funds a short date (`19-Nov-90`).
  - ICAV register: every row has a long date, so an umbrella or standalone row is one with a Yes/No value in the Internally Managed column.
  - CCF register: a top-level row starts a new block and carries the qualifying-investor key.
- **Pages without a header.** 5 ICAV pages have no repeated header, so the previous page's columns are reused.
- **Rows with no fund name.** Dated rows that have no fund name are kept so the counts reconcile, then quarantined by rule Q01.

**Reconciliation to printed control totals.** Every register prints two totals: top-level funds (umbrellas plus standalone) and standalone plus sub-funds. 11 of 12 reconcile exactly.

| Register | As at | Printed top-level | Parsed | Printed standalone + sub-funds | Parsed | Status |
|---|---|---:|---:|---:|---:|---|
| AIF: Common Contractual Fund | 2026-09-02 | 22 | 22 | 104 | 104 | exact |
| AIF: Designated Investment Company | 2026-09-02 | 172 | 172 | 762 | 762 | exact |
| AIF: ICAV | 2026-10-27 | 624 | 624 | 2,056 | 2,056 | exact |
| AIF: Investment Limited Partnership | 2026-10-14 | 91 | 91 | 101 | 101 | exact |
| UCITS (all legal forms) | 2026-10-16 | 557 | 557 | 6,349 | 6,350 | variance +1 |
| AIF: Unit Trust | 2026-09-08 | 92 | 92 | 536 | 536 | exact |

The single UCITS variance most likely comes from an umbrella authorised before any sub-fund was approved: it is parsed as standalone, while the CBI does not count it. It is reported, not forced to zero.

**Layers (DuckDB).**
- **Bronze:** raw parsed rows and raw GLEIF columns, all as text.
- **Silver:** typed and normalised records, data-quality findings, a quarantine table, and deduplicated GLEIF entities.
- **Gold:** matches, exceptions, the fund dimension, and market-structure tables.

**Data-quality rules.**

| Rule | Action | Description | Records |
|---|---|---|---:|
| Q01 | quarantine | Fund name missing in source row | 5 |
| W01 | flag | Duplicate fund name within the same umbrella | 1 |
| W02 | flag | Sub-fund authorised before its umbrella | 9 |
| W03 | flag | No management company / AIFM printed and not internally managed | 25 |
| W10 | flag | LEI present in more than one extract pool (deduplicated) | 129 |
| Q10, Q11 | quarantine | GLEIF LEI fails ISO 17442 check digits, or legal name missing | 0 |

**Name normalisation (`normalise.py`).** Every name gets two keys: `name_key` for exact equality and `name_core` for core and fuzzy comparison. Normalisation steps:
- Accents folded and case unified.
- All dash characters unified; intra-word hyphens become spaces.
- `&` becomes `AND`; currency symbols become codes (`€` to EUR, `$` to USD, `£` to GBP).
- `(IRL)` kept as a token; punctuation removed.
- Trailing legal-form words stripped repeatedly: PLC, PUBLIC LIMITED COMPANY, ICAV, DAC, LIMITED, CCF, COMMON CONTRACTUAL FUND, ILP, UNIT TRUST and similar; a leading THE is also dropped.
- GLEIF names are split into umbrella and sub-fund parts on `' - '`, `' : '`, `' / '` and `', a sub-fund of'`.
- Manager names drop trading names (`t/a ...`).

## Step 2: reconciliation

Methods run in order; the first hit wins:

| Method | Rule | Accepted |
|---|---|---:|
| EXACT_NAME | `name_core` equal (sub-funds: umbrella + sub-fund name) | 1,434 |
| EXACT_IN_UMBRELLA | sub-fund name equals the sub-fund part of a GLEIF record linked to the matched umbrella (IS_SUBFUND_OF, or umbrella name prefix) | 7,968 |
| EXACT_UNIQUE_SUBFUND | sub-fund `name_core` (3+ tokens) equals exactly one GLEIF name | 524 |
| FUZZY_IN_UMBRELLA | rapidfuzz `token_sort_ratio` within the umbrella's candidates | 26 |
| FUZZY_GLOBAL | `token_sort_ratio` against unassigned Irish GLEIF names | 4 |

**Thresholds.**
- Accept at a score of 92 or above.
- Scores from 85 up to the accept threshold go to the review band (exception E15) and are not counted as matched.
- Among several LEIs with the same name, the order of preference is ISSUED, then pending, then LAPSED, then other statuses.
- Each LEI is assigned to one CBI fund only; 11 clashes were resolved in favour of the stronger method.

**Guards.**
- **Distinguishing tokens.** Numbers, roman numerals, currency codes and tokens of two characters or fewer must be identical on both sides, so `Fund XII` never matches `Fund XIV` and `UK` never matches `World`. This rejected 534 candidates.
- **Legal-form conflict.** Both names carry a legal form and the forms differ, as in `... Fund I ICAV` vs `... Fund I ILP`. This rejected 64 candidates.

**Measuring precision.**
- **Tuning review.** A first review (`data/manual_review/tuning_sample_seed20261108.csv`) exposed the sibling and legal-form errors that the guards now block.
- **Evaluation sample.** After the guards were in place, an evaluation sample was drawn with seed 20261109: 20 records from each exact method, every accepted fuzzy match, and every review-band match. Each pair was labelled CORRECT, INCORRECT or UNCERTAIN. Strict precision counts UNCERTAIN as wrong. All 113 labelled pairs still match the current run.

| Stratum | Sampled | Correct | Uncertain | Incorrect | Precision (strict) |
|---|---:|---:|---:|---:|---:|
| EXACT_NAME | 20 | 20 | 0 | 0 | 100.0% |
| EXACT_IN_UMBRELLA | 20 | 20 | 0 | 0 | 100.0% |
| EXACT_UNIQUE_SUBFUND | 20 | 20 | 0 | 0 | 100.0% |
| FUZZY_ACCEPTED | 30 | 26 | 3 | 1 | 86.7% |
| FUZZY_REVIEW_BAND | 23 | 4 | 7 | 12 | 17.4% |

The review band is far less reliable (17.4% strict), which is why it is held out of the match rate.

## Step 3: exceptions

Rules, severities and owner queues are defined in `exceptions.py` and exported in the Rules sheet of the Excel pack. Severity reflects reporting impact:
- **HIGH:** the identifier is unusable or contradicts the authorisation status.
- **MEDIUM:** stale or conflicting data that would mislead a report.
- **LOW:** completeness or preventive items.

Two further details:
- **Renewal window.** The renewal warning window is 60 days.
- **Manager check.** The manager check compares the CBI ManCo (UCITS) or AIFM (AIF) with the GLEIF IS_FUND-MANAGED_BY entity. It takes the higher of rapidfuzz `token_sort_ratio` and `token_set_ratio`, with a threshold of 85. A mismatch where the brand (the first token) is the same goes to E18 (LOW); otherwise it goes to E11 (MEDIUM).

## Step 4: market structure

- **Unit of measure.** The unit is the investable portfolio, meaning standalone funds plus sub-funds; umbrellas are counted separately.
- **Manager.** The manager is the register's ManCo or AIFM, and sub-funds inherit their umbrella's.
- **Manager register mapping.** 66.6% of top-level funds' managers map to an entry in the Irish AIFM or UCITS ManCo register (fuzzy score 90 or above). The rest are mainly EU ManCos managing Irish funds cross-border.
- **Concentration.** HHI is the sum of squared percentage shares across named managers.
