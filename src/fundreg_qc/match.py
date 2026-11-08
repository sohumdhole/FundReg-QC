"""Step 2: reconcile CBI register funds to GLEIF LEIs.

Order of methods (first hit wins):
  1 EXACT_NAME            name_core equal (umbrellas and standalone funds; sub-funds by combined name)
  2 EXACT_IN_UMBRELLA     sub-fund name equals the sub-fund part of a GLEIF record linked to
                          the matched umbrella (by IS_SUBFUND_OF or by umbrella name prefix)
  3 EXACT_UNIQUE_SUBFUND  sub-fund name_core equals exactly one GLEIF name_core (3+ tokens)
  4 FUZZY_IN_UMBRELLA     token_sort_ratio within the umbrella candidate set
  5 FUZZY_GLOBAL          token_sort_ratio against all Irish GLEIF names
Fuzzy scores >= FUZZY_ACCEPT are accepted, FUZZY_REVIEW..FUZZY_ACCEPT-1 are
'probable' and kept out of match-rate numerators until reviewed.
An LEI may be assigned to one CBI fund only; losers of a clash are recorded.
"""
from __future__ import annotations

import csv
import random
from collections import defaultdict

from rapidfuzz import fuzz, process

from . import config as C
from .normalise import distinguishing_tokens, legal_forms_conflict

STATUS_RANK = {"ISSUED": 1, "PENDING_TRANSFER": 2, "PENDING_ARCHIVAL": 2, "LAPSED": 3}
POOL_RANK = {"IE_FUND": 1, "IE_OTHER": 2, "COUNTERPARTY": 3}
METHOD_RANK = {"EXACT_NAME": 1, "EXACT_IN_UMBRELLA": 2, "EXACT_UNIQUE_SUBFUND": 3,
               "FUZZY_IN_UMBRELLA": 4, "FUZZY_GLOBAL": 5}


def _best(leis, ent):
    return sorted(leis, key=lambda l: (STATUS_RANK.get(ent[l]["registration_status"], 4),
                                       POOL_RANK.get(ent[l]["pool"], 9), l))


