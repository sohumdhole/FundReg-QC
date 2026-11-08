"""Paths, as-of dates and thresholds. Every threshold used in the pipeline lives here."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
RAW_CBI = RAW / "cbi"
RAW_GLEIF = RAW / "gleif"
REVIEW = ROOT / "data" / "manual_review"
OUT = ROOT / "outputs"
DB_PATH = OUT / "fundreg_qc.duckdb"
AUDIT_PATH = OUT / "audit_record.json"

# GLEIF Golden Copy publish used for this run (from gleif manifest); all LEI status
# checks are evaluated as at this date.
AS_OF = "2026-11-08"
RENEWAL_WARNING_DAYS = 60

# fuzzy matching (rapidfuzz token_sort_ratio on name_core, 0 to 100)
FUZZY_ACCEPT = 92        # at or above: accepted match
FUZZY_REVIEW = 85        # REVIEW to ACCEPT-1: probable, routed to manual review
MANAGER_MATCH = 85       # CBI manager name vs GLEIF fund-manager LEI name
REVIEW_SAMPLE_TUNING_SEED = 20261108  # first review; its errors motivated the two match guards
REVIEW_SAMPLE_SEED = 20261109         # evaluation sample, drawn after the guards were fixed
REVIEW_SAMPLE_EXACT = 20      # per exact method
REVIEW_SAMPLE_ACCEPTED = 100
REVIEW_SAMPLE_BAND = 50

CBI_REGISTER_FILES = {
    "UCITS": "cbi_register_ucits.pdf",
    "ICAV": "cbi_register_icav.pdf",
    "DIC": "cbi_register_dic.pdf",
    "UNIT_TRUST": "cbi_register_unit_trust.pdf",
    "CCF": "cbi_register_ccf.pdf",
    "ILP": "cbi_register_ilp.pdf",
}
CBI_MANAGER_FILES = {"AIFM": "cbi_register_aifm.pdf", "UCITS_MANCO": "cbi_register_ucits_mancos.pdf"}
REGISTER_LABEL = {
    "UCITS": "UCITS (all legal forms)",
    "ICAV": "AIF: ICAV",
    "DIC": "AIF: Designated Investment Company",
    "UNIT_TRUST": "AIF: Unit Trust",
    "CCF": "AIF: Common Contractual Fund",
    "ILP": "AIF: Investment Limited Partnership",
}
SOURCE_URLS = {
    "cbi_registers_page": "https://registers.centralbank.ie/downloadspage.aspx",
    "gleif_golden_copy_page": "https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy",
    "gleif_terms": "https://www.gleif.org/en/meta/lei-data-terms-of-use",
}
