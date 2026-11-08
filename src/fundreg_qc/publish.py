"""Publish deliverables from the built database: exception queue CSV, Excel pack,
Power BI export, data manifest, and docs rendered from the audit record."""
import json
import shutil

import duckdb

from . import config as C
from . import excel_pack, powerbi_export, render_docs


def run():
    con = duckdb.connect(str(C.DB_PATH))
    con.execute(f"COPY (SELECT * FROM gold_exception ORDER BY exception_id) TO '{C.OUT / 'exception_queue.csv'}' (HEADER)")
    con.execute(f"COPY (SELECT * FROM gold_rule_catalogue ORDER BY rule_id) TO '{C.OUT / 'rule_catalogue.csv'}' (HEADER)")
    p = excel_pack.build(con)
    counts = powerbi_export.export(con)
    con.close()
    shutil.copy(C.OUT / "data_manifest.json", C.ROOT / "data" / "manifest.json")
    render_docs.render()
    print("published:", p.name, json.dumps(counts))


if __name__ == "__main__":
    run()
