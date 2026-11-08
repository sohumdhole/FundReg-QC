"""Step 1: bronze / silver layers in DuckDB with data-quality rules and quarantine."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime

import duckdb
import pdfplumber

from . import config as C
from .extract_cbi import (parse_register, record_rows, totals_from_text,
                          parse_manager_register)
from .normalise import name_key, name_core, split_gleif_name, manager_core, lei_check_digits_ok

RUN_DATE = re.compile(r"Run [Dd]ate:\s*(\d{1,2} \w+ \d{4})")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def register_as_at(path):
    with pdfplumber.open(path) as pdf:
        text = pdf.pages[-1].extract_text() or ""
        first = pdf.pages[0].extract_text() or ""
    m = RUN_DATE.search(text) or RUN_DATE.search(first)
    if not m:
        m = re.search(r"as at (\d{1,2} \w+ \d{4})", first)
    return datetime.strptime(m.group(1), "%d %B %Y").date().isoformat() if m else None


def build_manifest():
    files = []
    for reg, fn in {**C.CBI_REGISTER_FILES, **C.CBI_MANAGER_FILES}.items():
        p = C.RAW_CBI / fn
        with pdfplumber.open(p) as pdf:
            pages = len(pdf.pages)
        files.append({"source": "Central Bank of Ireland register", "register": reg, "file": fn,
                      "url": C.SOURCE_URLS["cbi_registers_page"], "edition_as_at": register_as_at(p),
                      "pages": pages, "bytes": p.stat().st_size, "sha256": sha256(p),
                      "format": "PDF (text layer, no OCR needed)"})
    gm = json.loads((C.RAW_GLEIF / "manifest.json").read_text())
    for s in gm["sources"]:
        files.append({"source": "GLEIF Golden Copy (downloaded and filtered locally)", "register": s["kind"],
                      "file": s["file"], "url": s["final_url"], "edition_as_at": s["file"][:13],
                      "bytes": s["bytes"], "sha256": s["sha256"], "licence": gm["licence"]})
    for k, o in gm["outputs"].items():
        for f in o["files"]:
            p = C.RAW_GLEIF / f["file"]
            files.append({"source": "GLEIF filtered extract", "register": k, "file": f["file"],
                          "rows": o["rows"], "bytes": p.stat().st_size, "sha256": sha256(p),
                          "sha256_matches_filter_manifest": sha256(p) == f["sha256"]})
    return {"generated_by": "fundreg_qc.data_layer", "filter_rules": gm["filter_rules"],
            "gleif_level1_rows_scanned": gm["level1_rows_scanned"],
            "gleif_rr_rows_scanned": gm["rr_rows_scanned"], "files": files}


# ------------------------------------------------------------------ bronze
def load_bronze(con):
    fund_rows, totals, unparsed = [], [], []
    for reg, fn in C.CBI_REGISTER_FILES.items():
        regime = "UCITS" if reg == "UCITS" else "AIF"
        recs, tt, un = parse_register(str(C.RAW_CBI / fn), reg, regime)
        rows = record_rows(recs)
        for r in rows:
            r["source_file"] = fn
        fund_rows += rows
        t = totals_from_text(tt)
        t.update({"register": reg, "as_at": register_as_at(C.RAW_CBI / fn)})
        totals.append(t)
        for u in un:
            u["register"] = reg
        unparsed += un
    cols = ["register", "regime", "level", "seq", "umbrella_seq", "name_raw", "name_full_raw", "name_clean",
            "flags", "auth_date_raw", "auth_date", "manager_raw", "aifm_raw", "internally_managed",
            "depositary_raw", "extra_name_lines", "date_notes", "page", "line_top", "source_file"]
    con.execute("CREATE OR REPLACE TABLE bronze_cbi_fund (" + ", ".join(
        f"{c} VARCHAR" for c in cols) + ")")
    con.executemany(f"INSERT INTO bronze_cbi_fund VALUES ({','.join('?' * len(cols))})",
                    [[None if r.get(c) is None else str(r.get(c)) for c in cols] for r in fund_rows])
    con.execute("CREATE OR REPLACE TABLE bronze_cbi_totals (register VARCHAR, as_at VARCHAR, "
                "printed_top_level INTEGER, printed_standalone_plus_subfunds INTEGER)")
    con.executemany("INSERT INTO bronze_cbi_totals VALUES (?,?,?,?)",
                    [[t["register"], t["as_at"], t.get("printed_top_level"),
                      t.get("printed_standalone_plus_subfunds")] for t in totals])
    con.execute("CREATE OR REPLACE TABLE bronze_cbi_unparsed (register VARCHAR, page INTEGER, text VARCHAR, reason VARCHAR)")
    con.executemany("INSERT INTO bronze_cbi_unparsed VALUES (?,?,?,?)",
                    [[u["register"], u["page"], u["text"], u["reason"]] for u in unparsed])
    mgr_rows, mgr_tot = [], []
    for kind, fn in C.CBI_MANAGER_FILES.items():
        rows, tt = parse_manager_register(str(C.RAW_CBI / fn), kind)
        mgr_rows += rows
        for t in tt:
            m = re.search(r":\s*(\d+)", t)
            mgr_tot.append([kind, int(m.group(1)) if m else None, register_as_at(C.RAW_CBI / fn)])
    con.execute("CREATE OR REPLACE TABLE bronze_cbi_manager (reference VARCHAR, name_raw VARCHAR, page INTEGER, kind VARCHAR)")
    con.executemany("INSERT INTO bronze_cbi_manager VALUES (?,?,?,?)",
                    [[r["reference"], r["name_raw"], r["page"], r["kind"]] for r in mgr_rows])
    con.execute("CREATE OR REPLACE TABLE bronze_cbi_manager_totals (kind VARCHAR, printed_total INTEGER, as_at VARCHAR)")
    con.executemany("INSERT INTO bronze_cbi_manager_totals VALUES (?,?,?)", mgr_tot)

    # GLEIF extracts, all columns as text
    pools = [("IE_FUND", "ie_funds_l1.csv"), ("IE_OTHER", "ie_other_l1_slim.csv"),
             ("COUNTERPARTY", "rr_counterparties_l1.csv")]
    con.execute("CREATE OR REPLACE TABLE bronze_gleif_l1 AS SELECT * FROM read_csv_auto(?, all_varchar=true, header=true) LIMIT 0",
                [str(C.RAW_GLEIF / "ie_funds_l1.csv")])
    con.execute("ALTER TABLE bronze_gleif_l1 ADD COLUMN pool VARCHAR")
    for pool, fn in pools:
        con.execute(f"INSERT INTO bronze_gleif_l1 BY NAME SELECT *, '{pool}' AS pool FROM "
                    "read_csv_auto(?, all_varchar=true, header=true)", [str(C.RAW_GLEIF / fn)])
    con.execute("CREATE OR REPLACE TABLE bronze_gleif_rr AS SELECT * FROM read_csv_auto(?, all_varchar=true, header=true)",
                [str(C.RAW_GLEIF / "ie_fund_rr.csv")])


# ------------------------------------------------------------------ silver
def build_silver(con):
    con.execute("""
    CREATE OR REPLACE TABLE silver_cbi_fund_stage AS
    WITH b AS (
      SELECT *, CAST(seq AS INTEGER) AS seq_i, CAST(umbrella_seq AS INTEGER) AS umb_i FROM bronze_cbi_fund),
    subs AS (SELECT register, umb_i, COUNT(*) AS n_sub FROM b WHERE level='SUB' GROUP BY 1,2)
    SELECT b.register || '-' || lpad(CAST(b.seq_i AS VARCHAR), 5, '0') AS fund_id,
           b.register, b.regime,
           CASE WHEN b.level='SUB' THEN 'SUB_FUND'
                WHEN COALESCE(s.n_sub,0) > 0 THEN 'UMBRELLA' ELSE 'STANDALONE' END AS structure,
           CASE WHEN b.level='SUB' THEN b.register || '-' || lpad(CAST(b.umb_i AS VARCHAR), 5, '0') END AS umbrella_fund_id,
           b.name_clean AS fund_name, b.flags, CAST(b.auth_date AS DATE) AS auth_date,
           b.manager_raw, b.aifm_raw, b.internally_managed, b.depositary_raw, b.date_notes,
           CAST(b.page AS INTEGER) AS source_page, b.source_file, b.seq_i, b.umb_i,
           COALESCE(s.n_sub, 0) AS n_sub_funds
    FROM b LEFT JOIN subs s ON s.register=b.register AND s.umb_i=b.seq_i AND b.level='TOP'
    """)
    rows = con.execute("SELECT fund_id, fund_name, manager_raw, aifm_raw, internally_managed, structure FROM silver_cbi_fund_stage").fetchall()
    keys = []
    for fid, nm, mco, aifm, internal, structure in rows:
        keys.append((fid, name_key(nm or ""), name_core(nm or ""),
                          manager_core(mco) if structure != "SUB_FUND" and mco else None,
                     manager_core(aifm) if structure != "SUB_FUND" and aifm else None))
    con.execute("CREATE OR REPLACE TEMP TABLE _cbi_keys (fund_id VARCHAR, name_key VARCHAR, name_core VARCHAR, manco_core VARCHAR, aifm_core VARCHAR)")
    con.executemany("INSERT INTO _cbi_keys VALUES (?,?,?,?,?)", keys)
    con.execute("""
    CREATE OR REPLACE TABLE silver_cbi_fund_all AS
    SELECT f.*, k.name_key, k.name_core,
           u.fund_name AS umbrella_name, uk.name_core AS umbrella_core,
           -- manager responsible: UCITS -> management company; AIF -> AIFM, or the fund itself if internally managed
           trim(regexp_replace(
             CASE WHEN f.regime='UCITS' THEN NULLIF(COALESCE(u.manager_raw, f.manager_raw),'')
                  ELSE COALESCE(NULLIF(COALESCE(u.aifm_raw, f.aifm_raw),''), NULLIF(COALESCE(u.manager_raw, f.manager_raw),''))
             END, '[#\u00a7\u2020\u00d8*\u2021$]', '', 'g')) AS responsible_manager_raw,
           CASE WHEN f.regime='UCITS' THEN NULLIF(COALESCE(uk.manco_core, k.manco_core),'')
                ELSE COALESCE(NULLIF(COALESCE(uk.aifm_core, k.aifm_core),''), NULLIF(COALESCE(uk.manco_core, k.manco_core),''))
           END AS responsible_manager_core,
           CASE WHEN f.structure='SUB_FUND' THEN regexp_matches(COALESCE(u.internally_managed,''), '^Yes')
                ELSE regexp_matches(COALESCE(f.internally_managed,''), '^Yes') END AS is_internally_managed,
           CASE WHEN f.structure='SUB_FUND' THEN u.flags ELSE f.flags END LIKE '%#%' AND f.regime='AIF' AS qiaif_flag,
           f.flags LIKE '%§%' AS umbrella_flag,
           f.flags LIKE '%†%' AS closed_ended_flag,
           f.flags LIKE '%Ø%' AS limited_liquidity_flag,
           CASE WHEN f.structure='SUB_FUND' THEN u.auth_date END AS umbrella_auth_date,
           t.as_at AS register_as_at
    FROM silver_cbi_fund_stage f
    JOIN _cbi_keys k USING (fund_id)
    LEFT JOIN silver_cbi_fund_stage u ON u.fund_id = f.umbrella_fund_id
    LEFT JOIN _cbi_keys uk ON uk.fund_id = f.umbrella_fund_id
    LEFT JOIN bronze_cbi_totals t ON t.register = f.register
    """)
    # ---- data quality rules
    con.execute("""
    CREATE OR REPLACE TABLE silver_dq_findings AS
    SELECT fund_id AS record_id, 'CBI' AS source, 'Q01' AS rule_id, 'QUARANTINE' AS action,
           'Fund name missing in source row' AS rule_desc, 'page ' || source_page AS detail
      FROM silver_cbi_fund_all WHERE COALESCE(fund_name,'')=''
    UNION ALL
    SELECT fund_id, 'CBI', 'Q02', 'QUARANTINE', 'Authorisation date after register as-at date', CAST(auth_date AS VARCHAR)
      FROM silver_cbi_fund_all WHERE auth_date > CAST(register_as_at AS DATE)
    UNION ALL
    SELECT fund_id, 'CBI', 'W01', 'FLAG', 'Duplicate fund name within the same umbrella', fund_name
      FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY register, umbrella_fund_id, name_key ORDER BY seq_i) rn
              FROM silver_cbi_fund_all WHERE structure='SUB_FUND' AND COALESCE(fund_name,'')<>'') WHERE rn > 1
    UNION ALL
    SELECT fund_id, 'CBI', 'W02', 'FLAG', 'Sub-fund authorised before its umbrella', CAST(auth_date AS VARCHAR) || ' < ' || CAST(umbrella_auth_date AS VARCHAR)
      FROM silver_cbi_fund_all WHERE structure='SUB_FUND' AND auth_date < umbrella_auth_date
    UNION ALL
    SELECT fund_id, 'CBI', 'W03', 'FLAG', 'No management company / AIFM printed and not internally managed', fund_name
      FROM silver_cbi_fund_all WHERE structure<>'SUB_FUND' AND responsible_manager_raw IS NULL AND NOT is_internally_managed
    """)
    con.execute("""CREATE OR REPLACE TABLE silver_cbi_fund AS SELECT * FROM silver_cbi_fund_all
                   WHERE fund_id NOT IN (SELECT record_id FROM silver_dq_findings WHERE source='CBI' AND action='QUARANTINE')""")
    con.execute("""CREATE OR REPLACE TABLE silver_cbi_quarantine AS SELECT f.*, d.rule_id, d.rule_desc FROM silver_cbi_fund_all f
                   JOIN silver_dq_findings d ON d.record_id=f.fund_id AND d.action='QUARANTINE'""")

    # ---- managers
    mrows = con.execute("SELECT reference, name_raw, kind FROM bronze_cbi_manager").fetchall()
    con.execute("CREATE OR REPLACE TABLE silver_cbi_manager (reference VARCHAR, manager_name VARCHAR, kind VARCHAR, manager_core VARCHAR)")
    con.executemany("INSERT INTO silver_cbi_manager VALUES (?,?,?,?)",
                    [(r, n, k, manager_core(n)) for r, n, k in mrows])

    # ---- GLEIF entities
    g = con.execute("""
      SELECT LEI, "Entity.LegalName", "Entity.OtherEntityNames.OtherEntityName.1", pool
      FROM bronze_gleif_l1""").fetchall()
    gk = []
    for lei, nm, other, pool in g:
        up, sp = split_gleif_name(nm or "")
        gk.append((lei, pool, name_key(nm or ""), name_core(nm or ""), up, sp,
                   name_core(other) if other else None, lei_check_digits_ok(lei or "")))
    con.execute("CREATE OR REPLACE TEMP TABLE _g_keys (lei VARCHAR, pool VARCHAR, name_key VARCHAR, name_core VARCHAR, "
                "umb_part VARCHAR, sub_part VARCHAR, other_core VARCHAR, lei_valid BOOLEAN)")
    con.executemany("INSERT INTO _g_keys VALUES (?,?,?,?,?,?,?,?)", gk)
    con.execute("""
    CREATE OR REPLACE TABLE silver_gleif_entity_all AS
    SELECT b.LEI AS lei, replace(b."Entity.LegalName", chr(8212), '-') AS legal_name, b."Entity.EntityCategory" AS entity_category,
           b."Registration.RegistrationStatus" AS registration_status, b."Entity.EntityStatus" AS entity_status,
           b."Entity.LegalJurisdiction" AS legal_jurisdiction, b."Entity.LegalAddress.Country" AS legal_country,
           b."Entity.LegalForm.EntityLegalFormCode" AS legal_form_code,
           TRY_CAST(substr(b."Registration.NextRenewalDate",1,10) AS DATE) AS next_renewal_date,
           TRY_CAST(substr(b."Registration.InitialRegistrationDate",1,10) AS DATE) AS initial_registration_date,
           TRY_CAST(substr(b."Registration.LastUpdateDate",1,10) AS DATE) AS last_update_date,
           b."Registration.ManagingLOU" AS managing_lou, b."Registration.ValidationSources" AS validation_sources,
           b."Entity.RegistrationAuthority.RegistrationAuthorityID" AS ra_id,
           b."Entity.RegistrationAuthority.RegistrationAuthorityEntityID" AS ra_entity_id,
           b.pool, k.name_key, k.name_core, k.umb_part, k.sub_part, k.other_core, k.lei_valid,
           ROW_NUMBER() OVER (PARTITION BY b.LEI ORDER BY CASE b.pool WHEN 'IE_FUND' THEN 1 WHEN 'IE_OTHER' THEN 2 ELSE 3 END) AS pool_rank
    FROM bronze_gleif_l1 b JOIN _g_keys k ON k.lei=b.LEI AND k.pool=b.pool
    """)
    con.execute("""
    INSERT INTO silver_dq_findings
    SELECT lei, 'GLEIF', 'Q10', 'QUARANTINE', 'LEI fails ISO 17442 check digits', lei FROM silver_gleif_entity_all WHERE NOT lei_valid
    UNION ALL
    SELECT lei, 'GLEIF', 'Q11', 'QUARANTINE', 'Legal name missing', lei FROM silver_gleif_entity_all WHERE COALESCE(legal_name,'')=''
    UNION ALL
    SELECT lei, 'GLEIF', 'W10', 'FLAG', 'LEI present in more than one extract pool (deduplicated)', pool
      FROM silver_gleif_entity_all WHERE pool_rank > 1
    """)
    con.execute("""CREATE OR REPLACE TABLE silver_gleif_entity AS SELECT * EXCLUDE (pool_rank) FROM silver_gleif_entity_all
                   WHERE pool_rank = 1 AND lei NOT IN (SELECT record_id FROM silver_dq_findings WHERE source='GLEIF' AND action='QUARANTINE')""")
    con.execute("""
    CREATE OR REPLACE TABLE silver_gleif_rel AS
    SELECT "Relationship.StartNode.NodeID" AS start_lei, "Relationship.EndNode.NodeID" AS end_lei,
           "Relationship.EndNode.NodeIDType" AS end_type, "Relationship.RelationshipType" AS rel_type,
           "Relationship.RelationshipStatus" AS rel_status, "Registration.RegistrationStatus" AS rr_registration_status,
           TRY_CAST(substr("Relationship.Period.1.startDate",1,10) AS DATE) AS rel_start
    FROM bronze_gleif_rr
    """)


def run(con):
    load_bronze(con)
    build_silver(con)
    manifest = build_manifest()
    (C.OUT / "data_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
