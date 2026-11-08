"""Step 6: render docs from templates and the audit record.

Placeholders: {{dotted.path}} or {{dotted.path|fmt}} with fmt in int, pct, raw.
list indices are allowed in the path (registers.0.as_at). Each substituted value is
written to outputs/doc_number_trace.json so tests can trace every number in the docs.
"""
from __future__ import annotations

import json
import re

from . import config as C

PH = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)(?:\|(\w+))?\s*\}\}")
TEMPLATES = {"README.md.tmpl": "README.md", "methodology.md.tmpl": "docs/methodology.md",
             "primer.md.tmpl": "docs/primer.md", "limitations.md.tmpl": "docs/limitations.md",
             "powerbi_build_guide.md.tmpl": "docs/powerbi_build_guide.md"}


def lookup(a, path):
    cur = a
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def fmt(v, f):
    if f == "int":
        return f"{int(v):,}"
    if f == "len":
        return str(len(v))
    if f == "pct":
        return f"{float(v):.1f}%"
    return str(v)


def table_blocks(a):
    """Pre-built markdown tables, inserted with {{table.<name>}}."""
    t = {}
    rows = ["| Register | As at | Printed top-level | Parsed | Printed standalone + sub-funds | Parsed | Status |",
            "|---|---|---:|---:|---:|---:|---|"]
    for r in a["registers"]:
        ok = r["top_level_variance"] == 0 and r["standalone_plus_subfunds_variance"] == 0
        rows.append(f"| {r['label']} | {r['as_at']} | {r['printed_top_level']:,} | {r['parsed_top_level']:,} | "
                    f"{r['printed_standalone_plus_subfunds']:,} | {r['parsed_standalone_plus_subfunds']:,} | "
                    f"{'exact' if ok else 'variance ' + format(r['standalone_plus_subfunds_variance'], '+d')} |")
    t["reconciliation"] = "\n".join(rows)
    rows = ["| Register | Funds | Accepted | Match rate |", "|---|---:|---:|---:|"]
    for r in sorted(a["match_by_register"], key=lambda r: -r["accepted_pct"]):
        rows.append(f"| {r['label']} | {r['funds']:,} | {r['accepted']:,} | {r['accepted_pct']:.1f}% |")
    t["match_by_register"] = "\n".join(rows)
    rows = ["| Rule | Exception | Severity | Owner queue | Open |", "|---|---|---|---|---:|"]
    for k, v in a["exceptions"]["by_rule"].items():
        rows.append(f"| {k} | {v['name']} | {v['severity']} | {v['owner']} | {v['n']:,} |")
    t["exceptions"] = "\n".join(rows)
    rows = ["| Stratum | Sampled | Correct | Uncertain | Incorrect | Precision (strict) |", "|---|---:|---:|---:|---:|---:|"]
    for k in ["EXACT_NAME", "EXACT_IN_UMBRELLA", "EXACT_UNIQUE_SUBFUND", "FUZZY_ACCEPTED", "FUZZY_REVIEW_BAND"]:
        v = a["review"][k]
        rows.append(f"| {k} | {v['n']} | {v['correct']} | {v['uncertain']} | {v['incorrect']} | {v['precision_strict_pct']:.1f}% |")
    t["review"] = "\n".join(rows)
    rows = ["| # | Manager (CBI register) | Portfolios |", "|---:|---|---:|"]
    for i, r in enumerate(a["market"]["top10_managers"], 1):
        rows.append(f"| {i} | {r['manager']} | {r['portfolios']:,} |")
    t["top_managers"] = "\n".join(rows)
    return t


def render(a=None, write=True):
    a = a or json.loads(C.AUDIT_PATH.read_text())
    tables = table_blocks(a)
    trace, outputs = [], {}
    for tmpl, target in TEMPLATES.items():
        src = (C.ROOT / "docs" / "templates" / tmpl).read_text(encoding="utf-8")

        def sub(m):
            path, f = m.group(1), m.group(2) or "raw"
            if path.startswith("table."):
                val = tables[path[6:]]
                trace.append({"doc": target, "key": path, "value": "table"})
                return val
            val = fmt(lookup(a, path), f)
            trace.append({"doc": target, "key": path, "value": val})
            return val

        outputs[target] = PH.sub(sub, src)
    if write:
        for target, text in outputs.items():
            (C.ROOT / target).write_text(text, encoding="utf-8")
        (C.OUT / "doc_number_trace.json").write_text(json.dumps(trace, indent=1))
    return outputs, trace, tables


if __name__ == "__main__":
    render()
