"""Step 3: quality-control exception queue.

Every rule has a severity, an owner queue (who would work it in a data
operations team), a plain-English reason it matters, and a next action.
Rules run only on CBI funds with an ACCEPTED match unless stated otherwise.
"""
from __future__ import annotations

from rapidfuzz import fuzz

from . import config as C
from .normalise import manager_core

OWNERS = {
    "LEI_OPS": "LEI Operations",
    "FUND_REG": "Fund Registration",
    "STATIC": "Static Data and Relationships",
    "GOV": "Data Governance",
}

RULES = {
    "E01": ("LEI lapsed", "MEDIUM", "LEI_OPS",
            "The LEI was not renewed on time. It still identifies the fund, but its reference data has not been "
            "re-validated within the one-year cycle, and GLEIF notes regulators increasingly check that reported LEIs "
            "are renewed. Counterparties and reporting engines often treat a lapsed record as stale.",
            "Contact the fund's LEI issuer (or the management company) to renew; confirm the reference data."),
    "E02": ("LEI retired, annulled or duplicate while fund is authorised", "HIGH", "FUND_REG",
            "The CBI register lists the fund as authorised, but GLEIF says this LEI is no longer in use (retired, "
            "annulled or flagged duplicate). Reports keyed on it would identify a dead or wrong record.",
            "Confirm the fund's current LEI; if the fund merged or redomiciled, find the successor LEI and update master data."),
    "E03": ("Renewal date passed but status still ISSUED", "MEDIUM", "LEI_OPS",
            "The next renewal date is in the past yet the record still shows ISSUED, so the status is about to "
            "lapse or the issuer has not updated it.",
            "Check renewal with the issuer before the record lapses."),
    "E04": ("Renewal due within 60 days", "LOW", "LEI_OPS",
            "Preventive: renewing before the due date avoids the fund appearing as lapsed in counterparty checks.",
            "Add to the renewal calendar and notify the management company."),
    "E05": ("GLEIF entity status INACTIVE while fund is authorised", "HIGH", "FUND_REG",
            "GLEIF records the legal entity as no longer operating, which conflicts with the CBI register listing "
            "it as authorised. One of the two sources is out of date.",
            "Check for revocation, merger or termination; raise a challenge with GLEIF or correct internal records."),
    "E06": ("Legal jurisdiction is not Ireland", "MEDIUM", "FUND_REG",
            "A fund authorised by the Central Bank of Ireland should carry IE as its legal jurisdiction. A different "
            "value suggests a mis-registered LEI or a mismatch to a foreign fund with the same name.",
            "Verify the match; if correct, challenge the GLEIF record."),
    "E07": ("Matched LEI not categorised as FUND", "LOW", "STATIC",
            "GLEIF lets data users filter on entity category FUND. A fund registered as GENERAL is missed by such filters.",
            "Ask the issuer to correct the entity category."),
    "E08": ("No fund-manager relationship in GLEIF", "LOW", "STATIC",
            "GLEIF Level 2 can record the fund's main management entity (IS_FUND-MANAGED_BY). Without it, downstream "
            "users cannot link the fund to its ManCo or AIFM from LEI data alone.",
            "Ask the management company to report the fund-manager relationship at the next renewal."),
    "E09": ("Sub-fund without an umbrella relationship in GLEIF", "LOW", "STATIC",
            "The CBI register lists this fund as a sub-fund, but GLEIF has no IS_SUBFUND_OF link, so the umbrella "
            "structure is not visible in LEI data.",
            "Request the umbrella relationship at the next renewal."),
    "E10": ("Umbrella in GLEIF differs from CBI umbrella", "MEDIUM", "STATIC",
            "The sub-fund's GLEIF parent umbrella is not the umbrella the CBI register shows. Structure-based "
            "reporting (for example aggregating sub-funds to the umbrella legal entity) would roll up to the wrong parent.",
            "Confirm the umbrella with the fund administrator and correct the relationship record."),
    "E11": ("Fund manager in GLEIF differs from CBI ManCo / AIFM", "MEDIUM", "STATIC",
            "The management entity in GLEIF does not match the management company or AIFM on the CBI register, "
            "often after a ManCo change. Manager-level reporting and fee or oversight attribution would be wrong.",
            "Confirm the current ManCo or AIFM and update the relationship record. Where GLEIF names the delegate "
            "investment manager instead of the ManCo or AIFM, ask for the relationship to point at the main management entity."),
    "E18": ("Manager name differs but brand matches (rename or translation)", "LOW", "STATIC",
            "The GLEIF fund manager and the CBI ManCo / AIFM share the same brand name but the legal names differ, "
            "usually a renamed or translated legal name. Low risk, but exact-name lookups of the manager will fail.",
            "Confirm it is the same legal entity; align the legal name."),
    "E12": ("Fund name differs between CBI and GLEIF", "LOW", "STATIC",
            "Matched only after fuzzy matching or via a former name. Name drift breaks exact-name lookups used by "
            "distributors and data vendors.",
            "Confirm the legal name and update whichever source is stale."),
    "E13": ("Possible duplicate LEIs for one fund", "MEDIUM", "GOV",
            "More than one live (ISSUED or LAPSED) LEI carries the same fund name. Two identifiers for one fund "
            "fragment reporting and positions.",
            "Identify the correct LEI and ask the issuer to mark the other as duplicate."),
    "E14": ("No LEI found for an authorised fund", "MEDIUM", "FUND_REG",
            "No GLEIF record could be matched. Either the fund has no LEI (it cannot be reported as a counterparty "
            "or client where an LEI is mandatory) or its GLEIF name differs too much to match automatically.",
            "Search GLEIF manually; if none exists, request an LEI before the fund trades."),
    "E15": ("Probable match awaiting review", "LOW", "STATIC",
            "A fuzzy match scored in the review band. It is not counted as matched until a person confirms it.",
            "Review the pair and confirm or reject."),
    "E16": ("Issued Irish fund LEI not found on any CBI register", "LOW", "FUND_REG",
            "An active, issued LEI for an Irish fund that matches nothing on the current registers. It may belong "
            "to a terminated or revoked fund whose LEI was never retired, or to a fund outside these registers.",
            "Check whether the fund is still authorised; if terminated, ask for the LEI to be retired."),
    "E17": ("Source record quarantined or flagged by data-quality rules", "LOW", "GOV",
            "A CBI register row failed a data-quality rule (for example a dated row with no fund name, or the same "
            "sub-fund name twice in one umbrella). Such rows are excluded from matching or flagged for follow-up.",
            "Raise with the source owner; re-run after the next register edition."),
}