def run(con):
    ent = {}
    for row in con.execute("""SELECT lei, legal_name, name_core, umb_part, sub_part, other_core,
                                     registration_status, pool, entity_category
                              FROM silver_gleif_entity WHERE pool IN ('IE_FUND','IE_OTHER')""").fetchall():
        ent[row[0]] = dict(zip(["lei", "legal_name", "name_core", "umb_part", "sub_part", "other_core",
                                "registration_status", "pool", "entity_category"], row))
    by_core = defaultdict(list)
    for l, e in ent.items():
        if e["name_core"]:
            by_core[e["name_core"]].append(l)
        if e["other_core"] and e["other_core"] != e["name_core"]:
            by_core[e["other_core"]].append(l)
    children = defaultdict(set)
    for s, e in con.execute("SELECT start_lei, end_lei FROM silver_gleif_rel WHERE rel_type='IS_SUBFUND_OF'").fetchall():
        children[e].add(s)
    by_umb_part = defaultdict(set)
    for l, e in ent.items():
        if e["umb_part"]:
            by_umb_part[e["umb_part"]].add(l)
    fund_choices = list(ent.keys())
    fund_names = [ent[l]["name_core"] for l in fund_choices]

    funds = con.execute("""SELECT fund_id, register, structure, fund_name, name_core, umbrella_fund_id,
                                  umbrella_core FROM silver_cbi_fund ORDER BY fund_id""").fetchall()
    tops = [f for f in funds if f[2] != "SUB_FUND"]
    subs = [f for f in funds if f[2] == "SUB_FUND"]
    result = {}

    def put(fid, leis, method, score, cand_n, note=""):
        leis = _best(leis, ent)
        result[fid] = {"fund_id": fid, "lei": leis[0], "method": method, "score": score,
                       "n_candidates": cand_n, "alt_leis": ";".join(leis[1:]), "note": note}

    guard_rejects = []

    def ok_form(fid, nm, leis):
        keep = []
        for l in leis:
            if legal_forms_conflict(nm, ent[l]["legal_name"]):
                guard_rejects.append((fid, l, 100.0, "LEGAL_FORM_CONFLICT"))
            else:
                keep.append(l)
        return keep

    # ---- pass 1: exact methods
    def umbrella_candidates(ufid, ucore):
        cand = set(by_umb_part.get(ucore, set()))
        ulei = result.get(ufid, {}).get("lei")
        if ulei:
            cand |= {c for c in children.get(ulei, set()) if c in ent}
        return cand

    for fid, reg, st, nm, core, _, _ in tops:
        if core in by_core:
            cands = ok_form(fid, nm, by_core[core])
            if cands:
                put(fid, cands, "EXACT_NAME", 100.0, len(cands))
    for fid, reg, st, nm, core, ufid, ucore in subs:
        if not core:
            continue
        combined = (ucore + " " + core).strip()
        if combined in by_core and ok_form(fid, nm, by_core[combined]):
            cands = ok_form(fid, nm, by_core[combined])
            put(fid, cands, "EXACT_NAME", 100.0, len(cands))
            continue
        exact = [l for l in umbrella_candidates(ufid, ucore)
                 if (ent[l]["sub_part"] or ent[l]["name_core"]) == core
                 or (ucore and ent[l]["name_core"] == ucore + " " + core)]
        exact = ok_form(fid, nm, exact)
        if exact:
            put(fid, exact, "EXACT_IN_UMBRELLA", 100.0, len(exact))
            continue
        if core in by_core and len(core.split()) >= 3 and len(set(by_core[core])) == 1 \
                and ok_form(fid, nm, by_core[core]):
            put(fid, by_core[core], "EXACT_UNIQUE_SUBFUND", 100.0, 1)

    # ---- pass 2: fuzzy, only against LEIs not already taken by an exact match,
    # and only when distinguishing tokens (numbers, roman numerals, currencies,
    # single letters) agree
    taken = {m["lei"] for m in result.values()}
    taken |= {a for m in result.values() for a in m["alt_leis"].split(";") if a}
    free = [l for l in fund_choices if l not in taken]
    free_names = [ent[l]["name_core"] for l in free]

    def fuzzy(query, choices, names, method, fid, cand_n, nm):
        hits = process.extract(query, names, scorer=fuzz.token_sort_ratio,
                               score_cutoff=C.FUZZY_REVIEW, limit=5)
        q_tok = distinguishing_tokens(query)
        for name, score, idx in hits:
            lei = choices[idx]
            if distinguishing_tokens(name) != q_tok:
                guard_rejects.append((fid, lei, round(score, 1), "DISTINGUISHING_TOKENS"))
            elif legal_forms_conflict(nm, ent[lei]["legal_name"]):
                guard_rejects.append((fid, lei, round(score, 1), "LEGAL_FORM_CONFLICT"))
            else:
                put(fid, [lei], method, round(score, 1), cand_n)
                return True
        return False

    for fid, reg, st, nm, core, _, _ in tops:
        if fid in result or not core:
            continue
        fuzzy(core, free, free_names, "FUZZY_GLOBAL", fid, 1, nm)
    for fid, reg, st, nm, core, ufid, ucore in subs:
        if fid in result or not core:
            continue
        cl = sorted(c for c in umbrella_candidates(ufid, ucore) if c not in taken)
        if cl:
            names = [ent[l]["sub_part"] or ent[l]["name_core"] for l in cl]
            if fuzzy(core, cl, names, "FUZZY_IN_UMBRELLA", fid, len(cl), nm):
                continue
        fuzzy((ucore + " " + core).strip(), free, free_names, "FUZZY_GLOBAL", fid, 1, nm)

    # ---- one LEI to one CBI fund
    by_lei = defaultdict(list)
    for fid, m in result.items():
        by_lei[m["lei"]].append(fid)
    clashes = []
    for lei, fids in by_lei.items():
        if len(fids) > 1:
            fids.sort(key=lambda f: (METHOD_RANK[result[f]["method"]], -result[f]["score"], f))
            for loser in fids[1:]:
                clashes.append({"lei": lei, "kept_fund_id": fids[0], "dropped_fund_id": loser,
                                "dropped_method": result[loser]["method"], "dropped_score": result[loser]["score"]})
                del result[loser]

    rows = []
    for fid, reg, st, nm, core, ufid, ucore in funds:
        m = result.get(fid)
        if m:
            band = "ACCEPTED" if m["score"] >= C.FUZZY_ACCEPT else "PROBABLE_REVIEW"
            rows.append([fid, m["lei"], m["method"], m["score"], band, m["n_candidates"], m["alt_leis"],
                         ent[m["lei"]]["legal_name"]])
        else:
            rows.append([fid, None, "UNMATCHED", None, "UNMATCHED", 0, "", None])
    con.execute("""CREATE OR REPLACE TABLE gold_match (fund_id VARCHAR, lei VARCHAR, method VARCHAR, score DOUBLE,
                   band VARCHAR, n_candidates INTEGER, alt_leis VARCHAR, gleif_legal_name VARCHAR)""")
    con.executemany("INSERT INTO gold_match VALUES (?,?,?,?,?,?,?,?)", rows)
    con.execute("CREATE OR REPLACE TABLE gold_match_guard_reject (fund_id VARCHAR, lei VARCHAR, score DOUBLE, guard VARCHAR)")
    if guard_rejects:
        con.executemany("INSERT INTO gold_match_guard_reject VALUES (?,?,?,?)", guard_rejects)
    con.execute("CREATE OR REPLACE TABLE gold_match_clash (lei VARCHAR, kept_fund_id VARCHAR, dropped_fund_id VARCHAR, dropped_method VARCHAR, dropped_score DOUBLE)")
    if clashes:
        con.executemany("INSERT INTO gold_match_clash VALUES (?,?,?,?,?)",
                        [[c["lei"], c["kept_fund_id"], c["dropped_fund_id"], c["dropped_method"], c["dropped_score"]] for c in clashes])


