"""FundReg-QC test suite.

Groups: unit tests on normalisation and matching guards (labelled synthetic
fixture), data contracts on the built database, metric recomputation against the
audit record, doc-number tracing, a mutation check, and repository hygiene
(no em dashes, no raw data committed, no banned project names).
"""
from __future__ import annotations

import copy
import csv
import json
import re
import subprocess
from pathlib import Path

import duckdb
import pytest

from fundreg_qc import config as C
from fundreg_qc import audit, render_docs
from fundreg_qc.normalise import (name_core, name_key, split_gleif_name, legal_forms_conflict,
                                  distinguishing_tokens, lei_check_digits_ok, manager_core)
from fundreg_qc.extract_cbi import to_iso, split_flags, split_date_cell, totals_from_text

ROOT = C.ROOT
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_names.csv"
DB_EXISTS = C.DB_PATH.exists()
needs_db = pytest.mark.skipif(not DB_EXISTS, reason="build the database first: python -m fundreg_qc.pipeline")


# ---------------------------------------------------------------- unit: normalisation (synthetic fixture)
def test_fixture_is_labelled_synthetic():
    first = FIXTURE.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# SYNTHETIC TEST FIXTURE")


@pytest.mark.parametrize("row", list(csv.DictReader(
    [l for l in open(FIXTURE, encoding="utf-8") if not l.startswith("#")])))
def test_name_core_fixture(row):
    assert name_core(row["input"]) == row["expected_core"]


def test_currency_symbols_become_codes():
    assert name_core("Example € Bond Fund") != name_core("Example $ Bond Fund")
    assert "EUR" in name_core("Example € Bond Fund")


def test_split_gleif_name_variants():
    assert split_gleif_name("Alpha Funds plc - Beta Equity Fund") == ("ALPHA FUNDS", "BETA EQUITY FUND")
    assert split_gleif_name("Beta Equity Fund, a sub-fund of Alpha Funds plc") == ("ALPHA FUNDS", "BETA EQUITY FUND")
    assert split_gleif_name("Plain Name Fund") == ("", "")


def test_guards():
    assert legal_forms_conflict("Sample Fund I ICAV", "Sample Fund I ILP")
    assert not legal_forms_conflict("Sample Funds plc", "SAMPLE FUNDS PUBLIC LIMITED COMPANY")
    assert distinguishing_tokens("SAMPLE FUND XII") != distinguishing_tokens("SAMPLE FUND XIV")
    assert distinguishing_tokens("SAMPLE 2029 FUND") != distinguishing_tokens("SAMPLE 2026 FUND")
    assert distinguishing_tokens("SAMPLE GLOBAL FUND") == distinguishing_tokens("GLOBAL SAMPLE FUND")


def test_lei_check_digits():
    assert lei_check_digits_ok("5493001KJTIIGC8Y1R12")      # GLEIF's own LEI
    assert not lei_check_digits_ok("5493001KJTIIGC8Y1R13")
    assert not lei_check_digits_ok("TOO-SHORT")


def test_manager_core_drops_trading_name():
    assert manager_core("Example Managers Limited t/a Brand X") == "EXAMPLE MANAGERS"


def test_date_parsing():
    assert to_iso("05 Jul 1989") == "1989-07-05"
    assert to_iso("16 March 2009") == "2009-03-16"
    assert to_iso("19-Nov-90") == "1990-11-19"
    assert to_iso("01-Mar-24") == "2024-03-01"
    assert split_date_cell("04 Sep 2015 Redomiciled From") == ("04 Sep 2015", "Redomiciled From")
    assert split_flags("Example ICAV#§") == ("Example ICAV", "#§")


def test_totals_parsing():
    t = totals_from_text(["Total number of authorised UCITS: 10",
                          "Total number of authorised UCITS including sub-funds: 25"])
    assert t == {"printed_top_level": 10, "printed_standalone_plus_subfunds": 25}


