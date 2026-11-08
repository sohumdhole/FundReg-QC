"""The audit record: every number quoted in the docs is computed here from the database
and written once to outputs/audit_record.json. Docs are rendered from this record only."""
from __future__ import annotations

import csv
import json
import statistics
from collections import Counter

from . import config as C


def q1(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()[0]


def pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def review_metrics(con):
    path = C.REVIEW / "evaluation_sample_labelled.csv"
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    out = {}
    still_valid = 0
    for r in rows:
        cur = con.execute("SELECT lei FROM gold_match WHERE fund_id=?", [r["fund_id"]]).fetchone()
        if cur and cur[0] == r["lei"]:
            still_valid += 1
    for stratum in sorted({r["stratum"] for r in rows}):
        s = [r for r in rows if r["stratum"] == stratum]
        c = Counter(r["label"] for r in s)
        out[stratum] = {"n": len(s), "correct": c["CORRECT"], "incorrect": c["INCORRECT"],
                        "uncertain": c["UNCERTAIN"],
                        "precision_strict_pct": pct(c["CORRECT"], len(s)),
                        "precision_lenient_pct": pct(c["CORRECT"] + c["UNCERTAIN"], len(s))}
    exact = [r for r in rows if r["stratum"].startswith("EXACT")]
    out["EXACT_ALL"] = {"n": len(exact), "correct": sum(r["label"] == "CORRECT" for r in exact)}
    out["EXACT_ALL"]["precision_strict_pct"] = pct(out["EXACT_ALL"]["correct"], len(exact))
    # rule of three: with 0 errors in n, the 95% upper bound on the error rate is about 3/n
    errs = len(exact) - out["EXACT_ALL"]["correct"]
    out["EXACT_ALL"]["error_rate_95_upper_pct"] = round(300.0 / len(exact), 1) if errs == 0 else None
    out["sample_rows"] = len(rows)
    out["sample_rows_still_matching_current_run"] = still_valid
    out["seed"] = C.REVIEW_SAMPLE_SEED
    return out


def build(con):
    a = {"as_of": C.AS_OF, "thresholds": {"fuzzy_accept": C.FUZZY_ACCEPT, "fuzzy_review": C.FUZZY_REVIEW,
                                          "manager_match": C.MANAGER_MATCH,
                                          "renewal_warning_days": C.RENEWAL_WARNING_DAYS}}
    # ---------------- data layer
    regs = []
    for reg, as_at, p_top, p_sf in con.execute("SELECT register, as_at, printed_top_level, printed_standalone_plus_subfunds FROM bronze_cbi_totals ORDER BY register").fetchall():
        top = q1(con, "SELECT COUNT(*) FROM silver_cbi_fund_all WHERE register=? AND structure<>'SUB_FUND'", [reg])
        sf = q1(con, "SELECT COUNT(*) FROM silver_cbi_fund_all WHERE register=? AND structure<>'UMBRELLA'", [reg])
        rows = q1(con, "SELECT COUNT(*) FROM silver_cbi_fund_all WHERE register=?", [reg])
        regs.append({"register": reg, "label": C.REGISTER_LABEL[reg], "as_at": as_at, "rows_parsed": rows,
                     "printed_top_level": p_top, "parsed_top_level": top, "top_level_variance": top - p_top,
                     "printed_standalone_plus_subfunds": p_sf, "parsed_standalone_plus_subfunds": sf,
                     "standalone_plus_subfunds_variance": sf - p_sf})
    a["registers"] = regs
    a["cbi_rows_parsed"] = sum(r["rows_parsed"] for r in regs)
    a["cbi_printed_top_level_total"] = sum(r["printed_top_level"] for r in regs)
    a["cbi_printed_sf_total"] = sum(r["printed_standalone_plus_subfunds"] for r in regs)
    a["control_totals_checked"] = 2 * len(regs)
    a["control_totals_exact"] = sum((r["top_level_variance"] == 0) + (r["standalone_plus_subfunds_variance"] == 0) for r in regs)
    a["control_total_abs_variance"] = sum(abs(r["top_level_variance"]) + abs(r["standalone_plus_subfunds_variance"]) for r in regs)
    a["cbi_quarantined"] = q1(con, "SELECT COUNT(*) FROM silver_cbi_quarantine")
    a["cbi_funds_in_scope"] = q1(con, "SELECT COUNT(*) FROM silver_cbi_fund")
    a["dq_findings"] = {r[0]: {"action": r[1], "desc": r[2], "n": r[3]} for r in con.execute(
        "SELECT rule_id, action, rule_desc, COUNT(*) FROM silver_dq_findings GROUP BY 1,2,3 ORDER BY 1").fetchall()}
    a["managers"] = {r[0]: {"printed": r[1], "parsed": q1(con, "SELECT COUNT(*) FROM bronze_cbi_manager WHERE kind=?", [r[0]]), "as_at": r[2]}
                     for r in con.execute("SELECT kind, printed_total, as_at FROM bronze_cbi_manager_totals").fetchall()}
    a["pdf_pages_without_header"] = q1(con, "SELECT COUNT(*) FROM bronze_cbi_unparsed WHERE reason LIKE 'page without header%'")
    gm = json.loads((C.RAW_GLEIF / "manifest.json").read_text())
    a["gleif"] = {"golden_copy_file": gm["sources"][0]["file"], "rr_file": gm["sources"][1]["file"],
                  "level1_rows_scanned": gm["level1_rows_scanned"], "rr_rows_scanned": gm["rr_rows_scanned"],
                  "ie_fund_leis": gm["outputs"]["ie_funds_l1"]["rows"], "ie_other_entities": gm["outputs"]["ie_other_l1_slim"]["rows"],
                  "fund_relationships": gm["outputs"]["ie_fund_rr"]["rows"],
                  "counterparties": gm["outputs"]["rr_counterparties_l1"]["rows"]}
    a["gleif"]["ie_fund_status"] = dict(con.execute("SELECT registration_status, COUNT(*) FROM silver_gleif_entity WHERE pool='IE_FUND' GROUP BY 1 ORDER BY 2 DESC").fetchall())
    a["gleif"]["relationship_types"] = dict(con.execute("SELECT rel_type, COUNT(*) FROM silver_gleif_rel GROUP BY 1 ORDER BY 2 DESC").fetchall())
    a["gleif"]["quarantined"] = q1(con, "SELECT COUNT(*) FROM silver_dq_findings WHERE source='GLEIF' AND action='QUARANTINE'")

    # ---------------- matching
    n = a["cbi_funds_in_scope"]
    acc = q1(con, "SELECT COUNT(*) FROM gold_match WHERE band='ACCEPTED'")
    prob = q1(con, "SELECT COUNT(*) FROM gold_match WHERE band='PROBABLE_REVIEW'")
    unm = q1(con, "SELECT COUNT(*) FROM gold_match WHERE band='UNMATCHED'")
    a["match"] = {"funds": n, "accepted": acc, "probable": prob, "unmatched": unm, "accepted_pct": pct(acc, n),
                  "methods": dict(con.execute("SELECT method, COUNT(*) FROM gold_match WHERE band='ACCEPTED' GROUP BY 1 ORDER BY 2 DESC").fetchall()),
                  "lei_clashes_resolved": q1(con, "SELECT COUNT(*) FROM gold_match_clash"),
                  "guard_rejects": dict(con.execute("SELECT guard, COUNT(*) FROM gold_match_guard_reject GROUP BY 1").fetchall())}
    fuzzy_acc = q1(con, "SELECT COUNT(*) FROM gold_match WHERE band='ACCEPTED' AND method LIKE 'FUZZY%'")
    a["match"]["fuzzy_accepted"] = fuzzy_acc
    a["match"]["exact_accepted"] = acc - fuzzy_acc
    by = []
    for reg, st, tot, ac, pr, un in con.execute("""
        SELECT f.register, f.structure, COUNT(*), SUM((m.band='ACCEPTED')::INT), SUM((m.band='PROBABLE_REVIEW')::INT), SUM((m.band='UNMATCHED')::INT)
        FROM silver_cbi_fund f JOIN gold_match m USING (fund_id) GROUP BY 1,2 ORDER BY 1,2""").fetchall():
        by.append({"register": reg, "structure": st, "funds": tot, "accepted": ac, "probable": pr, "unmatched": un,
                   "accepted_pct": pct(ac, tot)})
    a["match_by_register_structure"] = by
    a["match_by_register"] = [{"register": r, "label": C.REGISTER_LABEL[r], "funds": t, "accepted": ac, "accepted_pct": pct(ac, t)}
                              for r, t, ac in con.execute("""SELECT f.register, COUNT(*), SUM((m.band='ACCEPTED')::INT)
                                 FROM silver_cbi_fund f JOIN gold_match m USING (fund_id) GROUP BY 1 ORDER BY 1""").fetchall()]
    a["match_by_structure"] = {r[0]: {"funds": r[1], "accepted": r[2], "accepted_pct": pct(r[2], r[1])} for r in con.execute(
        """SELECT f.structure, COUNT(*), SUM((m.band='ACCEPTED')::INT) FROM silver_cbi_fund f JOIN gold_match m USING (fund_id) GROUP BY 1""").fetchall()}
    a["match_by_regime"] = {r[0]: {"funds": r[1], "accepted": r[2], "accepted_pct": pct(r[2], r[1])} for r in con.execute(
        """SELECT f.regime, COUNT(*), SUM((m.band='ACCEPTED')::INT) FROM silver_cbi_fund f JOIN gold_match m USING (fund_id) GROUP BY 1""").fetchall()}
    a["match"]["unmatched_subfunds"] = q1(con, """SELECT COUNT(*) FROM gold_match m JOIN silver_cbi_fund f USING (fund_id)
                                               WHERE m.band='UNMATCHED' AND f.structure='SUB_FUND'""")
    a["match"]["unmatched_subfunds_with_matched_umbrella"] = q1(con, """SELECT COUNT(*) FROM gold_match m JOIN silver_cbi_fund f USING (fund_id)
        JOIN gold_match u ON u.fund_id = f.umbrella_fund_id AND u.band='ACCEPTED'
        WHERE m.band='UNMATCHED' AND f.structure='SUB_FUND'""")
    a["match"]["unmatched_subfunds_with_matched_umbrella_pct"] = pct(a["match"]["unmatched_subfunds_with_matched_umbrella"], unm)
    a["review"] = review_metrics(con)
    # status of matched LEIs
    a["matched_lei_status"] = dict(con.execute("""SELECT g.registration_status, COUNT(*) FROM gold_match m JOIN silver_gleif_entity g USING (lei)
                                                  WHERE m.band='ACCEPTED' GROUP BY 1 ORDER BY 2 DESC""").fetchall())

    # ---------------- exceptions
    a["exceptions"] = {"total": q1(con, "SELECT COUNT(*) FROM gold_exception"),
                       "funds_with_exception": q1(con, "SELECT COUNT(DISTINCT fund_id) FROM gold_exception WHERE fund_id IS NOT NULL"),
                       "by_severity": dict(con.execute("SELECT severity, COUNT(*) FROM gold_exception GROUP BY 1").fetchall()),
                       "by_owner": dict(con.execute("SELECT owner_queue, COUNT(*) FROM gold_exception GROUP BY 1 ORDER BY 2 DESC").fetchall()),
                       "by_rule": {r[0]: {"name": r[1], "severity": r[2], "owner": r[3], "n": r[4]} for r in con.execute(
                           """SELECT c.rule_id, c.rule_name, c.severity, c.owner_queue, COUNT(e.exception_id)
                              FROM gold_rule_catalogue c LEFT JOIN gold_exception e USING (rule_id) GROUP BY 1,2,3,4 ORDER BY 1""").fetchall()}}
    a["exceptions"]["matched_issued_pct"] = pct(a["matched_lei_status"].get("ISSUED", 0), acc)
    a["exceptions"]["matched_lapsed_pct"] = pct(a["matched_lei_status"].get("LAPSED", 0), acc)
    a["manager_check"] = {"pairs_checked": q1(con, "SELECT COUNT(*) FROM gold_manager_check"),
                          "below_threshold": q1(con, f"SELECT COUNT(*) FROM gold_manager_check WHERE score < {C.MANAGER_MATCH}")}
    a["manager_check"]["agree_pct"] = pct(a["manager_check"]["pairs_checked"] - a["manager_check"]["below_threshold"],
                                          a["manager_check"]["pairs_checked"])
    issued_matched = q1(con, """SELECT COUNT(*) FROM gold_match m JOIN silver_gleif_entity g USING (lei)
                               WHERE m.band='ACCEPTED' AND g.registration_status IN ('ISSUED','LAPSED','PENDING_TRANSFER','PENDING_ARCHIVAL') AND g.entity_category='FUND'""")
    a["relationships"] = {"live_matched_funds": issued_matched,
                          "with_manager_rel": issued_matched - a["exceptions"]["by_rule"]["E08"]["n"]}
    a["relationships"]["with_manager_rel_pct"] = pct(a["relationships"]["with_manager_rel"], issued_matched)

    # ---------------- market structure
    def cnt(where):
        return q1(con, f"SELECT COUNT(*) FROM gold_fund WHERE {where}")
    m = {"umbrellas": cnt("structure='UMBRELLA'"), "standalone": cnt("structure='STANDALONE'"),
         "sub_funds": cnt("structure='SUB_FUND'")}
    m["portfolios"] = m["standalone"] + m["sub_funds"]
    m["ucits_portfolios"] = cnt("regime='UCITS' AND structure<>'UMBRELLA'")
    m["aif_portfolios"] = cnt("regime='AIF' AND structure<>'UMBRELLA'")
    m["ucits_portfolio_pct"] = pct(m["ucits_portfolios"], m["portfolios"])
    m["ucits_top_level"] = cnt("regime='UCITS' AND structure<>'SUB_FUND'")
    m["aif_top_level"] = cnt("regime='AIF' AND structure<>'SUB_FUND'")
    m["sub_fund_share_pct"] = pct(m["sub_funds"], m["portfolios"])
    m["umbrella_share_of_top_level_pct"] = pct(m["umbrellas"], m["umbrellas"] + m["standalone"])
    subs = [r[0] for r in con.execute("""SELECT COUNT(*) FROM gold_fund WHERE structure='SUB_FUND' GROUP BY umbrella_fund_id""").fetchall()]
    m["subfunds_per_umbrella_median"] = statistics.median(subs)
    m["subfunds_per_umbrella_mean"] = round(statistics.mean(subs), 1)
    m["subfunds_per_umbrella_max"] = max(subs)
    m["largest_umbrella"] = con.execute("""SELECT u.fund_name, COUNT(*) n FROM gold_fund s JOIN gold_fund u ON u.fund_id=s.umbrella_fund_id
                                           GROUP BY 1 ORDER BY n DESC LIMIT 1""").fetchone()[0]
    m["aif_qiaif_flag_pct"] = pct(cnt("regime='AIF' AND structure<>'UMBRELLA' AND qiaif_flag"), m["aif_portfolios"])
    for regime in ("UCITS", "AIF"):
        rows = con.execute("""SELECT manager_name, n, share FROM gold_manager_concentration WHERE regime=? ORDER BY rank""", [regime]).fetchall()
        named = [r for r in rows if r[0] not in ("Internally managed", "No ManCo / AIFM printed")]
        m[f"{regime.lower()}_managers"] = len(named)
        m[f"{regime.lower()}_top5_share_pct"] = round(100 * sum(r[2] for r in named[:5]), 1)
        m[f"{regime.lower()}_top10_share_pct"] = round(100 * sum(r[2] for r in named[:10]), 1)
        m[f"{regime.lower()}_hhi"] = round(sum((r[2] * 100) ** 2 for r in named))
        m[f"{regime.lower()}_top_manager"] = named[0][0]
        m[f"{regime.lower()}_top_manager_share_pct"] = round(100 * named[0][2], 1)
    m["internally_managed_portfolios"] = cnt("structure<>'UMBRELLA' AND manager_name='Internally managed'")
    m["top10_managers"] = [{"manager": r[0], "portfolios": r[1]} for r in con.execute(
        """SELECT manager_name, SUM(n) FROM gold_manager_concentration WHERE manager_name NOT IN ('Internally managed','No ManCo / AIFM printed')
           GROUP BY 1 ORDER BY 2 DESC LIMIT 10""").fetchall()]
    m["portfolios_authorised_since_2020_pct"] = pct(cnt("structure<>'UMBRELLA' AND auth_year >= 2020"), m["portfolios"])
    m["portfolios_authorised_2026_ytd"] = cnt("structure<>'UMBRELLA' AND auth_year = 2026")
    m["peak_year"] = con.execute("""SELECT auth_year FROM gold_auth_by_year GROUP BY 1 ORDER BY SUM(portfolios_authorised) DESC LIMIT 1""").fetchone()[0]
    m["managers_mapped_to_register_pct"] = pct(q1(con, "SELECT COUNT(*) FROM gold_fund WHERE structure<>'SUB_FUND' AND manager_cbi_reference IS NOT NULL"),
                                               q1(con, "SELECT COUNT(*) FROM gold_fund WHERE structure<>'SUB_FUND' AND manager_name NOT IN ('Internally managed','No ManCo / AIFM printed')"))
    a["market"] = m
    C.AUDIT_PATH.write_text(json.dumps(a, indent=2, default=str))
    return a