def review_sample(con, path):
    """Draw the fixed-seed manual review sample (labels are then added by a human reviewer).

    Strata: REVIEW_SAMPLE_EXACT records from each exact method, up to
    REVIEW_SAMPLE_ACCEPTED accepted fuzzy matches, and up to REVIEW_SAMPLE_BAND
    probable (review-band) matches.
    """
    rng = random.Random(C.REVIEW_SAMPLE_SEED)
    q = """SELECT m.fund_id, f.register, f.structure, COALESCE(f.umbrella_name,''), f.fund_name,
                  m.lei, m.gleif_legal_name, m.method, m.score
           FROM gold_match m JOIN silver_cbi_fund f USING (fund_id) WHERE {w} ORDER BY m.fund_id"""
    strata = []
    for meth in ["EXACT_NAME", "EXACT_IN_UMBRELLA", "EXACT_UNIQUE_SUBFUND"]:
        rows = con.execute(q.format(w=f"m.method='{meth}'")).fetchall()
        strata.append((meth, rng.sample(rows, min(C.REVIEW_SAMPLE_EXACT, len(rows)))))
    rows = con.execute(q.format(w="m.method LIKE 'FUZZY%' AND m.band='ACCEPTED'")).fetchall()
    strata.append(("FUZZY_ACCEPTED", rng.sample(rows, min(C.REVIEW_SAMPLE_ACCEPTED, len(rows)))))
    rows = con.execute(q.format(w="m.band='PROBABLE_REVIEW'")).fetchall()
    strata.append(("FUZZY_REVIEW_BAND", rng.sample(rows, min(C.REVIEW_SAMPLE_BAND, len(rows)))))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stratum", "fund_id", "register", "structure", "cbi_umbrella", "cbi_fund_name", "lei",
                    "gleif_legal_name", "method", "score", "label", "reviewer_note"])
        for name, rows in strata:
            for r in rows:
                w.writerow([name, *r, "", ""])
    return {name: len(rows) for name, rows in strata}
