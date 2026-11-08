# Power BI build guide

The export in `outputs/powerbi/` is a star schema. Load every CSV with **Get data > Text/CSV**, then set the relationships and measures below. Row counts in this run: fund dimension 11,190, exceptions 6,312.

## Model

| Table | Grain | Key |
|---|---|---|
| `fact_match` | one row per CBI fund | `fund_key` |
| `fact_exception` | one row per open exception | `exception_id` |
| `dim_fund` | CBI fund, umbrella or sub-fund | `fund_key` |
| `dim_manager` | ManCo / AIFM as printed on the CBI register | `manager_key` |
| `dim_register` | CBI register | `register_key` |
| `dim_lei` | GLEIF entity | `lei_key` |
| `dim_rule` | QC rule (severity, owner queue) | `rule_key` |
| `dim_date` | calendar | `date_key` |

**Relationships**

All relationships are many-to-one, with single filter direction.
- `fact_match[fund_key]` to `dim_fund[fund_key]`
- `fact_match[lei_key]` to `dim_lei[lei_key]`
- `fact_exception[fund_key]` to `dim_fund[fund_key]`
- `fact_exception[rule_key]` to `dim_rule[rule_key]`
- `fact_exception[lei_key]` to `dim_lei[lei_key]`. Make this one inactive, because `fund_key` already reaches `dim_lei` through `fact_match`.
- `dim_fund[manager_key]` to `dim_manager[manager_key]`
- `dim_fund[register_key]` to `dim_register[register_key]`
- `dim_fund[auth_date]` to `dim_date[date_key]`. Make this one inactive; the `Authorised (by auth_date)` measure activates it with USERELATIONSHIP.

In Power Query, set the column types:
- `auth_date`, `next_renewal_date`, `as_of_date` and `date_key`: Date
- `score`: Decimal number
- `is_accepted`: Whole number

**Measures**

Paste the measures from `outputs/powerbi/measures.dax` into a new `Measures` table.

## Pages

1. **Overview.** Cards: `Funds`, `Match Rate`, `Open Exceptions`, `High Exceptions`. Clustered bar: `Match Rate` by `dim_register[register_label]`. Slicer: `dim_manager[manager_name]` (search enabled). The match rate should read 89.0% with no filter applied.
2. **Exception queue.** Table: `fact_exception[exception_id]`, `dim_rule[rule_name]`, `dim_rule[severity]`, `dim_rule[owner_queue]`, `dim_fund[fund_name]`, `fact_exception[detail]`. Sort by `dim_rule[severity_order]`. Slicers: owner queue and severity. Stacked bar: `Open Exceptions` by `dim_rule[owner_queue]`, legend `severity`. Conditional formatting: severity HIGH red, MEDIUM amber.
3. **LEI health.** Donut: `Funds` by `dim_lei[registration_status]`. Card: `Lapsed LEIs`, which should read 845. Card: `Renewals Due 60d`. Table of E01 and E04 rows filtered by `dim_rule[rule_key]`.
4. **Market structure.** Bar: `Portfolios` by `dim_manager[manager_name]`, top N = 15 filter. Card: `Sub-funds per Umbrella`. Line: `Authorised (by auth_date)` by `dim_date[year]`, legend `dim_register[regime]`. Add a text box: "currently authorised funds only (survivorship)".
5. **Reconciliation.** Matrix: `dim_register[register_label]` by `fact_match[band]` with `COUNTROWS(fact_match)`. Drill through from a register to the unmatched funds.

## Check before sharing

Each figure in the report should match the audit record with no filter applied:
- `Funds` = 11,190
- `Accepted Matches` = 9,956
- `Open Exceptions` = 6,312
- `High Exceptions` = 109
