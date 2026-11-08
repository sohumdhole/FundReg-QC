"""Step 5b: Power BI star-schema export (outputs/powerbi/*.csv) and DAX measures."""
from __future__ import annotations

from . import config as C

TABLES = {
    "dim_register": """SELECT register AS register_key, register_label, regime, CAST(as_at AS DATE) AS register_as_at FROM (
        SELECT t.register, t.as_at, CASE WHEN t.register='UCITS' THEN 'UCITS' ELSE 'AIF' END AS regime,
               CASE t.register WHEN 'UCITS' THEN 'UCITS (all legal forms)' WHEN 'ICAV' THEN 'AIF: ICAV'
                    WHEN 'DIC' THEN 'AIF: Designated Investment Company' WHEN 'UNIT_TRUST' THEN 'AIF: Unit Trust'
                    WHEN 'CCF' THEN 'AIF: Common Contractual Fund' ELSE 'AIF: Investment Limited Partnership' END AS register_label
        FROM bronze_cbi_totals t) ORDER BY 1""",
    "dim_manager": """SELECT DENSE_RANK() OVER (ORDER BY manager_name) AS manager_key, manager_name,
                             MAX(manager_cbi_reference) AS cbi_reference
                      FROM gold_fund GROUP BY manager_name ORDER BY 1""",
    "dim_fund": """SELECT f.fund_id AS fund_key, f.fund_name, f.register AS register_key, f.structure,
                          f.umbrella_fund_id AS umbrella_fund_key, f.umbrella_name, m.manager_key, f.auth_date,
                          f.qiaif_flag, f.closed_ended_flag, f.limited_liquidity_flag
                   FROM gold_fund f JOIN (SELECT DENSE_RANK() OVER (ORDER BY manager_name) AS manager_key, manager_name
                                          FROM gold_fund GROUP BY manager_name) m USING (manager_name) ORDER BY 1""",
    "dim_lei": """SELECT lei AS lei_key, legal_name, registration_status, entity_status, entity_category,
                         legal_jurisdiction, next_renewal_date, initial_registration_date, managing_lou, pool AS extract_pool
                  FROM silver_gleif_entity
                  WHERE lei IN (SELECT lei FROM gold_match WHERE lei IS NOT NULL UNION SELECT lei FROM gold_exception WHERE lei IS NOT NULL)
                  ORDER BY 1""",
    "dim_rule": """SELECT rule_id AS rule_key, rule_name, severity,
                          CASE severity WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END AS severity_order,
                          owner_queue, why_it_matters, next_action FROM gold_rule_catalogue ORDER BY 1""",
    "dim_date": """SELECT CAST(d AS DATE) AS date_key, year(d) AS year, month(d) AS month, strftime(d, '%Y-%m') AS year_month
                   FROM range(DATE '1985-01-01', DATE '2028-12-31', INTERVAL 1 DAY) t(d) ORDER BY 1""",
    "fact_match": """SELECT fund_id AS fund_key, lei AS lei_key, method, score, band,
                            CASE WHEN band='ACCEPTED' THEN 1 ELSE 0 END AS is_accepted FROM gold_match ORDER BY 1""",
    "fact_exception": """SELECT exception_id, rule_id AS rule_key, fund_id AS fund_key, lei AS lei_key, register AS register_key,
                                as_of AS as_of_date, status, detail FROM gold_exception ORDER BY 1""",
}

DAX = r"""-- FundReg-QC DAX measures (create in a 'Measures' table)
Funds = COUNTROWS ( dim_fund )
Accepted Matches = SUM ( fact_match[is_accepted] )
Match Rate = DIVIDE ( [Accepted Matches], [Funds] )
Open Exceptions = COUNTROWS ( fact_exception )
High Exceptions = CALCULATE ( [Open Exceptions], dim_rule[severity] = "HIGH" )
Medium Exceptions = CALCULATE ( [Open Exceptions], dim_rule[severity] = "MEDIUM" )
Low Exceptions = CALCULATE ( [Open Exceptions], dim_rule[severity] = "LOW" )
Funds With Exception = DISTINCTCOUNT ( fact_exception[fund_key] )
Exception Rate = DIVIDE ( [Funds With Exception], [Funds] )
Lapsed LEIs = CALCULATE ( [Open Exceptions], dim_rule[rule_key] = "E01" )
Renewals Due 60d = CALCULATE ( [Open Exceptions], dim_rule[rule_key] = "E04" )
Unmatched Funds = CALCULATE ( COUNTROWS ( fact_match ), fact_match[band] = "UNMATCHED" )
Portfolios = CALCULATE ( [Funds], dim_fund[structure] IN { "STANDALONE", "SUB_FUND" } )
Manager Share of Portfolios =
    DIVIDE ( [Portfolios], CALCULATE ( [Portfolios], ALL ( dim_manager ) ) )
Sub-funds per Umbrella =
    DIVIDE ( CALCULATE ( [Funds], dim_fund[structure] = "SUB_FUND" ),
             CALCULATE ( [Funds], dim_fund[structure] = "UMBRELLA" ) )
Authorised (by auth_date) =
    CALCULATE ( [Portfolios], USERELATIONSHIP ( dim_fund[auth_date], dim_date[date_key] ) )
"""


def export(con):
    out = C.OUT / "powerbi"
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, sql in TABLES.items():
        con.execute(f"COPY ({sql}) TO '{out / (name + '.csv')}' (HEADER, DELIMITER ',')")
        counts[name] = con.execute(f"SELECT COUNT(*) FROM ({sql})").fetchone()[0]
    (out / "measures.dax").write_text(DAX)
    return counts
