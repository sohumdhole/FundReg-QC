"""Step 4: market structure tables and charts, all built from the silver/gold tables."""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from rapidfuzz import fuzz, process

from . import config as C
from .normalise import manager_core

# reference palette (light mode): slot 1 blue, slot 2 orange; text and grid tokens
BLUE, ORANGE = "#2a78d6", "#eb6834"
TEXT, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def canonical_managers(con):
    """Map each fund's responsible manager to a CBI manager-register entry where possible."""
    reg = con.execute("SELECT reference, manager_name, kind, manager_core FROM silver_cbi_manager").fetchall()
    cores = [r[3] for r in reg]
    rows = con.execute("""SELECT DISTINCT responsible_manager_core, responsible_manager_raw
                          FROM silver_cbi_fund WHERE structure <> 'SUB_FUND'""").fetchall()
    out = []
    for core, raw in rows:
        if not core:
            continue
        hit = process.extractOne(core, cores, scorer=fuzz.token_sort_ratio, score_cutoff=90)
        if hit:
            r = reg[hit[2]]
            out.append((core, raw, r[0], r[1], r[2], round(hit[1], 1)))
        else:
            out.append((core, raw, None, raw, None, None))
    con.execute("""CREATE OR REPLACE TABLE gold_manager_map (manager_core VARCHAR, manager_raw VARCHAR,
                   cbi_reference VARCHAR, manager_name VARCHAR, register_kind VARCHAR, score DOUBLE)""")
    con.executemany("INSERT INTO gold_manager_map VALUES (?,?,?,?,?,?)", out)


def build(con):
    canonical_managers(con)
    con.execute("""
    CREATE OR REPLACE TABLE gold_fund AS
    SELECT f.fund_id, f.register, f.regime, f.structure, f.fund_name, f.umbrella_fund_id, f.umbrella_name,
           f.auth_date, year(f.auth_date) AS auth_year, f.qiaif_flag, f.closed_ended_flag, f.limited_liquidity_flag,
           f.is_internally_managed, f.register_as_at,
           CASE WHEN f.is_internally_managed THEN 'Internally managed'
                WHEN f.responsible_manager_core IS NULL THEN 'No ManCo / AIFM printed'
                ELSE COALESCE(mm.manager_name, f.responsible_manager_raw) END AS manager_name,
           mm.cbi_reference AS manager_cbi_reference,
           m.lei, m.method AS match_method, m.band AS match_band, g.registration_status, g.entity_status,
           g.next_renewal_date
    FROM silver_cbi_fund f
    LEFT JOIN (SELECT DISTINCT ON (manager_core) * FROM gold_manager_map) mm ON mm.manager_core = f.responsible_manager_core
    LEFT JOIN gold_match m USING (fund_id)
    LEFT JOIN silver_gleif_entity g ON g.lei = m.lei
    """)
    # fund-level unit for market structure: standalone funds + sub-funds (the investable portfolios)
    con.execute("""
    CREATE OR REPLACE TABLE gold_manager_concentration AS
    WITH p AS (SELECT * FROM gold_fund WHERE structure IN ('STANDALONE','SUB_FUND')),
    t AS (SELECT manager_name, regime, COUNT(*) n FROM p GROUP BY 1,2)
    SELECT manager_name, regime, n, n * 1.0 / SUM(n) OVER (PARTITION BY regime) AS share,
           ROW_NUMBER() OVER (PARTITION BY regime ORDER BY n DESC, manager_name) AS rank
    FROM t
    """)
    con.execute("""
    CREATE OR REPLACE TABLE gold_auth_by_year AS
    SELECT auth_year, regime,
           SUM(CASE WHEN structure IN ('UMBRELLA','STANDALONE') THEN 1 ELSE 0 END) AS top_level_authorised,
           SUM(CASE WHEN structure IN ('STANDALONE','SUB_FUND') THEN 1 ELSE 0 END) AS portfolios_authorised
    FROM gold_fund WHERE auth_year IS NOT NULL GROUP BY 1,2 ORDER BY 1,2
    """)
    charts(con)


