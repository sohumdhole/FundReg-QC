"""Step 5a: formula-driven Excel QC pack (outputs/FundReg_QC_pack.xlsx)."""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from . import config as C

FONT = "Arial"
HDR_FILL = PatternFill("solid", fgColor="1F3A5F")
INPUT_FILL = PatternFill("solid", fgColor="FFFF00")
THIN = Side(style="thin", color="D9D9D9")


def _hdr(ws, row, headers):
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=j, value=h)
        c.font = Font(name=FONT, bold=True, color="FFFFFF")
        c.fill = HDR_FILL
        c.alignment = Alignment(wrap_text=True, vertical="center")


def _table(ws, headers, rows, widths=None):
    _hdr(ws, 1, headers)
    for i, r in enumerate(rows, 2):
        for j, v in enumerate(r, 1):
            ws.cell(row=i, column=j, value=v).font = Font(name=FONT)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
    for j, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    return len(rows) + 1


def build(con, path=None):
    path = path or (C.OUT / "FundReg_QC_pack.xlsx")
    wb = Workbook()

    # ---------------- data sheets
    exc = con.execute("""SELECT e.exception_id, e.rule_id, e.rule_name, e.severity, e.owner_queue, e.fund_id, e.lei,
                                e.register, e.structure, e.fund_name, e.umbrella_name, COALESCE(f.manager_name, ''),
                                e.gleif_legal_name, e.registration_status, e.detail, e.next_action, e.status, CAST(e.as_of AS VARCHAR)
                         FROM gold_exception e LEFT JOIN gold_fund f USING (fund_id) ORDER BY e.exception_id""").fetchall()
    ws_e = wb.active
    ws_e.title = "Exceptions"
    n_e = _table(ws_e, ["Exception ID", "Rule", "Rule name", "Severity", "Owner queue", "Fund ID", "LEI", "Register",
                        "Structure", "Fund name", "Umbrella", "Manager (CBI)", "GLEIF legal name", "LEI status",
                        "Detail", "Next action", "Status", "As of"], exc,
                 [11, 6, 34, 9, 24, 13, 22, 11, 11, 40, 32, 34, 40, 12, 50, 40, 8, 11])
    ws_e.conditional_formatting.add(f"D2:D{n_e}", CellIsRule(operator="equal", formula=['"HIGH"'],
                                    fill=PatternFill("solid", fgColor="F4CCCC")))
    ws_e.conditional_formatting.add(f"D2:D{n_e}", CellIsRule(operator="equal", formula=['"MEDIUM"'],
                                    fill=PatternFill("solid", fgColor="FCE5CD")))

    funds = con.execute("""SELECT fund_id, register, regime, structure, fund_name, umbrella_name, CAST(auth_date AS VARCHAR),
                                  manager_name, lei, match_method, match_band, registration_status,
                                  CAST(next_renewal_date AS VARCHAR), qiaif_flag, CAST(register_as_at AS VARCHAR)
                           FROM gold_fund ORDER BY fund_id""").fetchall()
    ws_f = wb.create_sheet("Funds")
    n_f = _table(ws_f, ["Fund ID", "Register", "Regime", "Structure", "Fund name", "Umbrella", "Authorised",
                        "Manager (CBI)", "LEI", "Match method", "Match band", "LEI status", "Next renewal",
                        "QIAIF flag", "Register as at"], funds,
                 [13, 11, 8, 11, 44, 34, 11, 38, 22, 20, 16, 12, 12, 9, 12])

    rules = con.execute("SELECT * FROM gold_rule_catalogue ORDER BY rule_id").fetchall()
    ws_r = wb.create_sheet("Rules")
    n_r = _table(ws_r, ["Rule", "Rule name", "Severity", "Owner queue", "Why it matters", "Next action"], rules,
                 [6, 40, 9, 26, 90, 60])
    for row in ws_r.iter_rows(min_row=2, max_row=n_r):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")

    # ---------------- managers (counts by formula)
    mgrs = [r[0] for r in con.execute("""SELECT manager_name FROM gold_fund GROUP BY 1 ORDER BY COUNT(*) DESC, 1""").fetchall()]
    ws_m = wb.create_sheet("Managers")
    _hdr(ws_m, 1, ["Manager (CBI)", "Funds on register", "Accepted LEI matches", "Match rate", "Open exceptions",
                   "HIGH", "MEDIUM", "LOW"])
    for i, m in enumerate(mgrs, 2):
        ws_m.cell(row=i, column=1, value=m)
        ws_m.cell(row=i, column=2, value=f"=COUNTIF(Funds!$H$2:$H${n_f},A{i})")
        ws_m.cell(row=i, column=3, value=f'=COUNTIFS(Funds!$H$2:$H${n_f},A{i},Funds!$K$2:$K${n_f},"ACCEPTED")')
        ws_m.cell(row=i, column=4, value=f"=IF(B{i}=0,0,C{i}/B{i})").number_format = "0.0%"
        ws_m.cell(row=i, column=5, value=f"=COUNTIF(Exceptions!$L$2:$L${n_e},A{i})")
        for j, sev in enumerate(["HIGH", "MEDIUM", "LOW"], 6):
            ws_m.cell(row=i, column=j, value=f'=COUNTIFS(Exceptions!$L$2:$L${n_e},A{i},Exceptions!$D$2:$D${n_e},"{sev}")')
        for j in range(1, 9):
            ws_m.cell(row=i, column=j).font = Font(name=FONT)
    n_m = len(mgrs) + 1
    ws_m.freeze_panes = "A2"
    ws_m.auto_filter.ref = f"A1:H{n_m}"
    for j, w in enumerate([48, 14, 14, 11, 14, 8, 9, 8], 1):
        ws_m.column_dimensions[get_column_letter(j)].width = w
    ws_m.conditional_formatting.add(f"D2:D{n_m}", ColorScaleRule(start_type="num", start_value=0.5, start_color="F4CCCC",
                                                                 end_type="num", end_value=1, end_color="D9EAD3"))
    # dropdown source list: "(All)" + managers
    ws_m.cell(row=1, column=10, value="Dropdown list").font = Font(name=FONT, bold=True)
    ws_m.cell(row=2, column=10, value="(All)").font = Font(name=FONT)
    for i, m in enumerate(mgrs, 3):
        ws_m.cell(row=i, column=10, value=m).font = Font(name=FONT)
    n_list = len(mgrs) + 2

    # ---------------- reconciliation (printed control totals vs parsed rows)
    ws_c = wb.create_sheet("Reconciliation")
    _hdr(ws_c, 1, ["Register", "As at", "Printed: umbrellas + standalone", "Parsed: umbrellas + standalone", "Variance",
                   "Printed: standalone + sub-funds", "Parsed: standalone + sub-funds", "Variance", "Status"])
    regs = con.execute("SELECT register, as_at, printed_top_level, printed_standalone_plus_subfunds FROM bronze_cbi_totals ORDER BY register").fetchall()
    for i, (reg, as_at, pt, ps) in enumerate(regs, 2):
        top = con.execute("SELECT COUNT(*) FROM silver_cbi_fund_all WHERE register=? AND structure<>'SUB_FUND'", [reg]).fetchone()[0]
        sf = con.execute("SELECT COUNT(*) FROM silver_cbi_fund_all WHERE register=? AND structure<>'UMBRELLA'", [reg]).fetchone()[0]
        vals = [reg, as_at, pt, top, f"=D{i}-C{i}", ps, sf, f"=G{i}-F{i}", f'=IF(AND(E{i}=0,H{i}=0),"EXACT","VARIANCE")']
        for j, v in enumerate(vals, 1):
            ws_c.cell(row=i, column=j, value=v).font = Font(name=FONT)
    last = len(regs) + 1
    ws_c.cell(row=last + 1, column=1, value="Total").font = Font(name=FONT, bold=True)
    for col in "CDEFGH":
        ws_c[f"{col}{last + 1}"] = f"=SUM({col}2:{col}{last})"
        ws_c[f"{col}{last + 1}"].font = Font(name=FONT, bold=True)
    ws_c.conditional_formatting.add(f"I2:I{last}", CellIsRule(operator="equal", formula=['"VARIANCE"'],
                                    fill=PatternFill("solid", fgColor="FCE5CD")))
    ws_c.cell(row=last + 3, column=1, value=("Printed totals are typed from the last page of each CBI register PDF "
                                             "(source: registers.centralbank.ie downloads page). Parsed counts come from "
                                             "the pipeline's silver layer before quarantine.")).font = Font(name=FONT, italic=True)
    for j, w in enumerate([12, 11, 16, 16, 9, 16, 16, 9, 10], 1):
        ws_c.column_dimensions[get_column_letter(j)].width = w

    # ---------------- dashboard
    ws = wb.create_sheet("Dashboard", 0)
    ws["A1"] = "FundReg-QC: Irish fund master data quality pack"
    ws["A1"].font = Font(name=FONT, bold=True, size=14)
    ws["A2"] = f"CBI registers as at Sep to Oct 2026; GLEIF Golden Copy {C.AS_OF}. Pick a manager in B4 (yellow) to filter every count."
    ws["A2"].font = Font(name=FONT, italic=True, color="52514E")
    ws["A4"] = "Manager (CBI)"
    ws["B4"] = "(All)"
    ws["B4"].fill = INPUT_FILL
    ws["A4"].font = ws["B4"].font = Font(name=FONT, bold=True)
    dv = DataValidation(type="list", formula1=f"=Managers!$J$2:$J${n_list}", allow_blank=False)
    ws.add_data_validation(dv)
    dv.add("B4")

    sel = "$B$4"
    ws["A6"] = "Headline"
    ws["A6"].font = Font(name=FONT, bold=True, size=12)
    heads = [
        ("Funds on register (umbrellas, standalone, sub-funds)",
         f'=IF({sel}="(All)",COUNTA(Funds!$A$2:$A${n_f}),COUNTIF(Funds!$H$2:$H${n_f},{sel}))', "#,##0"),
        ("Accepted LEI matches",
         f'=IF({sel}="(All)",COUNTIF(Funds!$K$2:$K${n_f},"ACCEPTED"),COUNTIFS(Funds!$H$2:$H${n_f},{sel},Funds!$K$2:$K${n_f},"ACCEPTED"))', "#,##0"),
        ("Match rate", "=IF(B7=0,0,B8/B7)", "0.0%"),
        ("Open exceptions",
         f'=IF({sel}="(All)",COUNTA(Exceptions!$A$2:$A${n_e}),COUNTIF(Exceptions!$L$2:$L${n_e},{sel}))', "#,##0"),
        ("of which HIGH",
         f'=IF({sel}="(All)",COUNTIF(Exceptions!$D$2:$D${n_e},"HIGH"),COUNTIFS(Exceptions!$L$2:$L${n_e},{sel},Exceptions!$D$2:$D${n_e},"HIGH"))', "#,##0"),
        ("Lapsed LEIs (E01)",
         f'=IF({sel}="(All)",COUNTIF(Exceptions!$B$2:$B${n_e},"E01"),COUNTIFS(Exceptions!$L$2:$L${n_e},{sel},Exceptions!$B$2:$B${n_e},"E01"))', "#,##0"),
    ]
    for i, (label, f, fmt) in enumerate(heads, 7):
        ws.cell(row=i, column=1, value=label).font = Font(name=FONT)
        c = ws.cell(row=i, column=2, value=f)
        c.number_format = fmt
        c.font = Font(name=FONT, bold=True)
    ws.conditional_formatting.add("B9", CellIsRule(operator="lessThan", formula=["0.85"],
                                  fill=PatternFill("solid", fgColor="F4CCCC")))

    ws["A15"] = "Exceptions by rule"
    ws["A15"].font = Font(name=FONT, bold=True, size=12)
    _hdr(ws, 16, ["Rule", "Rule name", "Severity", "Owner queue", "Open"])
    for i, r in enumerate(rules, 17):
        rid, name, sev, owner = r[0], r[1], r[2], r[3]
        ws.cell(row=i, column=1, value=rid)
        ws.cell(row=i, column=2, value=f'=INDEX(Rules!$B$2:$B${n_r},MATCH(A{i},Rules!$A$2:$A${n_r},0))')
        ws.cell(row=i, column=3, value=f'=INDEX(Rules!$C$2:$C${n_r},MATCH(A{i},Rules!$A$2:$A${n_r},0))')
        ws.cell(row=i, column=4, value=f'=INDEX(Rules!$D$2:$D${n_r},MATCH(A{i},Rules!$A$2:$A${n_r},0))')
        ws.cell(row=i, column=5, value=(f'=IF({sel}="(All)",COUNTIF(Exceptions!$B$2:$B${n_e},A{i}),'
                                        f'COUNTIFS(Exceptions!$B$2:$B${n_e},A{i},Exceptions!$L$2:$L${n_e},{sel}))'))
        for j in range(1, 6):
            ws.cell(row=i, column=j).font = Font(name=FONT)
    end = 16 + len(rules)
    ws.cell(row=end + 1, column=4, value="Total").font = Font(name=FONT, bold=True)
    ws.cell(row=end + 1, column=5, value=f"=SUM(E17:E{end})").font = Font(name=FONT, bold=True)
    ws.cell(row=end + 2, column=4, value="Check: total equals headline").font = Font(name=FONT, italic=True)
    ws.cell(row=end + 2, column=5, value=f'=IF(E{end + 1}=B10,"OK","CHECK")').font = Font(name=FONT, italic=True)
    ws.conditional_formatting.add(f"E17:E{end}", ColorScaleRule(start_type="min", start_color="FFFFFF",
                                                                end_type="max", end_color="F6B26B"))
    ws.conditional_formatting.add(f"C17:C{end}", CellIsRule(operator="equal", formula=['"HIGH"'],
                                  fill=PatternFill("solid", fgColor="F4CCCC")))

    # match rate by register (formulas on Funds)
    start2 = end + 5
    ws.cell(row=start2 - 1, column=1, value="Match rate by register").font = Font(name=FONT, bold=True, size=12)
    _hdr(ws, start2, ["Register", "Funds", "Accepted", "Match rate"])
    reg_names = [r[0] for r in regs]
    for i, reg in enumerate(reg_names, start2 + 1):
        ws.cell(row=i, column=1, value=reg)
        ws.cell(row=i, column=2, value=(f'=IF({sel}="(All)",COUNTIF(Funds!$B$2:$B${n_f},A{i}),'
                                        f'COUNTIFS(Funds!$B$2:$B${n_f},A{i},Funds!$H$2:$H${n_f},{sel}))'))
        ws.cell(row=i, column=3, value=(f'=IF({sel}="(All)",COUNTIFS(Funds!$B$2:$B${n_f},A{i},Funds!$K$2:$K${n_f},"ACCEPTED"),'
                                        f'COUNTIFS(Funds!$B$2:$B${n_f},A{i},Funds!$K$2:$K${n_f},"ACCEPTED",Funds!$H$2:$H${n_f},{sel}))'))
        ws.cell(row=i, column=4, value=f"=IF(B{i}=0,0,C{i}/B{i})").number_format = "0.0%"
        for j in range(1, 5):
            ws.cell(row=i, column=j).font = Font(name=FONT)
    end2 = start2 + len(reg_names)
    for j, w in enumerate([46, 44, 10, 28, 10], 1):
        ws.column_dimensions[get_column_letter(j)].width = w

    ch = BarChart()
    ch.type = "bar"
    ch.title = "Open exceptions by rule (selected manager)"
    ch.add_data(Reference(ws, min_col=5, min_row=16, max_row=end), titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=1, min_row=17, max_row=end))
    ch.legend = None
    ch.height, ch.width = 9, 14
    ws.add_chart(ch, "G4")
    ch2 = BarChart()
    ch2.title = "Match rate by register (selected manager)"
    ch2.add_data(Reference(ws, min_col=4, min_row=start2, max_row=end2), titles_from_data=True)
    ch2.set_categories(Reference(ws, min_col=1, min_row=start2 + 1, max_row=end2))
    ch2.legend = None
    ch2.y_axis.numFmt = "0%"
    ch2.height, ch2.width = 8, 14
    ws.add_chart(ch2, "G24")

    # ---------------- README sheet
    ws0 = wb.create_sheet("README", 1)
    lines = [
        "How to use",
        "1. Dashboard!B4 (yellow) is the only input. Pick a manager from the dropdown, or (All).",
        "2. Every count on the Dashboard and Managers sheets is a COUNTIF/COUNTIFS formula over the Exceptions and Funds sheets.",
        "3. Filter the Exceptions sheet by Owner queue to work it like a data operations queue.",
        "4. Reconciliation shows the CBI register control totals against the parsed rows.",
        "",
        "Sources",
        "Central Bank of Ireland fund, AIFM and UCITS ManCo registers (PDF), https://registers.centralbank.ie/downloadspage.aspx",
        f"GLEIF Golden Copy Level 1 and Level 2 (CC0), publish {C.AS_OF} 08:00 UTC, https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy",
        "",
        "Scope limits",
        "Registration and master data only. No PRIIPs KID, SFDR or EET content. Fuzzy matches have a measured error rate (see README).",
        "Manager (CBI) is the ManCo for UCITS and the AIFM for AIFs as printed on the register; sub-funds inherit the umbrella's.",
    ]
    for i, t in enumerate(lines, 1):
        c = ws0.cell(row=i, column=1, value=t)
        c.font = Font(name=FONT, bold=t in ("How to use", "Sources", "Scope limits"))
    ws0.column_dimensions["A"].width = 130
    wb.save(path)
    return path
