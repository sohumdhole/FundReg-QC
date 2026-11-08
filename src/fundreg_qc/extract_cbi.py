"""Extract fund and manager records from Central Bank of Ireland register PDFs.

Each fund register prints umbrella or standalone funds with a long date
(e.g. "05 Jul 1989" or "16 March 2009") and their sub-funds underneath with a
short date (e.g. "19-Nov-90"). Columns are located per page from the header
row word positions, so empty cells do not shift values between columns.
Every register prints control totals on its last page; these are extracted
too and reconciled against the parsed row counts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

import pdfplumber

LONG_DATE = re.compile(
    r"^(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]* (\d{4})$"
)
SHORT_DATE = re.compile(r"^(\d{1,2})-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-(\d{2})$")
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
FLAG_CHARS = "#§†Ø*‡$"
STOP_PREFIXES = ("Total number", "Run Date", "Run date", "Page ")

REGISTERS = {
    "UCITS": ("cbi_register_ucits.pdf", "UCITS"),
    "ICAV": ("cbi_register_icav.pdf", "AIF"),
    "DIC": ("cbi_register_dic.pdf", "AIF"),
    "UNIT_TRUST": ("cbi_register_unit_trust.pdf", "AIF"),
    "CCF": ("cbi_register_ccf.pdf", "AIF"),
    "ILP": ("cbi_register_ilp.pdf", "AIF"),
}


def to_iso(text: str) -> str | None:
    m = LONG_DATE.match(text)
    if m:
        return f"{int(m.group(3)):04d}-{MONTHS[m.group(2)[:3]]:02d}-{int(m.group(1)):02d}"
    m = SHORT_DATE.match(text)
    if m:
        yy = int(m.group(3))
        year = 1900 + yy if yy >= 70 else 2000 + yy
        return f"{year:04d}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    return None


DATE_PREFIX = re.compile(
    r"^(\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{4}|\d{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{2})\b\s*(.*)$"
)


def split_date_cell(cell: str) -> tuple[str, str]:
    """Return (date text, trailing note) e.g. '04 Sep 2015 Redomiciled From'."""
    m = DATE_PREFIX.match(cell.strip())
    if not m:
        return "", ""
    return m.group(1), m.group(2).strip()


def split_flags(name: str) -> tuple[str, str]:
    flags = "".join(ch for ch in name if ch in FLAG_CHARS)
    clean = "".join(ch for ch in name if ch not in FLAG_CHARS)
    return re.sub(r"\s+", " ", clean).strip(), flags


@dataclass
class Record:
    register: str
    regime: str
    level: str  # TOP (umbrella or standalone) or SUB
    name_raw: str
    auth_date_raw: str
    page: int
    line_top: float
    manager_raw: str = ""
    aifm_raw: str = ""
    internally_managed: str = ""
    depositary_raw: str = ""
    umbrella_seq: int = 0
    seq: int = 0
    extra_name_lines: list = field(default_factory=list)
    date_notes: list = field(default_factory=list)


def _lines(words, tol=3.6):
    rows = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(rows[-1][0] - w["top"]) <= tol:
            rows[-1][1].append(w)
        else:
            rows.append([w["top"], [w]])
    return [(top, sorted(ws, key=lambda w: w["x0"])) for top, ws in rows]


def _header(lines):
    for i, (top, ws) in enumerate(lines):
        texts = [w["text"] for w in ws]
        if ws and ws[0]["text"] == "Name" and ws[0]["x0"] < 60 and "Authorisation" in texts:
            return i, top, ws
    return None


def _columns(header_ws, nearby_ws):
    cols = {"name": 0.0}
    texts = [w["text"] for w in header_ws]
    for i, w in enumerate(header_ws):
        t = w["text"]
        if t == "Authorisation":
            cols["date"] = min(w["x0"], cols.get("date", 1e9))
        elif t == "Alternative":
            cols["aifm"] = w["x0"]
        elif t == "Internally":
            cols["internal"] = w["x0"]
        elif t == "Management" and i + 1 < len(header_ws) and header_ws[i + 1]["text"] == "Company":
            cols["manco"] = w["x0"]
        elif t in ("Depositary", "Trustee"):
            cols["dep"] = w["x0"]
    for w in nearby_ws:  # "Date Of" sits on the line above "Authorisation"
        if w["text"] == "Date" and 0 <= cols.get("date", -99) - w["x0"] < 45:
            cols["date"] = min(cols["date"], w["x0"])
        if w["text"] in ("Internally", "Managed") and "aifm" in cols and w["x0"] > cols["aifm"] + 60:
            cols["internal"] = min(w["x0"], cols.get("internal", 1e9))
    if "date" not in cols:
        raise ValueError("date column not found: " + " ".join(texts))
    return dict(sorted(cols.items(), key=lambda kv: kv[1]))


def _assign(ws, cols):
    starts = list(cols.items())
    out = {k: [] for k in cols}
    for w in ws:
        key = "name"
        for k, x in starts:
            if w["x0"] + 3 >= x:
                key = k
        out[key].append(w["text"])
    return {k: " ".join(v).strip() for k, v in out.items()}


LEVEL_RULES = {"ICAV": "internal_col", "CCF": "gap"}


def parse_register(path: str, register: str, regime: str):
    level_rule = LEVEL_RULES.get(register, "date_format")
    records: list[Record] = []
    totals_text = []
    unparsed = []
    current_top = None
    seq = 0
    prev_cols = None
    pages_without_header = []
    last_name_line = None
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            lines = _lines(words)
            for top, ws in lines:
                text = " ".join(w["text"] for w in ws)
                if text.startswith("Total number"):
                    totals_text.append(text)
            hdr = _header(lines)
            if hdr is None:
                if prev_cols is None:
                    continue
                hi, htop, cols = -1, -1e9, prev_cols  # page without a repeated header
                pages_without_header.append(pno)
            else:
                hi, htop, hws = hdr
                nearby = [w for t, l in lines if abs(t - htop) <= 14 for w in l]
                cols = _columns(hws, nearby)
            prev_cols = cols
            last_kind = None
            prev_top = htop if htop > -1e8 else None
            new_page = False
            for top, ws in lines[hi + 1:]:
                if top <= htop + 8:
                    continue  # second header line ("Managed")
                text = " ".join(w["text"] for w in ws)
                if text.startswith(STOP_PREFIXES) or text[:1] in "†Ø#§‡*$" and "This fund" in text:
                    last_kind = None
                    continue
                if text.startswith(("Please note", "Due to time")):
                    continue
                cells = _assign(ws, cols)
                date_cell = cells.get("date", "")
                date_txt, date_note = split_date_cell(date_cell)
                iso = to_iso(date_txt) if date_txt else None
                name = cells.get("name", "")
                gap = top - prev_top if prev_top is not None else 0
                prev_top = top
                if iso and name:
                    seq += 1
                    is_long = bool(LONG_DATE.match(date_txt))
                    if level_rule == "internal_col":
                        is_long = is_long and bool(re.match(r"^(Yes|No)\b", cells.get("internal", "").strip()))
                    elif level_rule == "gap":
                        # CCF: a new block starts after a blank gap; every CCF in this
                        # register carries the '#' (qualifying investor) key, sub-funds do not
                        is_long = is_long and (current_top is None or gap > 11) and "#" in name
                    new_page = False
                    rec = Record(register, regime, "TOP" if is_long else "SUB", name, date_txt,
                                 pno, round(top, 1), seq=seq)
                    if date_note:
                        rec.date_notes.append(date_note)
                    rec.manager_raw = cells.get("manco", "")
                    rec.aifm_raw = cells.get("aifm", "")
                    rec.internally_managed = cells.get("internal", "")
                    rec.depositary_raw = cells.get("dep", "")
                    if is_long:
                        current_top = rec
                        rec.umbrella_seq = seq
                    else:
                        if current_top is None:
                            unparsed.append({"page": pno, "text": text, "reason": "sub-fund before any umbrella"})
                            continue
                        rec.umbrella_seq = current_top.seq
                    records.append(rec)
                    last_kind = "rec"
                elif name and not date_txt and last_kind == "rec" and records:
                    # wrapped fund name continues on the next line in the name column
                    records[-1].extra_name_lines.append(name)
                    last_name_line = (pno, top, records[-1])
                elif date_cell and not iso and not name:
                    if records and last_kind == "rec":
                        records[-1].date_notes.append(date_cell)
                elif date_cell and not iso:
                    unparsed.append({"page": pno, "text": text, "reason": "unrecognised date"})
                elif iso and not name and last_name_line and last_name_line[0] == pno \
                        and 0 < top - last_name_line[1] <= 8 and current_top is not None:
                    # the name sits slightly above its date and was read as a wrapped
                    # line of the previous record: move it into its own record
                    owner = last_name_line[2]
                    moved = owner.extra_name_lines.pop()
                    seq += 1
                    rec = Record(register, regime, "SUB", moved, date_txt, pno, round(top, 1), seq=seq,
                                 umbrella_seq=current_top.seq)
                    rec.date_notes.append("name realigned from line above")
                    records.append(rec)
                    last_name_line = None
                    last_kind = "rec"
                elif iso and not name and current_top is not None:
                    # source row carries an authorisation date but no fund name; keep it
                    # so counts reconcile, and let the DQ rules quarantine it
                    seq += 1
                    rec = Record(register, regime, "SUB", "", date_txt, pno, round(top, 1), seq=seq,
                                 umbrella_seq=current_top.seq)
                    rec.date_notes.append("source row has a date but no fund name")
                    records.append(rec)
                    last_kind = None
                else:
                    if not name:
                        last_kind = "addr" if last_kind else None
    unparsed.extend({"page": p, "text": "", "reason": "page without header; previous page columns reused"}
                    for p in pages_without_header)
    return records, totals_text, unparsed


def totals_from_text(totals_text):
    out = {}
    for t in totals_text:
        m = re.search(r":\s*([\d,]+)\s*$", t)
        if not m:
            continue
        n = int(m.group(1).replace(",", ""))
        low = t.lower()
        if "including sub-funds" in low or "standalone and sub-funds" in low:
            out["printed_standalone_plus_subfunds"] = n
        else:
            out["printed_top_level"] = n
    return out


def record_rows(records):
    rows = []
    for r in records:
        d = asdict(r)
        full = " ".join([r.name_raw] + r.extra_name_lines)
        d["name_full_raw"] = re.sub(r"\s+", " ", full).strip()
        d["name_clean"], d["flags"] = split_flags(d["name_full_raw"])
        d["auth_date"] = to_iso(r.auth_date_raw)
        d["extra_name_lines"] = " | ".join(r.extra_name_lines)
        d["date_notes"] = " | ".join(r.date_notes)
        rows.append(d)
    return rows


# ---------------------------------------------------------------- managers
REF = re.compile(r"^C\d+$")


def parse_manager_register(path: str, kind: str):
    """Return [(reference, name)] from the AIFM or UCITS ManCo register.

    The reference number sits alone in the left column; the name is the first
    line of the name/address column, plus a second line when the first line
    is cut mid-name (the next line starts lower-case or with 'Limited' etc.).
    """
    out = []
    totals = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            words = page.extract_words()
            lines = _lines(words)
            name_x = None
            for top, ws in lines:
                t = " ".join(w["text"] for w in ws)
                if t.startswith("Total number"):
                    totals.append(t)
                if ws[0]["text"] == "Reference":
                    for w in ws:
                        if w["text"] == "Name":
                            name_x = w["x0"]
                            break
            if name_x is None:
                name_x = 90.0
            for idx, (top, ws) in enumerate(lines):
                if REF.match(ws[0]["text"]) and ws[0]["x0"] < name_x - 5:
                    name_words = [w for w in ws[1:] if w["x0"] < name_x + 150]
                    name = " ".join(w["text"] for w in name_words)
                    if idx + 1 < len(lines):
                        nws = [w for w in lines[idx + 1][1] if name_x - 5 <= w["x0"] < name_x + 150]
                        nxt = " ".join(w["text"] for w in nws)
                        if nxt and re.match(r"^(Limited|Ltd|DAC|plc|S\.A\.|SA|LLC|LLP|Designated|Activity|Company|\(|[a-z])", nxt):
                            name = name + " " + nxt
                    out.append({"reference": ws[0]["text"], "name_raw": name.strip(), "page": pno, "kind": kind})
    return out, totals