LIVE = ("ISSUED", "LAPSED", "PENDING_TRANSFER", "PENDING_ARCHIVAL")


def _manager_matches(cbi_core, gleif_name):
    g = manager_core(gleif_name or "")
    if not cbi_core or not g:
        return None
    s = max(fuzz.token_sort_ratio(cbi_core, g), fuzz.token_set_ratio(cbi_core, g))
    return s


def run(con):
    con.execute("CREATE OR REPLACE TABLE gold_rule_catalogue (rule_id VARCHAR, rule_name VARCHAR, severity VARCHAR, "
                "owner_queue VARCHAR, why_it_matters VARCHAR, next_action VARCHAR)")
    con.executemany("INSERT INTO gold_rule_catalogue VALUES (?,?,?,?,?,?)",
                    [(k, v[0], v[1], OWNERS[v[2]], v[3], v[4]) for k, v in RULES.items()])

    con.execute(f"""
    CREATE OR REPLACE TEMP VIEW mf AS
    SELECT f.fund_id, f.register, f.regime, f.structure, f.fund_name, f.umbrella_fund_id, f.umbrella_name,
           f.responsible_manager_raw, f.responsible_manager_core, f.is_internally_managed,
           m.lei, m.method, m.score, m.band, m.alt_leis, g.legal_name, g.registration_status, g.entity_status,
           g.legal_jurisdiction, g.entity_category, g.next_renewal_date, g.pool, g.other_core, f.name_core AS cbi_core,
           g.name_core AS gleif_core
    FROM silver_cbi_fund f JOIN gold_match m USING (fund_id)
    LEFT JOIN silver_gleif_entity g ON g.lei = m.lei
    """)
    asof = C.AS_OF
    con.execute(f"""
    CREATE OR REPLACE TABLE _exc AS
    SELECT 'E01' rule_id, fund_id, lei, 'status LAPSED; next renewal ' || CAST(next_renewal_date AS VARCHAR) detail
      FROM mf WHERE band='ACCEPTED' AND registration_status='LAPSED'
    UNION ALL SELECT 'E02', fund_id, lei, 'status ' || registration_status
      FROM mf WHERE band='ACCEPTED' AND registration_status IN ('RETIRED','ANNULLED','DUPLICATE','MERGED')
    UNION ALL SELECT 'E03', fund_id, lei, 'next renewal ' || CAST(next_renewal_date AS VARCHAR)
      FROM mf WHERE band='ACCEPTED' AND registration_status='ISSUED' AND next_renewal_date < DATE '{asof}'
    UNION ALL SELECT 'E04', fund_id, lei, 'next renewal ' || CAST(next_renewal_date AS VARCHAR)
      FROM mf WHERE band='ACCEPTED' AND registration_status='ISSUED'
        AND next_renewal_date BETWEEN DATE '{asof}' AND DATE '{asof}' + INTERVAL {C.RENEWAL_WARNING_DAYS} DAY
    UNION ALL SELECT 'E05', fund_id, lei, 'entity status ' || entity_status
      FROM mf WHERE band='ACCEPTED' AND entity_status='INACTIVE' AND registration_status IN {LIVE}
    UNION ALL SELECT 'E06', fund_id, lei, 'legal jurisdiction ' || legal_jurisdiction
      FROM mf WHERE band='ACCEPTED' AND legal_jurisdiction NOT LIKE 'IE%'
    UNION ALL SELECT 'E07', fund_id, lei, 'entity category ' || COALESCE(entity_category,'blank')
      FROM mf WHERE band='ACCEPTED' AND COALESCE(entity_category,'') <> 'FUND' AND registration_status IN {LIVE}
    UNION ALL SELECT 'E08', fund_id, lei, 'no IS_FUND-MANAGED_BY relationship'
      FROM mf WHERE band='ACCEPTED' AND registration_status IN {LIVE} AND entity_category='FUND'
        AND lei NOT IN (SELECT start_lei FROM silver_gleif_rel WHERE rel_type='IS_FUND-MANAGED_BY')
    UNION ALL SELECT 'E09', fund_id, lei, 'no IS_SUBFUND_OF relationship'
      FROM mf WHERE band='ACCEPTED' AND structure='SUB_FUND' AND registration_status IN {LIVE}
        AND lei NOT IN (SELECT start_lei FROM silver_gleif_rel WHERE rel_type='IS_SUBFUND_OF')
    UNION ALL SELECT 'E10', s.fund_id, s.lei, 'GLEIF umbrella ' || r.end_lei || ' vs CBI umbrella ' || u.lei
      FROM mf s JOIN gold_match u ON u.fund_id = s.umbrella_fund_id AND u.band='ACCEPTED'
      JOIN silver_gleif_rel r ON r.start_lei = s.lei AND r.rel_type='IS_SUBFUND_OF' AND r.rel_status='ACTIVE'
      WHERE s.band='ACCEPTED' AND s.structure='SUB_FUND' AND r.end_lei <> u.lei
    UNION ALL SELECT 'E12', fund_id, lei, 'CBI: ' || fund_name || ' | GLEIF: ' || legal_name
      FROM mf WHERE band='ACCEPTED' AND (method LIKE 'FUZZY%' OR (other_core = cbi_core AND gleif_core <> cbi_core))
    UNION ALL SELECT 'E13', fund_id, lei, 'other live LEIs: ' || alt_leis
      FROM mf WHERE band='ACCEPTED' AND alt_leis <> ''
        AND EXISTS (SELECT 1 FROM silver_gleif_entity g2 WHERE list_contains(string_split(mf.alt_leis, ';'), g2.lei)
                    AND g2.registration_status IN ('ISSUED','LAPSED'))
    UNION ALL SELECT 'E14', fund_id, NULL, 'no GLEIF record matched' FROM mf WHERE band='UNMATCHED'
    UNION ALL SELECT 'E15', fund_id, lei, 'score ' || CAST(score AS VARCHAR) || ' | GLEIF: ' || legal_name
      FROM mf WHERE band='PROBABLE_REVIEW'
    UNION ALL SELECT 'E16', NULL, g.lei, g.legal_name FROM silver_gleif_entity g
      WHERE g.pool='IE_FUND' AND g.registration_status='ISSUED' AND g.entity_status='ACTIVE'
        AND g.legal_jurisdiction LIKE 'IE%'
        AND g.lei NOT IN (SELECT lei FROM gold_match WHERE lei IS NOT NULL)
    UNION ALL SELECT 'E17', d.record_id, NULL, d.rule_id || ': ' || d.rule_desc
      FROM silver_dq_findings d WHERE d.source='CBI' AND d.rule_id IN ('Q01','W01')
    """)
    # E11 manager mismatch (python fuzzy)
    rows = con.execute(f"""
      SELECT mf.fund_id, mf.lei, mf.responsible_manager_core, mf.responsible_manager_raw, mg.lei, mg.legal_name
      FROM mf JOIN silver_gleif_rel r ON r.start_lei = mf.lei AND r.rel_type='IS_FUND-MANAGED_BY' AND r.rel_status='ACTIVE'
      JOIN silver_gleif_entity mg ON mg.lei = r.end_lei
      WHERE mf.band='ACCEPTED' AND mf.registration_status IN {LIVE} AND NOT mf.is_internally_managed
        AND mf.responsible_manager_core IS NOT NULL""").fetchall()
    mm, checked = [], []
    for fid, lei, cbi_core, cbi_raw, mlei, mname in rows:
        s = _manager_matches(cbi_core, mname)
        checked.append((fid, lei, cbi_raw, mlei, mname, s))
        if s is not None and s < C.MANAGER_MATCH:
            same_brand = cbi_core.split()[0] == manager_core(mname).split()[0] if manager_core(mname) else False
            mm.append(("E18" if same_brand else "E11", fid, lei,
                       f"CBI: {cbi_raw} | GLEIF: {mname} (score {round(s)})"))
    con.execute("CREATE OR REPLACE TABLE gold_manager_check (fund_id VARCHAR, lei VARCHAR, cbi_manager VARCHAR, "
                "gleif_manager_lei VARCHAR, gleif_manager VARCHAR, score DOUBLE)")
    if checked:
        con.executemany("INSERT INTO gold_manager_check VALUES (?,?,?,?,?,?)", checked)
    if mm:
        con.executemany("INSERT INTO _exc VALUES (?,?,?,?)", mm)

    con.execute("""
    CREATE OR REPLACE TABLE gold_exception AS
    SELECT 'X' || lpad(CAST(ROW_NUMBER() OVER (ORDER BY CASE c.severity WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                 e.rule_id, COALESCE(e.fund_id, ''), COALESCE(e.lei, '')) AS VARCHAR), 6, '0') AS exception_id,
           e.rule_id, c.rule_name, c.severity, c.owner_queue,
           e.fund_id, e.lei, f.register, f.structure, f.fund_name, f.umbrella_name,
           f.responsible_manager_raw AS cbi_manager, g.legal_name AS gleif_legal_name,
           g.registration_status, e.detail, c.next_action, 'OPEN' AS status, CAST(? AS DATE) AS as_of
    FROM _exc e JOIN gold_rule_catalogue c USING (rule_id)
    LEFT JOIN silver_cbi_fund_all f ON f.fund_id = e.fund_id
    LEFT JOIN silver_gleif_entity g ON g.lei = e.lei
    """, [C.AS_OF])
