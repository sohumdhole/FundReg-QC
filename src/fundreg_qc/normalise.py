"""Name normalisation rules (documented in docs/methodology.md).

Two keys are produced for every name:
  name_key   upper case, accents folded, '&' -> AND, dashes unified, punctuation
             removed, whitespace collapsed. Used for exact matching.
  name_core  name_key with trailing legal-form words removed (PLC, ICAV, DAC,
             LIMITED, ...). Used for exact-core and fuzzy matching.
Umbrella and sub-fund separators seen in GLEIF legal names (' - ', ' : ',
', A SUB-FUND OF', ' / ') are split so a sub-fund can be compared on its own
name inside the umbrella it belongs to.
"""
from __future__ import annotations

import re
import unicodedata

DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")

CURRENCY = {"\u20ac": "EUR", "$": "USD", "\u00a3": "GBP", "\u00a5": "JPY"}
CURRENCY_CODES = {"EUR", "USD", "GBP", "JPY", "CHF", "SEK", "NOK", "DKK", "AUD", "CAD", "SGD", "HKD", "CNY", "CNH"}
ROMAN = re.compile(r"^(?=[IVXLC]+$)M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")


LEGAL_CLASS = [
    ("PUBLIC LIMITED COMPANY", "PLC"), ("P L C", "PLC"), ("PLC", "PLC"),
    ("IRISH COLLECTIVE ASSET MANAGEMENT VEHICLE", "ICAV"), ("ICAV", "ICAV"),
    ("INVESTMENT LIMITED PARTNERSHIP", "ILP"), ("LIMITED PARTNERSHIP", "ILP"), ("I L P", "ILP"),
    ("ILP", "ILP"), ("L P", "ILP"), ("LP", "ILP"),
    ("DESIGNATED ACTIVITY COMPANY", "DAC"), ("DAC", "DAC"),
    ("COMMON CONTRACTUAL FUND", "CCF"), ("CCF", "CCF"),
    ("LIMITED", "LTD"), ("LTD", "LTD"), ("UNIT TRUST", "UT"),
]


def legal_form_class(text: str) -> str | None:
    """Legal-form class from the end of the name (PLC, ICAV, ILP, DAC, CCF, LTD, UT) or None.

    Used as a guard: two names whose legal-form classes are both present and
    different (e.g. '... Fund I ICAV' vs '... Fund I ILP') are different entities.
    """
    k = name_key(text).replace(" - ", " ")
    for suf, cls in LEGAL_CLASS:
        if k.endswith(" " + suf):
            return cls
    return None


def legal_forms_conflict(a: str, b: str) -> bool:
    ca, cb = legal_form_class(a), legal_form_class(b)
    return bool(ca and cb and ca != cb)


def distinguishing_tokens(core: str) -> frozenset:
    """Tokens that tell sibling funds apart: numbers, years, roman numerals, single
    letters and currency codes. Fuzzy matches must agree on all of them."""
    out = set()
    for tok in core.split():
        if tok.isdigit() or ROMAN.match(tok) or len(tok) <= 2 or tok in CURRENCY_CODES \
                or any(ch.isdigit() for ch in tok):
            out.add(tok)
    return frozenset(out)


# applied repeatedly to the end of the name, longest first
LEGAL_SUFFIXES = [
    "PUBLIC LIMITED COMPANY", "DESIGNATED ACTIVITY COMPANY", "LIMITED PARTNERSHIP",
    "COMMON CONTRACTUAL FUND", "IRISH COLLECTIVE ASSET MANAGEMENT VEHICLE",
    "INVESTMENT LIMITED PARTNERSHIP", "UNIT TRUST", "PLC", "P L C", "ICAV", "DAC",
    "LIMITED", "LTD", "CCF", "ILP", "I L P", "LP", "L P", "SA", "S A", "SARL", "S A R L",
    "LLC", "LLP", "SICAV", "SE", "AG", "GMBH", "INC",
]
LEADING = ["THE "]
SUBFUND_SPLIT = re.compile(
    r"\s(?:-|:|/)\s|,?\s+(?:A|THE)\s+SUB[\s-]?FUND\s+OF\s+|\s+SUB[\s-]?FUND\s+OF\s+"
)
SUBFUND_OF = re.compile(r"^(?P<sub>.+?),?\s+(?:A|THE)?\s*SUB[\s-]?FUND\s+OF\s+(?:THE\s+)?(?P<umb>.+)$")


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.translate(DASHES)


def name_key(text: str) -> str:
    t = fold(text).upper()
    t = t.replace("&", " AND ")
    for sym, code in CURRENCY.items():
        t = t.replace(sym, f" {code} ")
    t = re.sub(r"\(\s*IRL\s*\)", " IRL ", t)
    t = re.sub(r"(?<=[A-Z0-9])-(?=[A-Z0-9])", " ", t)  # intra-word hyphen
    t = re.sub(r"[^A-Z0-9\- ]+", " ", t)
    t = re.sub(r"\s*-\s*", " - ", t)
    return re.sub(r"\s+", " ", t).strip(" -")


def strip_legal(key: str) -> str:
    t = key.replace(" - ", " ")
    t = re.sub(r"\s+", " ", t).strip()
    for lead in LEADING:
        if t.startswith(lead):
            t = t[len(lead):]
    changed = True
    while changed and t:
        changed = False
        for suf in LEGAL_SUFFIXES:
            if t == suf:
                continue
            if t.endswith(" " + suf):
                t = t[: -len(suf) - 1].strip()
                changed = True
                break
    return t


def name_core(text: str) -> str:
    return strip_legal(name_key(text))


def split_gleif_name(legal_name: str) -> tuple[str, str]:
    """Return (umbrella_part, sub_part) as name_core keys; ('', '') if no separator."""
    raw = fold(legal_name).upper()
    m = SUBFUND_OF.match(raw)
    if m:
        return name_core(m.group("umb")), name_core(m.group("sub"))
    parts = SUBFUND_SPLIT.split(raw, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return name_core(parts[0]), name_core(parts[1])
    return "", ""


def manager_core(text: str) -> str:
    """Manager names: drop trading names ('t/a ...') and addresses after a comma."""
    t = re.split(r"\s+t/a\s+|\s+T/A\s+|\s+trading as\s+", text or "", maxsplit=1)[0]
    return name_core(t)


def lei_check_digits_ok(lei: str) -> bool:
    """ISO 17442: 20 alphanumeric characters, mod 97 of the numeric form equals 1."""
    if not lei or not re.fullmatch(r"[A-Z0-9]{18}[0-9]{2}", lei):
        return False
    digits = "".join(str(int(ch, 36)) for ch in lei)
    return int(digits) % 97 == 1
