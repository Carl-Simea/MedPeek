#!/usr/bin/env python3
"""Build the care-finder data files (US only) from free public sources:

- GeoNames US postal codes (CC BY 4.0): ZIP -> city, state, latitude, longitude.
- CMS "Hospital General Information" (public domain): every Medicare-certified hospital, with whether it
  has an emergency department.
- HIFLD Hospitals (public domain, via ArcGIS mirror): hospital coordinates, type, status and trauma level.

Writes app/geo/zips.csv.gz and app/geo/hospitals.json.gz (about 2 MB together; committed so nobody has
to rebuild them). Re-run now and then to refresh:

    .venv/bin/python scripts/build_places.py
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
GEO = BACKEND / "app" / "geo"

GEONAMES_URL = "https://download.geonames.org/export/zip/US.zip"
CMS_URL = "https://data.cms.gov/provider-data/api/1/datastore/query/xubh-q36u/0/download?format=csv"
HIFLD_URL = "https://services.arcgis.com/XG15cJAlne2vxtgt/ArcGIS/rest/services/Hospitals_hifld/FeatureServer/0/query"

STOP = {"THE", "OF", "AND", "HOSPITAL", "MEDICAL", "CENTER", "CENTRE", "HEALTH", "HEALTHCARE", "SYSTEM", "INC",
        "LLC", "REGIONAL", "COMMUNITY", "MEMORIAL", "GENERAL", "SAINT", "ST", "UNIVERSITY", "CAMPUS", "DBA"}


def tokens(name: str) -> set[str]:
    return {t for t in re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split() if t not in STOP and len(t) > 1}


def street_key(address: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", address.upper().split(",")[0])[:14]


def load_geonames(client: httpx.Client) -> list[dict]:
    data = client.get(GEONAMES_URL).content
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        text = zf.read("US.txt").decode("utf-8")
    rows = []
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) < 11 or not f[9] or not f[10]:
            continue
        rows.append({"zip": f[1], "city": f[2], "state": f[4], "lat": float(f[9]), "lon": float(f[10])})
    return rows


def load_cms(client: httpx.Client) -> list[dict]:
    r = client.get(CMS_URL)
    r.raise_for_status()
    text = r.text
    if not text.lstrip().startswith('"Facility ID"'):
        raise RuntimeError("CMS hospital download didn't return the CSV: " + text[:120])
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        out.append({
            "id": r["Facility ID"], "name": r["Facility Name"].strip(), "address": r["Address"].strip(),
            "city": r["City/Town"].strip(), "state": r["State"].strip(), "zip": r["ZIP Code"].strip()[:5],
            "phone": r["Telephone Number"].strip(), "type": r["Hospital Type"].strip(),
            "emergency": r["Emergency Services"].strip().lower() == "yes",
            "rating": r.get("Hospital overall rating", "").strip(),
        })
    return out


def load_hifld(client: httpx.Client) -> list[dict]:
    out, offset = [], 0
    while True:
        r = client.get(HIFLD_URL, params={
            "where": "1=1", "outFields": "NAME,ADDRESS,CITY,STATE,ZIP,TELEPHONE,TYPE,STATUS,TRAUMA,LATITUDE,LONGITUDE",
            "f": "json", "resultOffset": offset, "resultRecordCount": 2000, "outSR": 4326,
        })
        r.raise_for_status()
        feats = r.json().get("features", [])
        for f in feats:
            a = f["attributes"]
            g = f.get("geometry") or {}
            lat, lon = a.get("LATITUDE") or g.get("y"), a.get("LONGITUDE") or g.get("x")
            if lat and lon:
                out.append({k.lower(): (v.strip() if isinstance(v, str) else v) for k, v in a.items()} | {"lat": lat, "lon": lon})
        if len(feats) < 2000:
            break
        offset += len(feats)
    return out


def main() -> None:
    GEO.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MedPeek care finder build)", "Accept": "*/*"}
    with httpx.Client(timeout=120.0, follow_redirects=True, headers=headers) as client:
        print("downloading ZIP codes ...", flush=True)
        zips = load_geonames(client)
        print("downloading CMS hospitals ...", flush=True)
        cms = load_cms(client)
        print("downloading hospital coordinates ...", flush=True)
        hifld = load_hifld(client)
    print(f"{len(zips)} ZIPs, {len(cms)} CMS hospitals, {len(hifld)} located hospitals")

    centroid = {z["zip"]: (z["lat"], z["lon"]) for z in zips}
    by_zip: dict[str, list[dict]] = {}
    for h in hifld:
        if h.get("status", "OPEN") == "CLOSED":
            continue
        by_zip.setdefault(str(h.get("zip", ""))[:5], []).append(h)

    hospitals, matched = [], 0
    for h in cms:
        if h["type"] in ("Psychiatric",):
            continue
        best, best_score = None, 0.0
        for cand in by_zip.get(h["zip"], []):
            if street_key(cand.get("address", "")) and street_key(cand["address"]) == street_key(h["address"]):
                best, best_score = cand, 2.0
                break
            a, b = tokens(h["name"]), tokens(cand.get("name", ""))
            score = len(a & b) / max(1, min(len(a), len(b))) if a and b else 0.0
            if score > best_score:
                best, best_score = cand, score
        if best and best_score >= 0.5:
            lat, lon, approx, trauma = best["lat"], best["lon"], False, best.get("trauma") or ""
            matched += 1
        elif h["zip"] in centroid:
            (lat, lon), approx, trauma = centroid[h["zip"]], True, ""
        else:
            continue
        hospitals.append({
            "id": h["id"], "name": h["name"].title(), "address": h["address"].title(), "city": h["city"].title(),
            "state": h["state"], "zip": h["zip"], "phone": h["phone"], "type": h["type"],
            "emergency": h["emergency"], "rating": h["rating"], "lat": round(lat, 5), "lon": round(lon, 5),
            "approx": approx, "trauma": "" if trauma in ("NOT AVAILABLE", "NONE") else trauma.title(),
        })
    print(f"{len(hospitals)} hospitals kept, {matched} with exact coordinates, "
          f"{sum(h['emergency'] for h in hospitals)} with an emergency department")

    with gzip.open(GEO / "zips.csv.gz", "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["zip", "city", "state", "lat", "lon"])
        for z in zips:
            w.writerow([z["zip"], z["city"], z["state"], z["lat"], z["lon"]])
    with gzip.open(GEO / "hospitals.json.gz", "wt") as fh:
        json.dump(hospitals, fh, separators=(",", ":"))
    for name in ("zips.csv.gz", "hospitals.json.gz"):
        print(f"  app/geo/{name}: {(GEO / name).stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    sys.exit(main())
