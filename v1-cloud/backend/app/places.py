"""Care finder: from a US ZIP code to the nearest emergency rooms, urgent care and specialists.

Data (all free, no API key):
- app/geo/zips.csv.gz        GeoNames US postal codes -> city, state, coordinates (CC BY 4.0)
- app/geo/hospitals.json.gz  CMS hospital list (emergency department yes/no) + HIFLD coordinates (public domain)
- NPI Registry API            every licensed US clinician/clinic by specialty and ZIP (CMS, public domain), live

Distances are straight-line miles from the ZIP code's center. Providers come with the address and phone
they registered with CMS; a provider's map pin is their ZIP code's center (good to about a mile).
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import math
import re
import threading
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

import httpx

log = logging.getLogger("medpeek.places")
GEO = Path(__file__).resolve().parent / "geo"
NPI_URL = "https://npiregistry.cms.hhs.gov/api/"
NPI_TIMEOUT = 8.0

# Which registry specialty to look for, for each specialist MedPeek can refer to.
# search: the NPI taxonomy search term; match: words that must appear in the provider's taxonomy
# description (the registry's search also returns loosely related providers); plural: label.
SPECIALTY = {
    "Primary care doctor": {"search": ["Family Medicine", "Internal Medicine"], "match": ["family medicine", "internal medicine", "general practice"], "plural": "primary care doctors"},
    "Orthopedic surgeon (orthopedics)": {"search": ["Orthopaedic Surgery"], "match": ["orthopaedic"], "plural": "orthopedic surgeons"},
    "Orthopedic spine specialist": {"search": ["Orthopaedic Surgery of the Spine", "Orthopaedic Surgery"], "match": ["spine", "orthopaedic"], "plural": "spine specialists"},
    "Hand surgeon": {"search": ["Hand Surgery", "Surgery of the Hand"], "match": ["hand"], "plural": "hand surgeons"},
    "Pediatric orthopedic surgeon": {"search": ["Pediatric Orthopaedic Surgery", "Orthopaedic Surgery"], "match": ["pediatric orthopaedic", "orthopaedic"], "plural": "pediatric orthopedic surgeons"},
    "Sports medicine doctor": {"search": ["Sports Medicine"], "match": ["sports medicine"], "plural": "sports medicine doctors"},
    "Rheumatologist": {"search": ["Rheumatology"], "match": ["rheumatology"], "plural": "rheumatologists"},
    "Orthopedic oncologist": {"search": ["Orthopaedic Surgery", "Medical Oncology"], "match": ["orthopaedic", "oncology"], "plural": "orthopedic and cancer specialists"},
    "Oncologist (cancer specialist)": {"search": ["Medical Oncology"], "match": ["oncology"], "plural": "oncologists"},
    "Pulmonologist (lung doctor)": {"search": ["Pulmonary Disease"], "match": ["pulmonary"], "plural": "lung doctors"},
    "Cardiologist (heart doctor)": {"search": ["Cardiovascular Disease"], "match": ["cardiovascular", "cardiology"], "plural": "cardiologists"},
    "Pediatrician": {"search": ["Pediatrics"], "match": ["pediatrics"], "plural": "pediatricians"},
    "Pediatric pulmonologist": {"search": ["Pediatric Pulmonology", "Pediatrics"], "match": ["pediatric pulmonology", "pediatrics"], "plural": "pediatric lung doctors"},
    "Gastroenterologist": {"search": ["Gastroenterology"], "match": ["gastroenterology"], "plural": "gastroenterologists"},
    "Urologist": {"search": ["Urology"], "match": ["urology"], "plural": "urologists"},
    "Nephrologist (kidney doctor)": {"search": ["Nephrology"], "match": ["nephrology"], "plural": "kidney doctors"},
    "General surgeon": {"search": ["Surgery"], "match": ["surgery"], "plural": "general surgeons"},
    "Breast surgeon": {"search": ["Surgical Oncology", "Surgery"], "match": ["surgical oncology", "surgery"], "plural": "breast surgeons"},
    "Dentist": {"search": ["Dentist"], "match": ["dentist"], "plural": "dentists"},
    "Endodontist (root canal specialist)": {"search": ["Endodontics"], "match": ["endodontics"], "plural": "endodontists"},
    "Oral and maxillofacial surgeon": {"search": ["Oral and Maxillofacial Surgery"], "match": ["maxillofacial"], "plural": "oral surgeons"},
    "Orthodontist": {"search": ["Orthodontics"], "match": ["orthodontics"], "plural": "orthodontists"},
    "ENT specialist (otolaryngologist)": {"search": ["Otolaryngology"], "match": ["otolaryngology"], "plural": "ENT specialists"},
    "Neurologist": {"search": ["Neurology"], "match": ["neurology"], "plural": "neurologists"},
    "Neurosurgeon": {"search": ["Neurological Surgery"], "match": ["neurological surgery"], "plural": "neurosurgeons"},
}
URGENT_CARE = {"search": ["Urgent Care"], "match": ["urgent care"], "plural": "urgent care centers"}


class PlacesError(ValueError):
    """A problem the patient can fix (bad ZIP code)."""


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@lru_cache(maxsize=1)
def _zips() -> dict[str, dict]:
    out: dict[str, dict] = {}
    with gzip.open(GEO / "zips.csv.gz", "rt") as fh:
        for r in csv.DictReader(fh):
            out[r["zip"]] = {"zip": r["zip"], "city": r["city"], "state": r["state"], "lat": float(r["lat"]), "lon": float(r["lon"])}
    return out


@lru_cache(maxsize=1)
def _hospitals() -> list[dict]:
    with gzip.open(GEO / "hospitals.json.gz", "rt") as fh:
        return json.load(fh)


def ready() -> bool:
    return (GEO / "zips.csv.gz").exists() and (GEO / "hospitals.json.gz").exists()


def find_zip_in_text(text: str) -> str | None:
    """A 5-digit US ZIP code mentioned in free text ('I'm in 11218' / 'zip 11218-1234')."""
    for m in re.finditer(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)", text or ""):
        if m.group(1) in _zips():
            return m.group(1)
    return None


def lookup_zip(zip_code: str) -> dict:
    z = re.sub(r"\D", "", str(zip_code or ""))[:5]
    if len(z) != 5:
        raise PlacesError("Please enter a 5-digit US ZIP code.")
    info = _zips().get(z)
    if not info:
        raise PlacesError(f"ZIP code {z} isn't in our US ZIP code list. Please check it.")
    return info


def nearest_zips(lat: float, lon: float, k: int = 12, max_miles: float = 25.0) -> list[str]:
    scored = []
    for z in _zips().values():
        if abs(z["lat"] - lat) > 0.6 or abs(z["lon"] - lon) > 0.8:
            continue
        d = haversine_miles(lat, lon, z["lat"], z["lon"])
        if d <= max_miles:
            scored.append((d, z["zip"]))
    scored.sort()
    return [z for _, z in scored[:k]]


def _directions(place: dict) -> dict:
    dest = quote_plus(f"{place['name']}, {place['address']}, {place['city']}, {place['state']} {place['zip']}")
    return {
        "directions_url": f"https://www.google.com/maps/dir/?api=1&destination={dest}",
        "apple_maps_url": f"https://maps.apple.com/?daddr={dest}",
    }


def nearby_hospitals(lat: float, lon: float, k: int = 3, emergency_only: bool = True, max_miles: float = 75.0) -> list[dict]:
    found = []
    for h in _hospitals():
        if emergency_only and not h["emergency"]:
            continue
        if abs(h["lat"] - lat) > 1.5 or abs(h["lon"] - lon) > 2.0:
            continue
        d = haversine_miles(lat, lon, h["lat"], h["lon"])
        if d <= max_miles:
            found.append((d, h))
    found.sort(key=lambda x: x[0])
    out = []
    for d, h in found[:k]:
        details = [h["type"].replace("Hospitals", "hospital").replace("Acute Care", "Acute care")]
        if h.get("trauma"):
            details.append(f"Trauma center: {h['trauma']}")
        if h.get("rating") and h["rating"].isdigit():
            details.append(f"CMS rating {h['rating']}/5")
        out.append({
            "kind": "emergency" if h["emergency"] else "hospital",
            "name": h["name"], "address": h["address"], "city": h["city"], "state": h["state"], "zip": h["zip"],
            "phone": h["phone"], "lat": h["lat"], "lon": h["lon"], "approx": h["approx"],
            "distance_miles": round(d, 1), "details": " · ".join(details),
            "open_24_7": h["emergency"], "source": "CMS Hospital Compare + HIFLD",
            **_directions(h),
        })
    return out


# ---------------------------------------------------------------- NPI registry (live)
_npi_cache: dict[tuple, tuple[float, list[dict]]] = {}
_npi_lock = threading.Lock()


def _npi_query(postal_code: str, search: str) -> list[dict]:
    key = (postal_code, search)
    with _npi_lock:
        hit = _npi_cache.get(key)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
    try:
        r = httpx.get(NPI_URL, params={"version": "2.1", "postal_code": postal_code, "taxonomy_description": search,
                                       "limit": 200}, timeout=NPI_TIMEOUT)
        r.raise_for_status()
        results = r.json().get("results") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("NPI registry lookup failed for %s/%s: %s", postal_code, search, exc)
        results = []
    with _npi_lock:
        _npi_cache[key] = (time.time(), results)
        if len(_npi_cache) > 2000:
            _npi_cache.pop(next(iter(_npi_cache)))
    return results


def _provider_record(raw: dict, match_words: list[str], origin: tuple[float, float], kind: str) -> dict | None:
    taxonomies = raw.get("taxonomies") or []
    descs = [t.get("desc", "") for t in taxonomies]
    matching = [d for d in descs if any(w in d.lower() for w in match_words)]
    if not matching:
        return None
    primary = next((t.get("desc") for t in taxonomies if t.get("primary")), None)
    specialty = primary if primary in matching else matching[0]
    loc = next((a for a in raw.get("addresses", []) if a.get("address_purpose") == "LOCATION"), None)
    if not loc or loc.get("country_code", "US") != "US":
        return None
    zip5 = str(loc.get("postal_code", ""))[:5]
    z = _zips().get(zip5)
    if not z:
        return None
    basic = raw.get("basic") or {}
    if raw.get("enumeration_type") == "NPI-1":
        parts = [basic.get("name_prefix", ""), basic.get("first_name", ""), basic.get("last_name", "")]
        name = " ".join(p.title() for p in parts if p and p != "--")
        if basic.get("credential"):
            name += f", {basic['credential'].replace('.', '')}"
    else:
        name = (basic.get("organization_name") or "").title()
    if not name.strip():
        return None
    place = {
        "kind": kind, "name": name.strip(), "specialty": specialty,
        "address": (loc.get("address_1") or "").title() + (f", {loc['address_2'].title()}" if loc.get("address_2") else ""),
        "city": (loc.get("city") or "").title(), "state": loc.get("state", ""), "zip": zip5,
        "phone": loc.get("telephone_number", ""), "lat": z["lat"], "lon": z["lon"], "approx": True,
        "distance_miles": round(haversine_miles(origin[0], origin[1], z["lat"], z["lon"]), 1),
        "npi": raw.get("number", ""), "source": "NPI Registry (CMS)",
        "details": specialty + (" · clinic" if raw.get("enumeration_type") == "NPI-2" else ""),
    }
    place.update(_directions(place))
    return place


def nearby_providers(zip_code: str, spec: dict, k: int = 3, kind: str = "specialist") -> list[dict]:
    """Closest matching clinicians/clinics: the patient's ZIP first, then neighboring ZIPs."""
    origin = lookup_zip(zip_code)
    seen: set[str] = set()
    found: list[dict] = []
    for z in [origin["zip"]] + nearest_zips(origin["lat"], origin["lon"], k=10):
        for search in spec["search"]:
            for raw in _npi_query(z, search):
                if raw.get("number") in seen:
                    continue
                rec = _provider_record(raw, spec["match"], (origin["lat"], origin["lon"]), kind)
                if rec:
                    seen.add(raw["number"])
                    found.append(rec)
        if len(found) >= max(k * 3, 6):
            break
    if not found:  # last resort: the whole 3-digit ZIP area
        for search in spec["search"]:
            for raw in _npi_query(origin["zip"][:3] + "*", search):
                if raw.get("number") not in seen:
                    rec = _provider_record(raw, spec["match"], (origin["lat"], origin["lon"]), kind)
                    if rec:
                        seen.add(raw["number"])
                        found.append(rec)
    found.sort(key=lambda p: (p["distance_miles"], p["name"]))
    return found[:k]


def find_care(zip_code: str, specialist: str | None = None, urgency: str = "soon", limit: int = 3,
              include: tuple[str, ...] = ("emergency", "urgent_care", "specialist")) -> dict:
    """Top-N places for each kind of help the patient may need, closest first."""
    origin = lookup_zip(zip_code)
    limit = max(1, min(int(limit or 3), 5))
    groups = []
    if "emergency" in include:
        places = nearby_hospitals(origin["lat"], origin["lon"], k=limit, emergency_only=True)
        groups.append({"kind": "emergency", "title": "Emergency rooms (open 24/7)",
                       "why": "For severe pain, a bone out of place, trouble breathing or anything that can't wait.",
                       "places": places})
    if "urgent_care" in include and urgency != "emergency":
        groups.append({"kind": "urgent_care", "title": "Urgent care centers",
                       "why": "Walk-in care for problems that need attention today but aren't life-threatening.",
                       "places": nearby_providers(origin["zip"], URGENT_CARE, k=limit, kind="urgent_care")})
    spec = SPECIALTY.get(specialist or "")
    if "specialist" in include and spec:
        groups.append({"kind": "specialist", "title": f"Nearby {spec['plural']}", "specialist": specialist,
                       "why": "The kind of doctor who usually treats this. Call to ask for the earliest appointment.",
                       "places": nearby_providers(origin["zip"], spec, k=limit)})
    if urgency == "emergency":
        groups.sort(key=lambda g: g["kind"] != "emergency")
    elif spec:
        groups.sort(key=lambda g: {"specialist": 0, "urgent_care": 1, "emergency": 2}[g["kind"]])
    return {
        "zip": origin["zip"], "city": origin["city"], "state": origin["state"],
        "lat": origin["lat"], "lon": origin["lon"], "groups": groups,
        "note": ("Distances are straight-line miles from the center of your ZIP code. Doctor pins show their ZIP "
                 "code area; the address and directions link are exact. Call ahead to confirm hours and insurance. "
                 "If this is an emergency, call 911."),
    }