# ---------------------------------------------------------------- data contracts
@pytest.fixture(scope="module")
def con():
    c = duckdb.connect(str(C.DB_PATH), read_only=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def a():
    return json.loads(C.AUDIT_PATH.read_text())


@needs_db
def test_contract_fund_ids_unique(con):
    assert con.execute("SELECT COUNT(*) - COUNT(DISTINCT fund_id) FROM silver_cbi_fund_all").fetchone()[0] == 0


@needs_db
def test_contract_structure_values(con):
    vals = {r[0] for r in con.execute("SELECT DISTINCT structure FROM silver_cbi_fund_all").fetchall()}
    assert vals <= {"UMBRELLA", "STANDALONE", "SUB_FUND"}


@needs_db
def test_contract_subfunds_have_umbrella(con):
    assert con.execute("""SELECT COUNT(*) FROM silver_cbi_fund s LEFT JOIN silver_cbi_fund_all u ON u.fund_id=s.umbrella_fund_id
                          WHERE s.structure='SUB_FUND' AND (u.fund_id IS NULL OR u.structure<>'UMBRELLA')""").fetchone()[0] == 0


@needs_db
def test_contract_dates_valid(con):
    assert con.execute("""SELECT COUNT(*) FROM silver_cbi_fund WHERE auth_date IS NULL
                          OR auth_date < DATE '1960-01-01' OR auth_date > CAST(register_as_at AS DATE)""").fetchone()[0] == 0


@needs_db
def test_contract_quarantine_excluded(con):
    assert con.execute("""SELECT COUNT(*) FROM silver_cbi_fund WHERE fund_id IN (SELECT fund_id FROM silver_cbi_quarantine)""").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM silver_cbi_fund WHERE COALESCE(fund_name,'')=''").fetchone()[0] == 0


@needs_db
def test_contract_leis_valid_and_unique(con):
    assert con.execute("SELECT COUNT(*) FROM silver_gleif_entity WHERE NOT lei_valid").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) - COUNT(DISTINCT lei) FROM silver_gleif_entity").fetchone()[0] == 0


@needs_db
def test_contract_one_lei_per_fund_and_fund_per_lei(con):
    assert con.execute("SELECT COUNT(*) - COUNT(DISTINCT fund_id) FROM gold_match").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(lei) - COUNT(DISTINCT lei) FROM gold_match").fetchone()[0] == 0


@needs_db
def test_contract_match_bands(con):
    bad = con.execute(f"""SELECT COUNT(*) FROM gold_match WHERE
        (band='ACCEPTED' AND score < {C.FUZZY_ACCEPT}) OR (band='PROBABLE_REVIEW' AND (score >= {C.FUZZY_ACCEPT} OR score < {C.FUZZY_REVIEW}))
        OR (band='UNMATCHED' AND lei IS NOT NULL)""").fetchone()[0]
    assert bad == 0


@needs_db
def test_contract_exceptions_reference_rules_and_funds(con):
    assert con.execute("SELECT COUNT(*) FROM gold_exception WHERE rule_id NOT IN (SELECT rule_id FROM gold_rule_catalogue)").fetchone()[0] == 0
    assert con.execute("""SELECT COUNT(*) FROM gold_exception WHERE fund_id IS NOT NULL
                          AND fund_id NOT IN (SELECT fund_id FROM silver_cbi_fund_all)""").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM gold_exception WHERE severity NOT IN ('HIGH','MEDIUM','LOW')").fetchone()[0] == 0


# ---------------------------------------------------------------- metric recomputation
def recompute(con):
    out = {}
    out["funds"] = con.execute("SELECT COUNT(*) FROM silver_cbi_fund").fetchone()[0]
    out["accepted"] = con.execute("SELECT COUNT(*) FROM gold_match WHERE band='ACCEPTED'").fetchone()[0]
    out["exceptions"] = con.execute("SELECT COUNT(*) FROM gold_exception").fetchone()[0]
    out["high"] = con.execute("SELECT COUNT(*) FROM gold_exception WHERE severity='HIGH'").fetchone()[0]
    out["lapsed"] = con.execute("SELECT COUNT(*) FROM gold_exception WHERE rule_id='E01'").fetchone()[0]
    out["portfolios"] = con.execute("SELECT COUNT(*) FROM gold_fund WHERE structure<>'UMBRELLA'").fetchone()[0]
    out["rows_parsed"] = con.execute("SELECT COUNT(*) FROM silver_cbi_fund_all").fetchone()[0]
    return out


def compare(a, r):
    """Return the list of audit fields that disagree with an independent recomputation."""
    diffs = []
    checks = [("match.funds", r["funds"]), ("match.accepted", r["accepted"]), ("exceptions.total", r["exceptions"]),
              ("exceptions.by_severity.HIGH", r["high"]), ("exceptions.by_rule.E01.n", r["lapsed"]),
              ("market.portfolios", r["portfolios"]), ("cbi_rows_parsed", r["rows_parsed"])]
    for path, val in checks:
        if render_docs.lookup(a, path) != val:
            diffs.append(path)
    pct = round(100.0 * r["accepted"] / r["funds"], 1)
    if a["match"]["accepted_pct"] != pct:
        diffs.append("match.accepted_pct")
    return diffs


