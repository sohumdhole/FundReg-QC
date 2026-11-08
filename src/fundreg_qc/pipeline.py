"""Run all steps end to end: python -m fundreg_qc.pipeline"""
import time

import duckdb

from . import config as C
from . import data_layer, match, exceptions, insights, audit


def run(rebuild_bronze=True):
    C.OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(C.DB_PATH))
    t = time.time()
    if rebuild_bronze:
        data_layer.run(con)
    else:
        data_layer.build_silver(con)
    print(f"[1] data layer {time.time() - t:.0f}s")
    match.run(con)
    print(f"[2] reconciliation {time.time() - t:.0f}s")
    exceptions.run(con)
    print(f"[3] exceptions {time.time() - t:.0f}s")
    insights.build(con)
    print(f"[4] insights {time.time() - t:.0f}s")
    a = audit.build(con)
    print(f"[audit] {C.AUDIT_PATH.name} written")
    return con, a


if __name__ == "__main__":
    import sys
    run(rebuild_bronze="--silver-only" not in sys.argv)