def _style(ax, title):
    ax.set_facecolor(SURFACE)
    for s in ["top", "right"]:
        ax.spines[s].set_visible(False)
    for s in ["left", "bottom"]:
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.set_title(title, loc="left", color=TEXT, fontsize=11, pad=10)


def charts(con):
    out = C.OUT / "charts"
    out.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": SURFACE})

    # 1 match rate by register (accepted matches / funds)
    rows = con.execute("""SELECT register, AVG(CASE WHEN match_band='ACCEPTED' THEN 1.0 ELSE 0 END) r, COUNT(*) n
                          FROM gold_fund GROUP BY 1 ORDER BY r""").fetchall()
    fig, ax = plt.subplots(figsize=(7, 3.4))
    labels = [f"{C.REGISTER_LABEL[r[0]]} (n={r[2]:,})" for r in rows]
    vals = [r[1] * 100 for r in rows]
    ax.barh(labels, vals, color=BLUE, height=0.55)
    for i, v in enumerate(vals):
        ax.text(v + 1, i, f"{v:.1f}%", va="center", color=TEXT, fontsize=9)
    ax.set_xlim(0, 108)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    _style(ax, "CBI funds matched to a GLEIF LEI, by register")
    fig.tight_layout()
    fig.savefig(out / "match_rate_by_register.png", dpi=150)
    plt.close(fig)

    # 2 exceptions by rule
    rows = con.execute("""SELECT rule_id || ' ' || rule_name, COUNT(*) FROM gold_exception GROUP BY 1 ORDER BY 2""").fetchall()
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.barh([r[0][:58] for r in rows], [r[1] for r in rows], color=BLUE, height=0.6)
    for i, r in enumerate(rows):
        ax.text(r[1] * 1.01 + 5, i, f"{r[1]:,}", va="center", color=TEXT, fontsize=8)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    _style(ax, "Open exceptions by rule")
    fig.tight_layout()
    fig.savefig(out / "exceptions_by_rule.png", dpi=150)
    plt.close(fig)

    # 3 top 10 managers by number of portfolios (UCITS and AIF combined)
    rows = con.execute("""SELECT manager_name, SUM(n) n FROM gold_manager_concentration
                          WHERE manager_name NOT IN ('Internally managed','No ManCo / AIFM printed')
                          GROUP BY 1 ORDER BY n DESC LIMIT 10""").fetchall()[::-1]
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.barh([r[0][:48] for r in rows], [r[1] for r in rows], color=BLUE, height=0.6)
    for i, r in enumerate(rows):
        ax.text(r[1] + 8, i, f"{r[1]:,}", va="center", color=TEXT, fontsize=8)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    _style(ax, "Top 10 ManCos / AIFMs by portfolios managed")
    fig.tight_layout()
    fig.savefig(out / "top_managers.png", dpi=150)
    plt.close(fig)

    # 4 authorisation year of currently authorised portfolios
    rows = con.execute("SELECT auth_year, regime, portfolios_authorised FROM gold_auth_by_year WHERE auth_year <= 2026").fetchall()
    years = sorted({r[0] for r in rows})
    fig, ax = plt.subplots(figsize=(8, 3.6))
    for regime, col in [("UCITS", BLUE), ("AIF", ORANGE)]:
        d = {r[0]: r[2] for r in rows if r[1] == regime}
        ys = [d.get(y, 0) for y in years]
        ax.plot(years, ys, color=col, linewidth=2)
        ax.text(years[-1] + 0.3, ys[-1], regime, color=TEXT, fontsize=9, va="center")
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    _style(ax, "Authorised portfolios by year authorised (survivors only; 2026 to date)")
    fig.tight_layout()
    fig.savefig(out / "authorisations_by_year.png", dpi=150)
    plt.close(fig)