@needs_db
def test_metrics_recompute(con, a):
    assert compare(a, recompute(con)) == []


@needs_db
def test_mutation_is_detected(con, a):
    """Mutation check: corrupt one audit figure and one database fact; both must be caught."""
    bad = copy.deepcopy(a)
    bad["match"]["accepted"] += 1
    assert "match.accepted" in compare(bad, recompute(con))
    mem = duckdb.connect()
    mem.execute(f"ATTACH '{C.DB_PATH}' AS src (READ_ONLY)")
    for t in ["silver_cbi_fund", "gold_match", "gold_exception", "gold_fund", "silver_cbi_fund_all"]:
        mem.execute(f"CREATE TABLE {t} AS SELECT * FROM src.{t}")
    mem.execute("DELETE FROM gold_exception WHERE exception_id = (SELECT MIN(exception_id) FROM gold_exception)")
    assert "exceptions.total" in compare(a, recompute(mem))
    mem.close()


@needs_db
def test_control_totals_in_audit_match_registers(a):
    for r in a["registers"]:
        assert r["parsed_top_level"] - r["printed_top_level"] == r["top_level_variance"]
    assert a["control_totals_exact"] >= 11


@needs_db
def test_review_labels_valid(a):
    rows = list(csv.DictReader(open(C.REVIEW / "evaluation_sample_labelled.csv", encoding="utf-8")))
    assert all(r["label"] in {"CORRECT", "INCORRECT", "UNCERTAIN"} for r in rows)
    assert a["review"]["sample_rows_still_matching_current_run"] == len(rows)


# ---------------------------------------------------------------- docs trace to the audit record
NUM = re.compile(r"(?<![\w.])\d(?:[\d,]*\d)?(?:\.\d+)?%?")


def _numbers(text):
    return {n for n in NUM.findall(text)}


@needs_db
def test_docs_render_exactly_from_audit(a):
    outputs, trace, tables = render_docs.render(a, write=False)
    for target, text in outputs.items():
        assert (ROOT / target).read_text(encoding="utf-8") == text, f"{target} is stale: re-run publish"


@needs_db
def test_every_doc_number_traces_to_audit(a):
    outputs, trace, tables = render_docs.render(a, write=False)
    allowed = {t["value"] for t in trace} | {v for tb in tables.values() for v in _numbers(tb)}
    for tmpl, target in render_docs.TEMPLATES.items():
        literal = _numbers(render_docs.PH.sub("", (ROOT / "docs" / "templates" / tmpl).read_text(encoding="utf-8")))
        allowed_nums = set()
        for v in allowed:
            allowed_nums |= _numbers(v)
        for n in _numbers(outputs[target]):
            assert n in literal or n in allowed_nums, f"{target}: number {n} does not trace to the audit record"


# ---------------------------------------------------------------- repository hygiene
def _repo_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
        if out:
            return [ROOT / p for p in out]
    except Exception:
        pass
    ignore = {".git", "__pycache__", ".pytest_cache", "raw"}
    return [p for p in ROOT.rglob("*") if p.is_file() and not (set(p.parts) & ignore) and p.suffix != ".duckdb"]


TEXT_EXT = {".py", ".md", ".csv", ".json", ".txt", ".dax", ".tmpl", ".toml", ".cfg", ".ini", ".yml", ".yaml"}


def test_no_em_dashes():
    offenders = []
    for p in _repo_files():
        if p.suffix in TEXT_EXT:
            if chr(0x2014) in p.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(p.relative_to(ROOT)))
    assert offenders == []


def test_no_raw_data_in_repo():
    gi = (ROOT / ".gitignore").read_text()
    assert "data/raw/" in gi and "*.duckdb" in gi
    for p in _repo_files():
        rel = p.relative_to(ROOT).as_posix()
        assert not rel.startswith("data/raw/"), rel
        assert p.suffix.lower() not in {".pdf", ".zip", ".duckdb"}, rel


def test_no_private_tool_names():
    pat = re.compile("career" + "twin", re.I)
    for p in _repo_files():
        if p.suffix in TEXT_EXT:
            assert not pat.search(p.read_text(encoding="utf-8", errors="ignore")), str(p)
