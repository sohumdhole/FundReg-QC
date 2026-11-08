"""
FundReg-QC: GLEIF Golden Copy filter for Irish funds.

Downloads the latest GLEIF Golden Copy Level 1 (LEI-CDF) and Level 2
relationship (RR-CDF) CSV files (CC0 licence), streams them without
unzipping, and writes small filtered extracts for upload.

Usage (Python 3.9+, standard library only):
    python gleif_ie_filter.py
or, if you downloaded the zips manually from the GLEIF website:
    python gleif_ie_filter.py --l1 path\\to\\lei2.csv.zip --rr path\\to\\rr.csv.zip

Outputs (folder ./gleif_out):
    ie_funds_l1.csv          Irish entities with EntityCategory = FUND (full selected columns)
    ie_other_l1_slim.csv     Other Irish entities (slim columns), used to test fund miscategorisation
    ie_fund_rr.csv           Level 2 relationships touching an Irish fund
    rr_counterparties_l1.csv Level 1 records of managers/umbrellas/parents linked to Irish funds
    manifest.json            URLs, publish file names, sizes, SHA-256, row counts, filter rules
Any output above 18 MB is split into .partNN.csv files, each with its header.
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile

BASE = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/{t}/latest.csv"
RAW_DIR = "gleif_raw"
OUT_DIR = "gleif_out"
SPLIT_BYTES = 18 * 1024 * 1024

L1_REQUIRED = [
    "LEI",
    "Entity.LegalName",
    "Entity.LegalJurisdiction",
    "Entity.EntityCategory",
    "Entity.LegalAddress.Country",
    "Registration.RegistrationStatus",
]
L1_WISHLIST = L1_REQUIRED + [
    "Entity.EntitySubCategory",
    "Entity.EntityStatus",
    "Entity.LegalForm.EntityLegalFormCode",
    "Entity.LegalForm.OtherLegalForm",
    "Entity.OtherEntityNames.OtherEntityName.1",
    "Entity.OtherEntityNames.OtherEntityName.2",
    "Entity.LegalAddress.City",
    "Entity.HeadquartersAddress.Country",
    "Entity.RegistrationAuthority.RegistrationAuthorityID",
    "Entity.RegistrationAuthority.RegistrationAuthorityEntityID",
    "Entity.EntityCreationDate",
    "Entity.SuccessorEntity.SuccessorLEI",
    "Registration.InitialRegistrationDate",
    "Registration.LastUpdateDate",
    "Registration.NextRenewalDate",
    "Registration.ManagingLOU",
    "Registration.ValidationSources",
]
L1_SLIM = [
    "LEI",
    "Entity.LegalName",
    "Entity.LegalJurisdiction",
    "Entity.EntityCategory",
    "Entity.LegalForm.EntityLegalFormCode",
    "Entity.EntityStatus",
    "Registration.RegistrationStatus",
    "Registration.NextRenewalDate",
]

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(kind):
    os.makedirs(RAW_DIR, exist_ok=True)
    url = BASE.format(t=kind)
    print(f"[download] {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "FundReg-QC/1.0"})
    with urllib.request.urlopen(req) as resp:
        final_url = resp.geturl()
        name = final_url.rstrip("/").split("/")[-1] or f"{kind}.csv.zip"
        path = os.path.join(RAW_DIR, name)
        h = hashlib.sha256()
        total = 0
        with open(path, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                total += len(chunk)
                if total % (50 << 20) < (1 << 20):
                    print(f"  {total / 1e6:,.0f} MB")
    print(f"[download] saved {path} ({total / 1e6:,.1f} MB)")
    return {
        "kind": kind,
        "requested_url": url,
        "final_url": final_url,
        "file": name,
        "bytes": total,
        "sha256": h.hexdigest(),
        "downloaded_utc": utc_now(),
    }


def local(kind, path):
    return {
        "kind": kind,
        "requested_url": "manual download from https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy",
        "final_url": None,
        "file": os.path.basename(path),
        "bytes": os.path.getsize(path),
        "sha256": sha256_file(path),
        "downloaded_utc": None,
        "_path": path,
    }


def open_csv(zip_path):
    zf = zipfile.ZipFile(zip_path)
    members = [m for m in zf.namelist() if m.lower().endswith(".csv")]
    if len(members) != 1:
        raise SystemExit(f"Expected one CSV inside {zip_path}, found {members}")
    fh = io.TextIOWrapper(zf.open(members[0]), encoding="utf-8", newline="")
    return zf, fh, members[0]


class SplitWriter:
    def __init__(self, base_name, header):
        self.base = base_name
        self.header = header
        self.part = 0
        self.rows = 0
        self.files = []
        self._open()

    def _open(self):
        if self.part == 0:
            name = f"{self.base}.csv"
        else:
            name = f"{self.base}.part{self.part:02d}.csv"
        self.path = os.path.join(OUT_DIR, name)
        self.fh = open(self.path, "w", encoding="utf-8", newline="")
        self.w = csv.writer(self.fh)
        self.w.writerow(self.header)
        self.files.append(self.path)

    def write(self, row):
        self.w.writerow(row)
        self.rows += 1
        if self.rows % 5000 == 0 and self.fh.tell() > SPLIT_BYTES:
            self.fh.close()
            if self.part == 0:
                first = os.path.join(OUT_DIR, f"{self.base}.part00.csv")
                os.replace(self.path, first)
                self.files[-1] = first
            self.part += 1
            self._open()

    def close(self):
        self.fh.close()
        return {
            "rows": self.rows,
            "files": [
                {"file": os.path.basename(p), "bytes": os.path.getsize(p), "sha256": sha256_file(p)}
                for p in self.files
            ],
        }


def is_ie(row):
    juris = (row.get("Entity.LegalJurisdiction") or "").upper()
    country = (row.get("Entity.LegalAddress.Country") or "").upper()
    return juris == "IE" or juris.startswith("IE-") or country == "IE"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--l1", help="local Level 1 golden copy CSV zip")
    ap.add_argument("--rr", help="local Level 2 RR golden copy CSV zip")
    a = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    sources = []
    for kind, arg in (("lei2", a.l1), ("rr", a.rr)):
        if arg:
            s = local(kind, arg)
        else:
            try:
                s = download(kind)
                s["_path"] = os.path.join(RAW_DIR, s["file"])
            except Exception as e:
                raise SystemExit(
                    f"Download of {kind} failed ({e}). Download it manually from "
                    "https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy "
                    f"and rerun with --{'l1' if kind == 'lei2' else 'rr'} <path>"
                )
        sources.append(s)
    l1_path, rr_path = sources[0]["_path"], sources[1]["_path"]

    # Pass 1: Level 1, Irish entities
    zf, fh, member = open_csv(l1_path)
    sources[0]["csv_member"] = member
    reader = csv.DictReader(fh)
    missing = [c for c in L1_REQUIRED if c not in reader.fieldnames]
    if missing:
        print("Level 1 header:", reader.fieldnames[:60])
        raise SystemExit(f"Required Level 1 columns missing: {missing}")
    l1_cols = [c for c in L1_WISHLIST if c in reader.fieldnames]
    l1_absent = [c for c in L1_WISHLIST if c not in reader.fieldnames]
    funds_w = SplitWriter("ie_funds_l1", l1_cols)
    other_w = SplitWriter("ie_other_l1_slim", [c for c in L1_SLIM if c in reader.fieldnames])
    slim_cols = other_w.header
    fund_leis, ie_leis, total = set(), set(), 0
    print("[pass 1] scanning Level 1")
    for row in reader:
        total += 1
        if total % 500000 == 0:
            print(f"  {total:,} rows")
        if not is_ie(row):
            continue
        lei = row["LEI"]
        ie_leis.add(lei)
        if (row.get("Entity.EntityCategory") or "").upper() == "FUND":
            fund_leis.add(lei)
            funds_w.write([row.get(c, "") for c in l1_cols])
        else:
            other_w.write([row.get(c, "") for c in slim_cols])
    fh.close()
    zf.close()
    l1_total = total
    funds_info, other_info = funds_w.close(), other_w.close()
    print(f"[pass 1] {l1_total:,} rows; IE funds {len(fund_leis):,}; other IE {other_info['rows']:,}")

    # Pass 2: Level 2 relationships touching an Irish fund
    zf, fh, member = open_csv(rr_path)
    sources[1]["csv_member"] = member
    reader = csv.DictReader(fh)
    start_col = next((c for c in reader.fieldnames if c.endswith("StartNode.NodeID")), None)
    end_col = next((c for c in reader.fieldnames if c.endswith("EndNode.NodeID")), None)
    if not start_col or not end_col:
        print("RR header:", reader.fieldnames)
        raise SystemExit("Could not find StartNode/EndNode NodeID columns in RR file")
    rr_w = SplitWriter("ie_fund_rr", reader.fieldnames)
    counterparties, rr_total = set(), 0
    print("[pass 2] scanning Level 2 relationships")
    for row in reader:
        rr_total += 1
        s, e = row[start_col], row[end_col]
        if s in fund_leis or e in fund_leis:
            rr_w.write([row.get(c, "") for c in reader.fieldnames])
            for x in (s, e):
                if x not in fund_leis:
                    counterparties.add(x)
    fh.close()
    zf.close()
    rr_info = rr_w.close()
    print(f"[pass 2] {rr_total:,} rows; kept {rr_info['rows']:,}; counterparties {len(counterparties):,}")

    # Pass 3: Level 1 records of counterparties (managers, umbrellas, parents)
    zf, fh, _ = open_csv(l1_path)
    reader = csv.DictReader(fh)
    cp_w = SplitWriter("rr_counterparties_l1", l1_cols)
    print("[pass 3] fetching counterparty Level 1 records")
    for row in reader:
        if row["LEI"] in counterparties:
            cp_w.write([row.get(c, "") for c in l1_cols])
    fh.close()
    zf.close()
    cp_info = cp_w.close()

    for s in sources:
        s.pop("_path", None)
    manifest = {
        "generated_utc": utc_now(),
        "licence": "GLEIF LEI data, CC0 1.0 (https://www.gleif.org/en/meta/lei-data-terms-of-use)",
        "sources": sources,
        "filter_rules": {
            "irish": "Entity.LegalJurisdiction == 'IE' or startswith 'IE-' or Entity.LegalAddress.Country == 'IE'",
            "fund": "Entity.EntityCategory == 'FUND'",
            "rr": "StartNode or EndNode is an Irish FUND LEI",
            "counterparties": "Level 1 records of non-Irish-fund LEIs appearing in kept RR rows",
        },
        "level1_rows_scanned": l1_total,
        "rr_rows_scanned": rr_total,
        "level1_columns_kept": l1_cols,
        "level1_wishlist_columns_absent": l1_absent,
        "outputs": {
            "ie_funds_l1": funds_info,
            "ie_other_l1_slim": other_info,
            "ie_fund_rr": rr_info,
            "rr_counterparties_l1": cp_info,
        },
    }
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"[done] upload everything in ./{OUT_DIR} (not ./{RAW_DIR})")


if __name__ == "__main__":
    main()
