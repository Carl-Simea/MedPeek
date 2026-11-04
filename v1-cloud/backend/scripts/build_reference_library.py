#!/usr/bin/env python3
"""Build the verified reference library that Gemma 4 checks every patient image against.

Sources are open, labeled X-ray / MRI / CT datasets (21 sources, see SOURCES_INFO). Files are
downloaded to --datasets (default /tmp/datasets, resumable) and capped by MAX_SOURCE_GB (default 25).
Big optional archives (NIH tarballs, GRAZPEDWRI parts, DENTEX, KUB-StoneX, MURA, Mammo-MX B1-B5 ...)
are used only when already downloaded, e.g. with scripts/fetch_sources.py. Images stay on this
machine under backend/data/reference/ (git-ignored); nothing from the datasets is committed.
Optional build-only libraries: pip install -r scripts/requirements-build.txt (h5py, SimpleITK).

Run from backend/:
    .venv/bin/python scripts/build_reference_library.py --dry-run      # count what would be added
    .venv/bin/python scripts/build_reference_library.py --clean        # (re)build everything
    .venv/bin/python scripts/build_reference_library.py --sources fracatlas,iu_chest --per-class 10

Then reload the running API:
    curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/api/references/reload
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import shutil
import sys
import threading
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

import httpx

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.config import settings  # noqa: E402
from app.references import ReferenceLibrary  # noqa: E402

# Largest single source file we will download. Was 1 GB for the hackathon build; raised for the
# "8 GB more per body part" expansion. Override with MAX_SOURCE_GB=... if needed.
MAX_SOURCE_BYTES = int(float(os.environ.get("MAX_SOURCE_GB", "25")) * 1_000_000_000)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".dcm", ""}  # Mammo-MX DICOMs have no extension

FILES = {
    "FracAtlas.zip": "https://ndownloader.figshare.com/files/65518038",
    "OrthoFrac-XR.zip": "https://ndownloader.figshare.com/files/68010862",
    "BTXRD.zip": "https://ndownloader.figshare.com/files/50653575",
    "BDCXR-3257.zip": "https://ndownloader.figshare.com/files/64149946",
    "IU_chest_test.parquet": "https://huggingface.co/datasets/MLforHealthcare/Indiana_University_Chest_X-ray_Collection/resolve/main/data/test-00000-of-00001.parquet",
    "IU_chest_train0.parquet": "https://huggingface.co/datasets/MLforHealthcare/Indiana_University_Chest_X-ray_Collection/resolve/main/data/train-00000-of-00003.parquet",
    "IU_chest_train1.parquet": "https://huggingface.co/datasets/MLforHealthcare/Indiana_University_Chest_X-ray_Collection/resolve/main/data/train-00001-of-00003.parquet",
    "ROCOv2_valid_images.zip": "https://zenodo.org/api/records/10821435/files/valid_images.zip/content",
    "ROCOv2_valid_captions.csv": "https://zenodo.org/api/records/10821435/files/valid_captions.csv/content",
    "ROCOv2_valid_concepts_manual.csv": "https://zenodo.org/api/records/10821435/files/valid_concepts_manual.csv/content",
    "ROCOv2_test_images.zip": "https://zenodo.org/api/records/10821435/files/test_images.zip/content",
    "ROCOv2_test_captions.csv": "https://zenodo.org/api/records/10821435/files/test_captions.csv/content",
    "ROCOv2_test_concepts_manual.csv": "https://zenodo.org/api/records/10821435/files/test_concepts_manual.csv/content",
    "ROCOv2_train_images.zip": "https://zenodo.org/api/records/10821435/files/train_images.zip/content",
    "ROCOv2_train_captions.csv": "https://zenodo.org/api/records/10821435/files/train_captions.csv/content",
    "ROCOv2_train_concepts_manual.csv": "https://zenodo.org/api/records/10821435/files/train_concepts_manual.csv/content",
    "MammoMX_B1.zip": "https://zenodo.org/api/records/17740027/files/B1.zip/content",
    "MammoMX_B3.zip": "https://zenodo.org/api/records/17740027/files/B3.zip/content",
    "MammoMX_B4.zip": "https://zenodo.org/api/records/17740027/files/B4.zip/content",
    "MammoMX_B5.zip": "https://zenodo.org/api/records/17740027/files/B5.zip/content",
    "MammoMX_B6.zip": "https://zenodo.org/api/records/17740027/files/B6.zip/content",
    "MammoMX_B0.zip": "https://zenodo.org/api/records/17740027/files/B0.zip/content",
    "MammoMX_Metadata.csv": "https://zenodo.org/api/records/17740027/files/Metadata.csv/content",
}

SOURCES_INFO = {
    "fracatlas": ("FracAtlas", "CC BY 4.0", "https://doi.org/10.6084/m9.figshare.22363012"),
    "orthofrac": ("OrthoFrac-XR", "CC BY 4.0", "https://figshare.com (OrthoFrac-XR)"),
    "btxrd": ("BTXRD", "CC BY 4.0", "https://doi.org/10.6084/m9.figshare.27865398"),
    "bdcxr": ("BDCXR-3257", "CC BY 4.0", "https://figshare.com (BDCXR-3257)"),
    "iu_chest": ("Indiana University Chest X-rays", "CC BY-NC-ND 4.0", "https://openi.nlm.nih.gov"),
    "rocov2": ("ROCOv2", "CC BY-NC 4.0", "https://doi.org/10.5281/zenodo.10821435"),
    "mammo_mx": ("Mammo-MX", "CC BY 4.0", "https://doi.org/10.5281/zenodo.17740027"),
}

# How many cases to keep per (body part, condition) = per_class x factor.
CAP_FACTOR = {"btxrd": 0.5, "orthofrac": 0.8, "bdcxr": 1.5, "mammo_mx": 1.0}


@dataclass
class Candidate:
    source: str
    body_part: str
    label: str  # "normal" or "abnormal"
    diagnosis: str  # "" for normal
    report: str
    verified_by: str
    filename: str
    read: Callable[[], bytes]
    modality: str = "xray"


# ---------------------------------------------------------------- helpers
def norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(s).lower()).strip()


def flag(v) -> bool:
    return str(v).strip().lower() in ("1", "1.0", "true", "yes")


def view_phrase(row: dict) -> str:
    views = [k for k in ("frontal", "lateral", "oblique") if flag(row.get(k, ""))]
    return (" and ".join(views).capitalize() + " ") if views else ""


def clean(text: str, limit: int = 600) -> str:
    text = re.sub(r"X{3,}", "[removed]", str(text or ""))
    return " ".join(text.split())[:limit]


NEGATION_START = re.compile(r"^(no|without|negative|absence|absent|not|there (is|are|was|were) no|resolution|resolved)\b")
NEGATION_PHRASE = re.compile(r"\b(no|without|negative for|absence of|ruled out|free of)\b[^,;.]*")


def positive_text(text: str) -> str:
    """Drop negated statements ('No fracture.', 'without effusion') before keyword matching."""
    kept = []
    for sentence in re.split(r"(?<=[.;!?])\s+|\n", text.lower()):
        s = sentence.strip()
        if not s or NEGATION_START.match(s):
            continue
        kept.append(NEGATION_PHRASE.sub(" ", s))
    return " ".join(kept)


def first_match(text: str, table: list[tuple[str, list[str]]]) -> str | None:
    for name, patterns in table:
        if any(re.search(p, text) for p in patterns):
            return name
    return None


class Datasets:
    """Finds each source file locally, or downloads it (resumable, capped by MAX_SOURCE_GB)."""

    def __init__(self, folder: Path, remote: bool):
        self.folder = folder
        self.remote = remote
        self.folder.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        path = self.folder / name
        if path.exists() and path.stat().st_size > 0:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise RuntimeError(f"{name} is {path.stat().st_size / 1e9:.2f} GB, over the MAX_SOURCE_GB limit")
            return path
        url = FILES[name]
        print(f"  downloading {name} ...", flush=True)
        tmp = path.with_name(path.name + ".part")
        done = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": "Mozilla/5.0 (compatible; MedPeek reference-library build)"}
        if done:
            headers["Range"] = f"bytes={done}-"
        with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                          timeout=httpx.Timeout(60.0, read=600.0)) as r:
            if done and r.status_code != 206:  # server ignored the resume request: start over
                done = 0
            r.raise_for_status()
            size = int(r.headers.get("content-length") or 0) + done
            if size > MAX_SOURCE_BYTES:
                raise RuntimeError(f"{name} is {size / 1e9:.2f} GB, over the MAX_SOURCE_GB limit - skipped")
            with open(tmp, "ab" if done else "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    done += len(chunk)
                    if done > MAX_SOURCE_BYTES:
                        raise RuntimeError(f"{name} is over the MAX_SOURCE_GB limit - skipped")
                    f.write(chunk)
        tmp.rename(path)
        print(f"  {name}: {path.stat().st_size / 1e6:.0f} MB", flush=True)
        return path

    def zip(self, name: str) -> zipfile.ZipFile:
        local = self.folder / name
        if self.remote and not local.exists():
            from remotezip import RemoteZip  # reads only the needed parts over HTTP

            with httpx.stream("GET", FILES[name], follow_redirects=True, timeout=60.0) as r:
                size = int(r.headers.get("content-length") or 0)
            if size > MAX_SOURCE_BYTES:
                raise RuntimeError(f"{name} is {size / 1e9:.2f} GB, over the MAX_SOURCE_GB limit")
            return RemoteZip(FILES[name])
        return zipfile.ZipFile(self.path(name))

    def text(self, name: str) -> str:
        return self.path(name).read_text(encoding="utf-8-sig", errors="replace")


_zip_lock = threading.Lock()


def zip_reader(zf: zipfile.ZipFile, member: str) -> Callable[[], bytes]:
    def read() -> bytes:
        with _zip_lock:
            return zf.read(member)

    return read


def index_zip(zf: zipfile.ZipFile) -> dict[str, str]:
    """Map normalized file stem -> member name, for image files only."""
    idx: dict[str, str] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        p = PurePosixPath(info.filename)
        if p.suffix.lower() in IMAGE_SUFFIXES and not p.name.startswith("."):
            idx.setdefault(norm(p.stem), info.filename)
    return idx


# ---------------------------------------------------------------- FracAtlas (fractures)
def load_fracatlas(ds: Datasets) -> Iterator[Candidate]:
    zf = ds.zip("FracAtlas.zip")
    idx = index_zip(zf)
    regions = {
        "hand": ("wrist_hand", "hand and wrist"),
        "leg": ("lower_leg_foot", "leg"),
        "hip": ("pelvis_hip", "hip"),
        "shoulder": ("shoulder_upper_arm", "shoulder"),
    }
    for r in csv.DictReader(io.StringIO(zf.read("FracAtlas/dataset.csv").decode("utf-8-sig"))):
        hits = [k for k in regions if flag(r[k])]
        if flag(r["mixed"]) or len(hits) != 1:
            continue
        member = idx.get(norm(PurePosixPath(r["image_id"]).stem))
        if not member:
            continue
        part, place = regions[hits[0]]
        what = f"{view_phrase(r)}X-ray of the {place}"
        if flag(r["fractured"]):
            n = int(float(r["fracture_count"] or 1)) or 1
            report = f"{what}. Verified label: fracture present ({n} fracture area{'s' if n > 1 else ''} marked by the annotators)."
            if flag(r["hardware"]):
                report += " Metal surgical hardware is also visible."
            label, diagnosis = "abnormal", "fracture"
        elif flag(r["hardware"]):
            continue
        else:
            report, label, diagnosis = f"{what}. Verified label: no fracture.", "normal", ""
        yield Candidate("FracAtlas", part, label, diagnosis, report,
                        "Expert-annotated fracture labels (FracAtlas, Scientific Data 2023)",
                        PurePosixPath(member).name, zip_reader(zf, member))


# ---------------------------------------------------------------- OrthoFrac-XR (fractures, healing)
ORTHO_BONES = {
    "elbow": "elbow_forearm", "ulna": "elbow_forearm", "radius": "elbow_forearm",
    "humerus": "shoulder_upper_arm", "shoulder": "shoulder_upper_arm",
    "femur": "thigh_knee", "knee": "thigh_knee",
    "tibia": "lower_leg_foot", "fibula": "lower_leg_foot", "calcaneus": "lower_leg_foot", "foot": "lower_leg_foot",
    "tarsal": "lower_leg_foot", "metatarsal": "lower_leg_foot",
    "hip join": "pelvis_hip", "hip joint": "pelvis_hip", "hip": "pelvis_hip",
    "carpal": "wrist_hand", "metacarpal": "wrist_hand", "finger": "wrist_hand",
}


def load_orthofrac(ds: Datasets) -> Iterator[Candidate]:
    zf = ds.zip("OrthoFrac-XR.zip")
    idx = index_zip(zf)
    for r in csv.DictReader(io.StringIO(zf.read("OrthoFrac-XR/Bone_metadata.csv").decode("utf-8-sig"))):
        bone = r["bone_type"].strip().lower()
        part = ORTHO_BONES.get(bone)
        member = idx.get(norm(r["id"]))
        if not part or not member:
            continue
        kind = r["fracture_type"].strip().lower()
        note = r["primary_observation"].strip()
        side = r["left_right"].strip().lower()
        where = f"{side + ' ' if side in ('left', 'right') else ''}{'hip joint' if bone.startswith('hip') else bone}"
        if kind == "non_fracture":
            label, diagnosis, desc = "normal", "", "no fracture"
        elif kind == "post_fracture":
            label = "abnormal"
            if "mal" in note.lower():
                diagnosis, desc = "malunion (broken bone healed out of line)", "a fracture that healed out of alignment (malunion)"
            else:
                diagnosis, desc = "healing fracture", "a healing fracture after treatment"
        elif kind in ("distal", "proximal"):
            label, diagnosis = "abnormal", "fracture"
            desc = f"fracture near the {'far' if kind == 'distal' else 'near'} ({kind}) end of the bone"
        else:
            continue
        report = f"X-ray of the {where}. Verified label: {desc}."
        if note and note.lower() != "looks normal":
            report += f" Annotator note: {note}."
        if r.get("age", "").strip():
            report += f" Patient age: {r['age'].strip()}."
        yield Candidate("OrthoFrac-XR", part, label, diagnosis, report,
                        "Expert-annotated fracture labels (OrthoFrac-XR dataset)",
                        PurePosixPath(member).name, zip_reader(zf, member))


# ---------------------------------------------------------------- BTXRD (bone tumors)
BTXRD_LOCATIONS = [
    ("knee-joint", "thigh_knee", "knee"), ("femur", "thigh_knee", "thigh bone (femur)"),
    ("hip-joint", "pelvis_hip", "hip"), ("hip bone", "pelvis_hip", "hip bone"),
    ("ankle-joint", "lower_leg_foot", "ankle"), ("foot", "lower_leg_foot", "foot"),
    ("tibia", "lower_leg_foot", "shin bone (tibia)"), ("fibula", "lower_leg_foot", "fibula"),
    ("shoulder-joint", "shoulder_upper_arm", "shoulder"), ("humerus", "shoulder_upper_arm", "upper arm bone (humerus)"),
    ("elbow-joint", "elbow_forearm", "elbow"), ("ulna", "elbow_forearm", "forearm (ulna)"),
    ("radius", "elbow_forearm", "forearm (radius)"),
    ("wrist-joint", "wrist_hand", "wrist"), ("hand", "wrist_hand", "hand"),
]
BTXRD_TYPES = [  # malignant first, so a malignant flag wins
    ("osteosarcoma", "osteosarcoma (malignant bone tumor)"),
    ("other mt", "other malignant bone tumor"),
    ("giant cell tumor", "giant cell tumor of bone (benign, locally aggressive)"),
    ("osteochondroma", "osteochondroma (benign bone tumor)"),
    ("multiple osteochondromas", "multiple osteochondromas (benign bone tumors)"),
    ("simple bone cyst", "simple bone cyst (benign)"),
    ("osteofibroma", "osteofibroma (benign bone tumor)"),
    ("synovial osteochondroma", "synovial osteochondroma (benign joint condition)"),
    ("other bt", "other benign bone tumor"),
]


def load_btxrd(ds: Datasets) -> Iterator[Candidate]:
    import openpyxl

    zf = ds.zip("BTXRD.zip")
    idx = index_zip(zf)
    wb = openpyxl.load_workbook(io.BytesIO(zf.read("BTXRD/dataset.xlsx")), read_only=True, data_only=True)
    rows = wb.active.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(rows)]
    for values in rows:
        r = dict(zip(header, values))
        if not r.get("image_id"):
            continue
        loc = next(((part, name) for key, part, name in BTXRD_LOCATIONS if flag(r.get(key))), None)
        member = idx.get(norm(PurePosixPath(str(r["image_id"])).stem))
        if not loc or not member:
            continue
        part, place = loc
        what = f"{view_phrase(r)}X-ray of the {place}"
        if flag(r.get("tumor")):
            diagnosis = next((name for key, name in BTXRD_TYPES if flag(r.get(key))), None)
            if not diagnosis:
                diagnosis = "malignant bone tumor" if flag(r.get("malignant")) else "benign bone tumor"
            label, report = "abnormal", f"{what}. Verified label: {diagnosis}."
        else:
            label, diagnosis, report = "normal", "", f"{what}. Verified label: no bone tumor."
        if r.get("age") not in (None, ""):
            report += f" Patient age: {r['age']}."
        yield Candidate("BTXRD", part, label, diagnosis, report,
                        "Expert-annotated bone tumor labels (BTXRD dataset)",
                        PurePosixPath(member).name, zip_reader(zf, member))


# ---------------------------------------------------------------- BDCXR-3257 (children's chest)
def load_bdcxr(ds: Datasets) -> Iterator[Candidate]:
    zf = ds.zip("BDCXR-3257.zip")
    idx = index_zip(zf)
    classes = {
        "normal": ("normal", "", "normal chest, no pneumonia"),
        "pneumonia bacterial": ("abnormal", "pneumonia (bacterial)", "bacterial pneumonia"),
        "pneumonia viral": ("abnormal", "pneumonia (viral)", "viral pneumonia"),
    }
    for r in csv.DictReader(io.StringIO(zf.read("BDCXR-3257/metadata.csv").decode("utf-8-sig"))):
        cls = classes.get(norm(r["Class"]))
        member = idx.get(norm(r["Filename"]))
        if not cls or not member:
            continue
        label, diagnosis, desc = cls
        age = str(r.get("Age", "")).strip()
        try:
            adult = float(age) >= 18
        except ValueError:
            adult = False
        if adult:
            who = "an adult"
        elif age in ("0", "0.0"):
            who = "a baby (under 1 year old)"
        else:
            who = f"a {age}-year-old child" if age else "a child"
        report = f"Chest X-ray of {who}. Verified label: {desc}."
        yield Candidate("BDCXR-3257", "chest_adult" if adult else "chest_child", label, diagnosis, report,
                        "Dataset diagnosis labels (BDCXR-3257, pediatric chest X-rays)",
                        PurePosixPath(member).name, zip_reader(zf, member))


# ---------------------------------------------------------------- Indiana University (adult chest + real reports)
IU_DIAGNOSES = [
    ("pneumothorax (collapsed lung)", [r"pneumothora"]),
    ("pneumonia / consolidation", [r"pneumonia", r"consolidat", r"air ?space (disease|opacit)", r"infiltrat"]),
    ("pleural effusion", [r"effusion"]),
    ("pulmonary edema / congestion", [r"edema", r"vascular congestion", r"venous congestion", r"heart failure"]),
    ("cardiomegaly (enlarged heart)", [r"cardiomegaly", r"heart (size )?is enlarged", r"enlarged (heart|cardiac)"]),
    ("mass, nodule or cyst", [r"nodul", r"\bmass\b"]),
    ("atelectasis (partly collapsed lung)", [r"atelecta"]),
    ("emphysema / hyperinflation (COPD)", [r"emphysema", r"hyperinflat", r"hyperexpan", r"copd", r"chronic obstructive"]),
    ("fracture", [r"fractur"]),
    ("calcified granuloma (old, healed infection)", [r"granulom"]),
    ("scarring / chronic lung changes", [r"scar", r"fibros", r"interstitial"]),
    ("degenerative spine changes", [r"degenerative", r"spondylosis", r"osteophyt"]),
    ("hiatal hernia", [r"hiatal hernia"]),
]
IU_NORMAL = [r"no acute", r"normal chest", r"no active", r"negative chest", r"lungs are clear", r"\bnormal\b",
             r"unremarkable", r"no (radiographic )?evidence"]


def load_iu_chest(ds: Datasets) -> Iterator[Candidate]:
    import pyarrow.parquet as pq

    seen_studies: set[str] = set()
    for shard in ("IU_chest_test.parquet", "IU_chest_train0.parquet", "IU_chest_train1.parquet"):
        yield from _iu_shard(pq.read_table(ds.path(shard), columns=["image", "report"]), seen_studies)


def _iu_shard(table, seen_studies: set[str]) -> Iterator[Candidate]:
    for image, report in zip(table.column("image").to_pylist(), table.column("report").to_pylist()):
        if not image or not image.get("bytes") or not report:
            continue
        path = image.get("path") or "iu.png"
        study = re.match(r"(CXR\d+)", PurePosixPath(path).name)
        if study:
            if study.group(1) in seen_studies:
                continue  # one image per study (the report covers both views)
            seen_studies.add(study.group(1))
        text = clean(report, 800)
        impression = text.split("IMPRESSION:", 1)[1] if "IMPRESSION:" in text else text
        diagnosis = first_match(positive_text(impression), IU_DIAGNOSES)
        if diagnosis:
            label = "abnormal"
        elif any(re.search(p, impression.lower()) for p in IU_NORMAL):
            label, diagnosis = "normal", ""
        else:
            continue
        data = image["bytes"]
        yield Candidate("Indiana University Chest X-rays", "chest_adult", label, diagnosis,
                        f"Radiologist report: {text}",
                        "Radiologist's report (Indiana University chest X-ray collection)",
                        PurePosixPath(path).name, lambda data=data: data)


# ---------------------------------------------------------------- ROCOv2 (published case reports; many body parts)
ROCO_PLAIN_XRAY = "C1306645"
ROCO_MODALITIES = {"C1306645": "xray", "C0040405": "ct", "C0024485": "mri"}
CT_MRI_DIAGNOSES = [  # checked before the general list for CT and MRI captions
    ("stroke (blocked blood flow in the brain)", [r"\bstroke\b", r"infarct", r"ischemi"]),
    ("bleeding in the brain (hemorrhage)", [r"h(a)?emorrhag", r"hematoma", r"\bbleed"]),
    ("brain tumor", [r"glioma", r"glioblastoma", r"meningioma", r"brain tumou?r", r"intracranial (mass|tumou?r|lesion)"]),
    ("aneurysm", [r"aneurysm"]),
    ("blood clot in the lung (pulmonary embolism)", [r"pulmonary embol", r"\bembol"]),
    ("herniated (slipped) disc", [r"herniat", r"disc (protrusion|extrusion|bulge)", r"disk (protrusion|extrusion|bulge)"]),
    ("spinal stenosis (narrowed spinal canal)", [r"stenosis"]),
    ("torn ligament or meniscus", [r"\btear\b", r"\btorn\b", r"rupture", r"meniscal", r"meniscus", r"\bacl\b", r"ligament injur"]),
    ("appendicitis", [r"appendicitis"]),
    ("abscess (pocket of infection)", [r"abscess"]),
    ("hydrocephalus (fluid build-up in the brain)", [r"hydrocephal"]),
    ("multiple sclerosis", [r"multiple sclerosis", r"demyelinat"]),
    ("cancer or metastasis", [r"metasta", r"carcinoma", r"\bcancer", r"malignan", r"lymphoma", r"sarcoma"]),
]
ROCO_REGIONS = {"C0006141", "C0037303", "C0037949", "C0000726", "C0030797", "C1140618", "C0023216", "C0817096"}
GENERAL_DIAGNOSES = [
    ("fracture", [r"fractur"]),
    ("dislocation", [r"dislocat", r"subluxat"]),
    ("bone tumor or bone lesion", [r"osteosarcoma", r"sarcoma", r"chondroma", r"giant cell tumou?r", r"bone cyst",
                                   r"lytic", r"sclerotic lesion", r"bone tumou?r", r"osteoma\b"]),
    ("metastasis (cancer spread)", [r"metasta"]),
    ("pneumothorax (collapsed lung)", [r"pneumothora"]),
    ("pneumonia / consolidation", [r"pneumonia", r"consolidat", r"infiltrat"]),
    ("pleural effusion", [r"effusion"]),
    ("cardiomegaly (enlarged heart)", [r"cardiomegaly", r"enlarged (heart|cardiac)"]),
    ("free air in the abdomen (pneumoperitoneum)", [r"pneumoperitoneum", r"free (intraperitoneal )?air", r"under the (right )?(hemi)?diaphragm"]),
    ("bowel obstruction", [r"(bowel|intestinal|colonic) obstruct", r"air[- ]fluid level", r"dilated (small )?(bowel|intestin|loops)",
                           r"\bileus\b", r"volvulus"]),
    ("stone (kidney, bladder or gallbladder)", [r"calcul", r"\bstones?\b", r"nephrolith", r"urolith", r"cholelith"]),
    ("foreign body", [r"foreign bod", r"ingest", r"swallow", r"\bcoins?\b"]),
    ("bone infection (osteomyelitis)", [r"osteomyelitis"]),
    ("arthritis / joint wear", [r"arthritis", r"arthrosis", r"joint space narrowing", r"osteophyt"]),
    ("scoliosis (curved spine)", [r"scoliosis"]),
    ("slipped vertebra (spondylolisthesis)", [r"spondylolisthesis"]),
    ("tooth decay or tooth infection", [r"caries", r"periapical", r"radiolucen.{0,30}(root|apex|tooth|teeth)"]),
    ("impacted tooth", [r"impacted"]),
    ("sinus disease (sinusitis)", [r"sinusitis", r"opacifi.{0,40}sinus"]),
    ("mass, nodule or cyst", [r"nodul", r"\bmass\b", r"tumou?r", r"cyst", r"\blesion"]),
    ("surgical hardware or implant", [r"prosthe", r"arthroplast", r"\bplates?\b", r"\bscrews?\b", r"\bnail", r"implant",
                                      r"fixat", r"k-?wires?", r"\bstent", r"pacemaker"]),
]


def roco_body_part(regions: set[str], caption: str) -> str | None:
    t = caption.lower()

    def has(*patterns: str) -> bool:
        return any(re.search(p, t) for p in patterns)

    if "C0006141" in regions:
        return "breast"
    if "C0037303" in regions:
        dental = has(r"\bteeth\b", r"\btooth\b", r"dental", r"mandib", r"maxill", r"panoram", r"orthopantomo",
                     r"\bopg\b", r"molar", r"incisor", r"\bcanine\b", r"periapical", r"\bjaws?\b", r"root canal")
        return "teeth_jaw" if dental else "skull_face_sinuses"
    if "C0037949" in regions:
        neck = has(r"cervical", r"\bneck\b", r"\bc[1-7]\b", r"odontoid", r"\bdens\b", r"atlanto")
        return "neck_cervical_spine" if neck else "back_spine"
    if "C0000726" in regions:
        return "abdomen_kidneys"
    if "C0030797" in regions:
        return "pelvis_hip"
    if "C1140618" in regions:
        if has(r"wrist", r"\bhands?\b", r"finger", r"carp", r"phalan", r"thumb", r"scaphoid", r"distal radius"):
            return "wrist_hand"
        if has(r"elbow", r"forearm", r"radius", r"radial", r"ulna", r"olecran", r"supracondylar"):
            return "elbow_forearm"
        if has(r"shoulder", r"clavic", r"humer", r"scapul", r"acromi", r"glenoid"):
            return "shoulder_upper_arm"
        return None
    if "C0023216" in regions:
        if has(r"\bhips?\b", r"acetab", r"femoral neck", r"trochanter"):
            return "pelvis_hip"
        if has(r"knee", r"patell", r"tibial plateau", r"femur", r"femoral", r"thigh"):
            return "thigh_knee"
        if has(r"ankle", r"\bfoot\b", r"\bfeet\b", r"\btoes?\b", r"calcane", r"talus", r"metatars", r"tarsal",
               r"\bheel\b", r"malleol", r"tibia", r"fibula", r"\blegs?\b"):
            return "lower_leg_foot"
        return None
    if "C0817096" in regions:
        age = re.search(r"(\d+)[- ](?:year|yr)s?[- ]old", t)
        child = has(r"infant", r"neonat", r"newborn", r"\bchild", r"\bboy\b", r"\bgirl\b", r"toddler", r"month[- ]old",
                    r"week[- ]old", r"day[- ]old", r"p(a)?ediatric") or bool(age and int(age.group(1)) < 18)
        return "chest_child" if child else "chest_adult"
    return None


CHEST = {"chest_adult", "chest_child"}
LIMBS = {"shoulder_upper_arm", "elbow_forearm", "wrist_hand", "pelvis_hip", "thigh_knee", "lower_leg_foot"}
ONLY_IN = {  # conditions that only make sense for some body parts
    "pneumothorax (collapsed lung)": CHEST,
    "pneumonia / consolidation": CHEST,
    "pleural effusion": CHEST,
    "cardiomegaly (enlarged heart)": CHEST,
    "stone (kidney, bladder or gallbladder)": {"abdomen_kidneys", "pelvis_hip", "back_spine"},
    "bowel obstruction": {"abdomen_kidneys", "pelvis_hip"} | CHEST,
    "free air in the abdomen (pneumoperitoneum)": {"abdomen_kidneys"} | CHEST,
    "tooth decay or tooth infection": {"teeth_jaw", "skull_face_sinuses"},
    "impacted tooth": {"teeth_jaw", "skull_face_sinuses"},
    "sinus disease (sinusitis)": {"teeth_jaw", "skull_face_sinuses"},
    # CT / MRI captions
    "stroke (blocked blood flow in the brain)": {"skull_face_sinuses"},
    "bleeding in the brain (hemorrhage)": {"skull_face_sinuses"},
    "brain tumor": {"skull_face_sinuses"},
    "hydrocephalus (fluid build-up in the brain)": {"skull_face_sinuses"},
    "multiple sclerosis": {"skull_face_sinuses", "neck_cervical_spine", "back_spine"},
    "aneurysm": {"skull_face_sinuses", "abdomen_kidneys"} | CHEST,
    "blood clot in the lung (pulmonary embolism)": CHEST,
    "herniated (slipped) disc": {"back_spine", "neck_cervical_spine"},
    "spinal stenosis (narrowed spinal canal)": {"back_spine", "neck_cervical_spine"},
    "torn ligament or meniscus": LIMBS,
    "appendicitis": {"abdomen_kidneys", "pelvis_hip"},
}


def roco_diagnosis(part: str, caption: str) -> tuple[str, str] | None:
    diagnosis = first_match(positive_text(caption), GENERAL_DIAGNOSES)
    if diagnosis == "pleural effusion" and part in LIMBS:
        diagnosis = "joint effusion (fluid in the joint)"
    elif diagnosis and part not in ONLY_IN.get(diagnosis, {part}):
        return None  # e.g. an incidental lung finding on a shoulder X-ray
    if diagnosis:
        return "abnormal", diagnosis
    if re.search(r"\bnormal\b|unremarkable|no abnormalit", caption.lower()):
        return "normal", ""
    return None


# CT and MRI captions carry no anatomy concepts in ROCOv2, so the body part is read from the caption with
# radiology words (the patient-chat matcher is too loose here: "head of the pancreas", uterine "cervical cancer").
CT_MRI_REGIONS: list[tuple[str, list[str]]] = [  # order = tie-break priority (more specific first)
    ("breast", [r"\bbreasts?\b", r"mammar", r"mammo"]),
    ("teeth_jaw", [r"\bteeth\b", r"\btooth\b", r"dental", r"odontogen", r"mandib", r"\bjaws?\b", r"periapical",
                   r"temporomandibular", r"\btmj\b", r"\bmolars?\b"]),
    ("wrist_hand", [r"\bwrists?\b", r"\bhands?\b", r"\bfingers?\b", r"carpal", r"scaphoid", r"metacarp", r"phalan", r"\bthumb"]),
    ("elbow_forearm", [r"\belbows?\b", r"forearm", r"olecran", r"\bulna", r"radial head", r"\bradius\b"]),
    ("shoulder_upper_arm", [r"shoulder", r"rotator cuff", r"glenoid", r"glenohumeral", r"humer(us|al)", r"clavic",
                            r"scapul", r"acromi", r"supraspinatus", r"\baxilla"]),
    ("lower_leg_foot", [r"\bankles?\b", r"\bfoot\b", r"\bfeet\b", r"calcane", r"\btalus\b", r"\btalar\b", r"achilles",
                        r"\btibia(?!l plateau)", r"fibula", r"metatars", r"\btoes?\b", r"\bcalf\b", r"\bheel\b", r"plantar", r"peroneal"]),
    ("thigh_knee", [r"\bknees?\b", r"menisc", r"patell", r"cruciate", r"\bacl\b", r"\bfemur\b", r"femoral (shaft|diaphysis|condyle)",
                    r"\bthighs?\b", r"quadriceps", r"hamstring", r"popliteal", r"tibial plateau"]),
    ("neck_cervical_spine", [r"\bneck\b(?! of)", r"cervical (spine|vertebra|cord|spinal|disc|disk|myelopathy|lymph|region)",
                             r"\bc[1-7](-c?[1-7])?\b", r"thyroid", r"laryn", r"pharyn", r"parotid", r"submandibular", r"salivary",
                             r"carotid"]),
    ("back_spine", [r"lumbar", r"thoracic (spine|vertebra|cord)", r"\bspinal (cord|canal|stenosis)", r"\bspine\b", r"vertebra",
                    r"\bsacr(um|al)\b", r"intervertebral", r"\bdis[ck] (herniation|protrusion|extrusion|bulg)", r"\bl[1-5](-s?[1-5])?\b",
                    r"paraspinal"]),
    ("pelvis_hip", [r"pelvi(s|c)", r"\bhips?\b", r"acetabul", r"prostat", r"uter(us|ine)", r"ovar(y|ian)", r"\bcervix",
                    r"cervical (cancer|carcinoma|polyp|stump)", r"\brect(um|al)\b", r"sacroiliac", r"adnex", r"endometri",
                    r"vagin", r"testi(s|cular)", r"scrot", r"femoral (head|neck)", r"proximal femur", r"groin", r"inguinal"]),
    ("skull_face_sinuses", [r"\bbrain", r"cerebr", r"cerebell", r"intracranial", r"\bcranial\b", r"cranium", r"\bskull\b",
                            r"\bhead (ct|mri|mr|computed|scan|magnetic)", r"\b(ct|mri|mr) (scan )?(of the )?head\b",
                            r"paranasal", r"(maxillary|frontal|sphenoid|ethmoid) sinus", r"\borbit(al|s)?\b", r"pituitar", r"\bsella",
                            r"(lateral|third|fourth) ventricle", r"intraventricular", r"subdural", r"epidural hematoma", r"subarachnoid",
                            r"(frontal|temporal|parietal|occipital) lobe", r"basal ganglia", r"thalam", r"\bpons\b", r"brain ?stem",
                            r"midbrain", r"gliom", r"glioblastom", r"meningiom", r"astrocytom", r"medulloblastom", r"\bfacial\b",
                            r"zygom", r"nasal cavity", r"mastoid", r"cochlea", r"temporal bone"]),
    ("abdomen_kidneys", [r"abdom", r"\bliver\b", r"hepat", r"pancrea", r"\bkidneys?\b", r"\brenal\b", r"nephr", r"spleen", r"splenic",
                         r"\bbowel", r"intestin", r"jejun", r"\bileum\b", r"\bileal\b", r"duoden", r"\bcolon", r"colonic", r"c(a)?ecum",
                         r"appendi", r"gallbladder", r"biliary", r"bile duct", r"cholang", r"choledoch", r"stomach", r"gastric",
                         r"adrenal", r"peritone", r"mesenter", r"retroperiton", r"\bureter", r"\bbladder\b", r"\bkub\b"]),
    ("chest_adult", [r"\bchest\b", r"\blungs?\b", r"pulmonar", r"\bthora(x|cic)\b(?! (spine|vertebra|cord))", r"mediastin", r"pleura",
                     r"bronch", r"\bheart\b", r"cardiac", r"myocard", r"pericard", r"coronary", r"(left|right) ventric", r"\batri(um|al)\b",
                     r"aortic (valve|root|arch)", r"esophag", r"oesophag", r"thym"]),
]
CHILD_WORDS = re.compile(r"\b(infant|newborn|neonat\w*|toddler|child|children|pediatric|paediatric|boy|girl)\b")


def roco_ct_mri_body_part(caption: str) -> str | None:
    t = caption.lower()
    best, best_score = None, 0
    for part, patterns in CT_MRI_REGIONS:
        score = sum(1 for p in patterns if re.search(p, t))
        if score > best_score:
            best, best_score = part, score
    if best == "chest_adult" and CHILD_WORDS.search(t):
        return "chest_child"
    return best


def roco_ct_mri_diagnosis(part: str, caption: str) -> tuple[str, str] | None:
    text = positive_text(caption)
    diagnosis = first_match(text, CT_MRI_DIAGNOSES) or first_match(text, GENERAL_DIAGNOSES)
    if diagnosis and part not in ONLY_IN.get(diagnosis, {part}):
        return None
    if diagnosis:
        return "abnormal", diagnosis
    if re.search(r"\bnormal\b|unremarkable|no abnormalit", caption.lower()):
        return "normal", ""
    return None


def load_rocov2(ds: Datasets, splits: tuple[str, ...] = ("valid", "test", "train")) -> Iterator[Candidate]:
    """ROCOv2 case-report images: plain X-rays, plus CT and MRI (the only MRI/CT cases we have)."""
    for split in splits:
        try:
            captions = {r["ID"]: r["Caption"] for r in csv.DictReader(io.StringIO(ds.text(f"ROCOv2_{split}_captions.csv")))}
        except Exception as exc:  # e.g. the big train split isn't downloaded yet
            print(f"  ROCOv2 {split}: skipped ({exc})", flush=True)
            continue
        wanted: dict[str, tuple[str, str, str]] = {}
        for r in csv.DictReader(io.StringIO(ds.text(f"ROCOv2_{split}_concepts_manual.csv"))):
            cuis = [c.strip() for c in r["CUIs"].split(";") if c.strip()]
            modality = ROCO_MODALITIES.get(cuis[0]) if cuis else None
            if not modality or r["ID"] not in captions:
                continue
            caption = clean(captions[r["ID"]], 500)
            if modality == "xray":
                part = roco_body_part(set(cuis) & ROCO_REGIONS, caption)
            else:  # ROCOv2 attaches anatomy concepts to X-rays only: read the body part from the caption
                part = roco_ct_mri_body_part(caption)
            if part and part != "whole_body":
                wanted[r["ID"]] = (part, caption, modality)
        try:
            zf = ds.zip(f"ROCOv2_{split}_images.zip")
        except Exception as exc:
            print(f"  ROCOv2 {split} images: skipped ({exc})", flush=True)
            continue
        idx = index_zip(zf)
        for image_id, (part, caption, modality) in wanted.items():
            member = idx.get(norm(image_id))
            if not member:
                continue
            found = roco_diagnosis(part, caption) if modality == "xray" else roco_ct_mri_diagnosis(part, caption)
            if not found:
                continue
            label, diagnosis = found
            yield Candidate("ROCOv2", part, label, diagnosis, f"Published case-report caption: {caption}",
                            "Caption written by the authors of a peer-reviewed case report (ROCOv2 / PubMed Central)",
                            PurePosixPath(member).name, zip_reader(zf, member), modality)


# ---------------------------------------------------------------- Mammo-MX (mammograms, BI-RADS)
DENSITY = {"D1": "almost entirely fatty", "D2": "scattered dense areas", "D3": "dense in many areas",
           "D4": "extremely dense"}
BIRADS = {
    "B6": ("breast cancer (biopsy-proven)", "BI-RADS 6, cancer already confirmed by biopsy"),
    "B5": ("highly suspicious finding (likely cancer)", "BI-RADS 5, highly suggestive of cancer; biopsy needed"),
    "B4": ("suspicious finding (biopsy recommended)", "BI-RADS 4, suspicious; biopsy recommended"),
    "B3": ("probably benign finding (short follow-up)", "BI-RADS 3, probably benign; repeat imaging in 6 months"),
    "B1": ("", "BI-RADS 1, negative (normal mammogram)"),
    "B0": ("unclear mammogram that needs more imaging", "BI-RADS 0, incomplete: more imaging needed"),
}


def load_mammo_mx(ds: Datasets) -> Iterator[Candidate]:
    meta = {norm(r["ImageID"]): r for r in csv.DictReader(io.StringIO(ds.text("MammoMX_Metadata.csv")))}
    for code in ("B6", "B5", "B4", "B3", "B1", "B0"):
        name = f"MammoMX_{code}.zip"
        if not (ds.folder / name).exists() and not ds.remote and code not in ("B6", "B0"):
            continue  # the big sets are optional: use them when they have been downloaded
        try:
            zf = ds.zip(name)
        except Exception as exc:
            print(f"  Mammo-MX {code}: skipped ({exc})", flush=True)
            continue
        for key, member in index_zip(zf).items():
            r = meta.get(key, {})
            diagnosis, meaning = BIRADS[code]
            side = {"L": "left", "R": "right"}.get(r.get("Laterality", ""), "")
            view = {"MLO": "Side-angle (MLO)", "CC": "Top-down (CC)"}.get(r.get("View", ""), "")
            report = f"{view + ' ' if view else ''}mammogram of the {side + ' ' if side else ''}breast. " \
                     f"Radiologist assessment: {meaning}."
            if r.get("Breast Density") in DENSITY:
                report += f" Breast tissue: {DENSITY[r['Breast Density']]}."
            if r.get("Patient Age"):
                report += f" Patient age: {r['Patient Age']}."
            report = report.strip()
            yield Candidate("Mammo-MX", "breast", "normal" if code == "B1" else "abnormal", diagnosis,
                            report[0].upper() + report[1:],
                            "Radiologist BI-RADS assessment (Mammo-MX dataset)",
                            PurePosixPath(member).name + ".dcm", zip_reader(zf, member))



# ================================================================ expansion sources (Nov 2026)
# Added for the "8 GB more per body part" build. Every URL below was checked with an HTTP range
# request on 2026-11-02 (sizes in the comments are the servers' byte counts, rounded).
FILES.update({
    # NIH ChestX-ray14, 112,120 frontal chest X-rays, 45 GB in 12 tarballs (Box serves GET only).
    "NIH_Data_Entry_2017_v2020.csv": "https://huggingface.co/datasets/alkzar90/NIH-Chest-X-ray-dataset/resolve/main/data/Data_Entry_2017_v2020.csv",
    "NIH_images_001.tar.gz": "https://nihcc.box.com/shared/static/vfk49d74nhbxq3nqjg0900w5nvkorp5c.gz",  # 2.0 GB
    "NIH_images_002.tar.gz": "https://nihcc.box.com/shared/static/i28rlmbvmfjbl8p2n3ril0pptcmcu9d1.gz",  # 4.0 GB
    "NIH_images_003.tar.gz": "https://nihcc.box.com/shared/static/f1t00wrtdk94satdfb9olcolqx20z2jp.gz",  # 3.9 GB
    "NIH_images_004.tar.gz": "https://nihcc.box.com/shared/static/0aowwzs5lhjrceb3qp67ahp0rd1l1etg.gz",  # 3.8 GB
    "NIH_images_005.tar.gz": "https://nihcc.box.com/shared/static/v5e3goj22zr6h8tzualxfsqlqaygfbsn.gz",
    "NIH_images_006.tar.gz": "https://nihcc.box.com/shared/static/asi7ikud9jwnkrnkj99jnpfkjdes7l6l.gz",
    "NIH_images_007.tar.gz": "https://nihcc.box.com/shared/static/jn1b4mw4n6lnh74ovmcjb8y48h8xj07n.gz",
    "NIH_images_008.tar.gz": "https://nihcc.box.com/shared/static/tvpxmn7qyrgl0w8wfh9kqfjskv6nmm1j.gz",
    "NIH_images_009.tar.gz": "https://nihcc.box.com/shared/static/upyy3ml7qdumlgk2rfcvlb9k6gvqq2pj.gz",
    "NIH_images_010.tar.gz": "https://nihcc.box.com/shared/static/l6nilvfa9cg3s28tqv1qc1olm3gnz54p.gz",
    "NIH_images_011.tar.gz": "https://nihcc.box.com/shared/static/hhq8fkdgvcari67vfhs7ppg2w6ni4jze.gz",
    "NIH_images_012.tar.gz": "https://nihcc.box.com/shared/static/ioqwiy20ihqwyr8pf4c24eazhh281pbu.gz",
    # Kermany pediatric chest X-rays (inside the 8.4 GB OCT + CXR archive), Mendeley rscbjbr9sj v3.
    "ZhangLabData.zip": "https://data.mendeley.com/public-files/datasets/rscbjbr9sj/files/810b2ce2-11c3-4424-996e-3bef36600907/file_downloaded",
    "NG_Pneum_Dataset.zip": "https://zenodo.org/api/records/14185822/files/NG%20Pneum%20Dataset.zip/content",  # 0.19 GB
    # GRAZPEDWRI-DX pediatric wrist trauma, 20,327 X-rays in 4 zips of ~4 GB (figshare 14825193).
    "GRAZPEDWRI_dataset.csv": "https://ndownloader.figshare.com/files/35026432",
    "GRAZPEDWRI_images_part1.zip": "https://ndownloader.figshare.com/files/34268828",
    "GRAZPEDWRI_images_part2.zip": "https://ndownloader.figshare.com/files/34268849",
    "GRAZPEDWRI_images_part3.zip": "https://ndownloader.figshare.com/files/34268864",
    "GRAZPEDWRI_images_part4.zip": "https://ndownloader.figshare.com/files/34268891",
    # DENTEX panoramic dental X-rays with dentist diagnoses (Zenodo 7812323), 10.9 GB.
    "DENTEX_training_data.zip": "https://zenodo.org/api/records/7812323/files/training_data.zip/content",
    "DentIRO.zip": "https://ndownloader.figshare.com/files/64174315",  # 0.7 GB, single-tooth X-rays
    # KUB-StoneX abdominal X-rays with stone annotations (figshare 32049369), 20.8 GB.
    "KUB-StoneX.zip": "https://ndownloader.figshare.com/files/64127092",
    # Knee osteoarthritis grading (Mendeley 56rmx5bjcr, OAI-derived), 7.2 GB.
    "KneeXrayData.zip": "https://data.mendeley.com/public-files/datasets/56rmx5bjcr/files/205a9a5d-5de5-4f06-8f63-e430ed23ca44/file_downloaded",
    # MURA v1.1 (Stanford): 40,561 upper-limb X-rays labeled normal/abnormal by radiologists, 3.4 GB.
    "MURA-v1.1.zip": "https://cs.stanford.edu/group/mlgroup/MURA-v1.1.zip",
    "PediaSHF-DX.zip": "https://ndownloader.figshare.com/files/55979294",  # 0.9 GB pediatric elbow
    "UCI_shoulder_implants.zip": "https://archive.ics.uci.edu/static/public/517/shoulder+implant+x+ray+manufacturer+classification.zip",
    # Brain tumor MRI (figshare 1512427), 3,064 T1 slices with pathology labels, 0.9 GB.
    "BrainTumor_1.zip": "https://ndownloader.figshare.com/files/3381290",
    "BrainTumor_2.zip": "https://ndownloader.figshare.com/files/3381296",
    "BrainTumor_3.zip": "https://ndownloader.figshare.com/files/3381293",
    "BrainTumor_4.zip": "https://ndownloader.figshare.com/files/3381302",
    # Cervical Spine X-ray Atlas (CSXA v3), 4,963 lateral neck X-rays, 1.4 GB + labels.
    "CSXA_PNG.zip": "https://huggingface.co/datasets/MartialTerran/Cervical_Spine_X-ray_Atlas_CSXA_V3.0/resolve/main/datasets-PNG.zip",
    "CSXA_labels.xlsx": "https://huggingface.co/datasets/MartialTerran/Cervical_Spine_X-ray_Atlas_CSXA_V3.0/resolve/main/datasets.xlsx",
    # SPIDER lumbar spine MRI with radiologist gradings (Zenodo 10159290), 3.7 GB.
    "SPIDER_images.zip": "https://zenodo.org/api/records/10159290/files/images.zip/content",
    "SPIDER_gradings.csv": "https://zenodo.org/api/records/10159290/files/radiological_gradings.csv/content",
    "SPIDER_overview.csv": "https://zenodo.org/api/records/10159290/files/overview.csv/content",
})

SOURCES_INFO.update({
    "nih_chest": ("NIH ChestX-ray14", "NIH Clinical Center open data (free to use, cite)", "https://nihcc.app.box.com/v/ChestXray-NIHCC"),
    "kermany": ("Kermany pediatric chest X-rays", "CC BY 4.0", "https://doi.org/10.17632/rscbjbr9sj.3"),
    "ng_pneumonia": ("Under-5 Nigerian pneumonia CXR", "CC BY 4.0", "https://doi.org/10.5281/zenodo.14185822"),
    "grazpedwri": ("GRAZPEDWRI-DX", "CC BY 4.0", "https://doi.org/10.6084/m9.figshare.14825193"),
    "dentex": ("DENTEX 2023", "CC BY 4.0", "https://doi.org/10.5281/zenodo.7812323"),
    "dentiro": ("DentIRO", "CC BY 4.0", "https://doi.org/10.6084/m9.figshare.32086377"),
    "kub_stonex": ("KUB-StoneX", "CC BY 4.0", "https://doi.org/10.6084/m9.figshare.32049369"),
    "knee_oa": ("Knee Osteoarthritis Severity Grading (OAI)", "CC BY 4.0", "https://doi.org/10.17632/56rmx5bjcr.1"),
    "mura": ("MURA v1.1 (Stanford)", "Stanford research-use agreement (non-commercial, not redistributed)", "https://stanfordmlgroup.github.io/competitions/mura/"),
    "pediashf": ("PediaSHF-DX", "CC BY 4.0", "https://doi.org/10.6084/m9.figshare.29322869"),
    "uci_shoulder": ("UCI shoulder implant X-rays", "CC BY 4.0", "https://archive.ics.uci.edu/dataset/517"),
    "brain_tumor": ("Brain tumor MRI (Cheng et al.)", "CC BY 4.0", "https://doi.org/10.6084/m9.figshare.1512427"),
    "csxa": ("Cervical Spine X-ray Atlas (CSXA)", "CC BY 4.0", "https://huggingface.co/datasets/MartialTerran/Cervical_Spine_X-ray_Atlas_CSXA_V3.0"),
    "spider": ("SPIDER lumbar spine MRI", "CC BY 4.0", "https://doi.org/10.5281/zenodo.10159290"),
})
CAP_FACTOR.update({"mura": 1.5, "nih_chest": 1.0, "grazpedwri": 1.5, "kub_stonex": 1.5, "knee_oa": 1.0})


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def have_file(ds: Datasets, name: str) -> bool:
    """True when a source file is on disk (or we are allowed to fetch it). Big optional files are
    only used when they have already been downloaded, so a build never stalls on a 20 GB download."""
    return (ds.folder / name).exists() or ds.remote


def unique_name(tag: str, p: PurePosixPath, depth: int = 2) -> str:
    """Case file names must be unique across every source (the library dedupes by file stem):
    prefix the image name with a source tag and its last folders, e.g. mura-patient00011-study1-positive-image1.png"""
    folders = [norm(x).replace(" ", "-") for x in p.parts[-depth - 1:-1]]
    return "-".join([tag, *folders, p.name])


def folder_cases(ds: Datasets, zip_name: str, source: str, rules: list[tuple[str, str, str, str, str]],
                 verified_by: str, modality: str = "xray", report_prefix: str = "", tag: str = "",
                 suffixes: tuple[str, ...] = (".png", ".jpg", ".jpeg"), depth: int = 2) -> Iterator[Candidate]:
    """Generic loader for datasets whose label is the folder name.

    rules: (path regex, body_part, label, diagnosis, report sentence); the first matching rule wins.
    """
    try:
        zf = ds.zip(zip_name)
    except Exception as exc:
        print(f"  {source}: skipped ({exc})", flush=True)
        return
    tag = tag or re.sub(r"[^a-z0-9]+", "", source.lower())[:8]
    compiled = [(re.compile(pat, re.I), part, label, diagnosis, text) for pat, part, label, diagnosis, text in rules]
    for info in zf.infolist():
        if info.is_dir() or info.file_size < 2_000:
            continue
        p = PurePosixPath(info.filename)
        if p.suffix.lower() not in suffixes or p.name.startswith(".") or "__MACOSX" in info.filename:
            continue
        for rx, part, label, diagnosis, text in compiled:
            if rx.search(info.filename):
                yield Candidate(source, part, label, diagnosis, (report_prefix + " " + text).strip(), verified_by,
                                unique_name(tag, p, depth), zip_reader(zf, info.filename), modality)
                break


# ---------------------------------------------------------------- NIH ChestX-ray14 (tar.gz streams)
NIH_FINDINGS = {
    "Atelectasis": "collapsed lung tissue (atelectasis)",
    "Cardiomegaly": "cardiomegaly (enlarged heart)",
    "Effusion": "pleural effusion",
    "Infiltration": "lung infiltrate (possible infection or fluid)",
    "Mass": "lung mass",
    "Nodule": "lung nodule",
    "Pneumonia": "pneumonia / consolidation",
    "Pneumothorax": "pneumothorax (collapsed lung)",
    "Consolidation": "pneumonia / consolidation",
    "Edema": "fluid in the lungs (pulmonary edema)",
    "Emphysema": "emphysema (damaged air sacs, COPD)",
    "Fibrosis": "lung scarring (fibrosis)",
    "Pleural_Thickening": "thickened lung lining (pleural thickening)",
    "Hernia": "hiatal hernia (stomach pushing up into the chest)",
}
NIH_PER_LABEL_PER_TAR = _env_int("NIH_PER_LABEL", 120)  # images extracted per finding per tarball


def nih_tarballs(ds: Datasets) -> list[str]:
    names = [f"NIH_images_{i:03d}.tar.gz" for i in range(1, 13)]
    picked = [n for n in names if (ds.folder / n).exists()]
    if not picked and ds.remote:
        picked = names[1:3]  # a sensible default when nothing is downloaded yet: ~8 GB
    return picked


def load_nih_chest(ds: Datasets) -> Iterator[Candidate]:
    """Streams each downloaded NIH tarball once, keeps a capped sample per finding in a cache folder."""
    import tarfile

    rows = {r["Image Index"]: r for r in csv.DictReader(io.StringIO(ds.text("NIH_Data_Entry_2017_v2020.csv")))}
    cache = ds.folder / "nih_extract"
    cache.mkdir(parents=True, exist_ok=True)
    for tar_name in nih_tarballs(ds):
        marker = cache / (tar_name + ".done")
        if marker.exists():
            kept = marker.read_text().split()
        else:
            quota: Counter = Counter()
            kept = []
            print(f"  streaming {tar_name} ...", flush=True)
            with tarfile.open(ds.path(tar_name), "r|gz") as tf:
                for m in tf:
                    if not m.isfile():
                        continue
                    fname = PurePosixPath(m.name).name
                    r = rows.get(fname)
                    if not r:
                        continue
                    primary = r["Finding Labels"].split("|")[0]
                    if quota[primary] >= NIH_PER_LABEL_PER_TAR:
                        continue
                    f = tf.extractfile(m)
                    if not f:
                        continue
                    (cache / fname).write_bytes(f.read())
                    quota[primary] += 1
                    kept.append(fname)
            marker.write_text("\n".join(kept))
            print(f"  {tar_name}: kept {len(kept)} images", flush=True)
        for fname in kept:
            r = rows.get(fname)
            path = cache / fname
            if not r or not path.exists():
                continue
            labels = [x for x in r["Finding Labels"].split("|") if x]
            try:
                age = int(float(r["Patient Age"]))
            except ValueError:
                age = 0
            part = "chest_child" if 0 < age < 16 else "chest_adult"
            view = {"PA": "front view, taken from behind (PA)", "AP": "front view, taken from the front (AP)"}.get(r["View Position"], "front view")
            sex = {"M": "male", "F": "female"}.get(r["Patient Gender"], "")
            who = ", ".join(x for x in (sex, f"age {age}" if age else "") if x)
            if labels == ["No Finding"]:
                label, diagnosis, finding = "normal", "", "no abnormal finding (report labeled 'No Finding')"
            else:
                names = [NIH_FINDINGS.get(x, x.replace("_", " ").lower()) for x in labels if x != "No Finding"]
                label, diagnosis, finding = "abnormal", names[0], ", ".join(dict.fromkeys(names))
            report = f"Chest X-ray, {view}{', ' + who if who else ''}. Findings labeled in the radiology report: {finding}."
            yield Candidate("NIH ChestX-ray14", part, label, diagnosis, report,
                            "Labels mined from the hospital radiology report by NIH's text-mining (ChestX-ray14, about 90% label accuracy)",
                            fname, (lambda pth=path: pth.read_bytes()))


# ---------------------------------------------------------------- children's chest X-rays
def load_kermany(ds: Datasets) -> Iterator[Candidate]:
    if not have_file(ds, "ZhangLabData.zip"):
        return
    rules = [
        (r"chest_xray/.*/NORMAL/", "chest_child", "normal", "", "Expert reading: normal lungs, no pneumonia."),
        (r"chest_xray/.*/PNEUMONIA/[^/]*bacteria", "chest_child", "abnormal", "pneumonia (bacterial)", "Expert reading: bacterial pneumonia."),
        (r"chest_xray/.*/PNEUMONIA/[^/]*virus", "chest_child", "abnormal", "pneumonia (viral)", "Expert reading: viral pneumonia."),
        (r"chest_xray/.*/PNEUMONIA/", "chest_child", "abnormal", "pneumonia / consolidation", "Expert reading: pneumonia."),
    ]
    yield from folder_cases(ds, "ZhangLabData.zip", "Kermany pediatric CXR", rules,
                            "Graded by two expert physicians, checked by a third (Kermany et al., Cell 2018)",
                            report_prefix="Chest X-ray of a child aged 1 to 5 (Guangzhou Women and Children's Medical Center).")


def load_ng_pneumonia(ds: Datasets) -> Iterator[Candidate]:
    rules = [
        (r"/NORMAL/", "chest_child", "normal", "", "Dataset label: normal chest."),
        (r"/PNEUMONIA/", "chest_child", "abnormal", "pneumonia / consolidation", "Dataset label: pneumonia."),
    ]
    yield from folder_cases(ds, "NG_Pneum_Dataset.zip", "Nigerian under-5 CXR", rules,
                            "Labels from the dataset authors (Under-5 Nigerian pneumonia CXR, Zenodo 14185822)",
                            report_prefix="Chest X-ray of a child under 5.")


# ---------------------------------------------------------------- GRAZPEDWRI-DX (children's wrists)
def load_grazpedwri(ds: Datasets) -> Iterator[Candidate]:
    meta = {r["filestem"]: r for r in csv.DictReader(io.StringIO(ds.text("GRAZPEDWRI_dataset.csv")))}
    parts = [f"GRAZPEDWRI_images_part{i}.zip" for i in range(1, 5)]
    parts = [n for n in parts if (ds.folder / n).exists()] or (parts[:2] if ds.remote else [])
    for name in parts:
        try:
            zf = ds.zip(name)
        except Exception as exc:
            print(f"  GRAZPEDWRI {name}: skipped ({exc})", flush=True)
            continue
        for info in zf.infolist():
            stem = PurePosixPath(info.filename).stem
            r = meta.get(stem)
            if info.is_dir() or not r or flag(r.get("diagnosis_uncertain", "")):
                continue
            ao = r.get("ao_classification", "")
            fracture, metal, cast, osteopenia = (flag(r.get(k, "")) for k in ("fracture_visible", "metal", "cast", "osteopenia"))
            if fracture:
                label = "abnormal"
                if "23r" in ao and "23u" in ao:
                    diagnosis = "fracture of both forearm bones near the wrist (distal radius and ulna, child)"
                elif "23u" in ao and "23r" not in ao:
                    diagnosis = "fracture of the ulna near the wrist (child)"
                else:
                    diagnosis = "fracture of the radius near the wrist (distal radius, child)"
            elif metal:
                label, diagnosis = "abnormal", "surgical hardware or implant"
            elif ao or cast:
                continue  # a cast or an AO code without a visible fracture: ambiguous, skip
            else:
                label, diagnosis = "normal", ""
            view = {"1": "front view (AP)", "2": "side view (lateral)"}.get(r.get("projection", ""), "")
            side = {"L": "left", "R": "right"}.get(r.get("laterality", ""), "")
            age = r.get("age", "")
            sex = {"M": "boy", "F": "girl"}.get(r.get("gender", ""), "child")
            extras = []
            if fracture and ao:
                extras.append(f"AO/OTA fracture code {ao}")
            if cast:
                extras.append("a cast is present")
            if metal:
                extras.append("metal hardware is present")
            if osteopenia:
                extras.append("bones look thinner than normal (osteopenia)")
            report = f"{(view + ' ') if view else ''}wrist X-ray of a {sex}{', age ' + age.split('.')[0] if age else ''}" \
                     f"{', ' + side + ' side' if side else ''}. Pediatric radiologist tags: " \
                     f"{'fracture visible' if fracture else 'no fracture'}{'; ' + '; '.join(extras) if extras else ''}."
            yield Candidate("GRAZPEDWRI-DX", "wrist_hand", label, diagnosis, report[0].upper() + report[1:],
                            "Pediatric radiologists at the University Hospital Graz (GRAZPEDWRI-DX)",
                            PurePosixPath(info.filename).name, zip_reader(zf, info.filename))


# ---------------------------------------------------------------- DENTEX (panoramic dental X-rays)
DENTEX_NAMES = [  # most serious first
    ("Periapical Lesion", "tooth decay or tooth infection", "an infection at the root tip (periapical lesion)"),
    ("Deep Caries", "tooth decay or tooth infection", "deep tooth decay (deep caries)"),
    ("Caries", "tooth decay or tooth infection", "tooth decay (caries)"),
    ("Impacted", "impacted tooth", "an impacted tooth"),
]


def load_dentex(ds: Datasets) -> Iterator[Candidate]:
    if not have_file(ds, "DENTEX_training_data.zip"):
        return
    try:
        zf = ds.zip("DENTEX_training_data.zip")
    except Exception as exc:
        print(f"  DENTEX: skipped ({exc})", flush=True)
        return
    ann_name = next((n for n in zf.namelist() if n.endswith("train_quadrant_enumeration_disease.json")), None)
    if not ann_name:
        print("  DENTEX: annotation file not found", flush=True)
        return
    data = json.loads(zf.read(ann_name))
    cats = {c["id"]: c["name"] for c in data.get("categories_3", [])}
    images = {im["id"]: im["file_name"] for im in data["images"]}
    per_image: dict[int, Counter] = defaultdict(Counter)
    for a in data["annotations"]:
        per_image[a["image_id"]][cats.get(a.get("category_id_3"), "")] += 1
    folder = PurePosixPath(ann_name).parent
    members = {PurePosixPath(n).name: n for n in zf.namelist() if str(folder) in n and n.lower().endswith(".png")}
    for image_id, fname in images.items():
        member = members.get(PurePosixPath(fname).name)
        if not member:
            continue
        counts = per_image.get(image_id, Counter())
        found = [(key, diag, text) for key, diag, text in DENTEX_NAMES if counts.get(key)]
        if not found:
            continue  # every image in this set has at least one marked tooth; skip anything unparsed
        pieces = [f"{counts[key]} with {text}" for key, _, text in found]
        n = sum(counts[key] for key, _, _ in found)
        report = f"Panoramic dental X-ray (shows all teeth and both jaws). Expert annotators marked {n} abnormal " \
                 f"{'tooth' if n == 1 else 'teeth'}: {', '.join(pieces)}."
        yield Candidate("DENTEX", "teeth_jaw", "abnormal", found[0][1], report,
                        "Tooth-by-tooth diagnosis labels from the DENTEX challenge's expert annotators (MICCAI 2023)",
                        "dentex-" + PurePosixPath(member).name, zip_reader(zf, member))


def load_dentiro(ds: Datasets) -> Iterator[Candidate]:
    rules = [
        (r"DentIRO/Healthy/", "teeth_jaw", "normal", "", "Dentists' label: healthy tooth."),
        (r"DentIRO/Caries/", "teeth_jaw", "abnormal", "tooth decay or tooth infection", "Dentists' label: caries (tooth decay)."),
        (r"DentIRO/Crowned/", "teeth_jaw", "abnormal", "dental crown (treated tooth)", "Dentists' label: crowned tooth."),
        (r"DentIRO/Root Canal/", "teeth_jaw", "abnormal", "root canal filling (treated tooth)", "Dentists' label: root canal treated tooth."),
    ]
    yield from folder_cases(ds, "DentIRO.zip", "DentIRO", rules, "Consensus of experienced dental practitioners (DentIRO)",
                            report_prefix="Small intraoral X-ray of a single tooth (periapical view).")


# ---------------------------------------------------------------- KUB-StoneX (abdomen / kidneys)
def load_kub_stonex(ds: Datasets) -> Iterator[Candidate]:
    if not have_file(ds, "KUB-StoneX.zip"):
        return
    rules = [
        (r"Kidney_Stone/Images/", "abdomen_kidneys", "abnormal", "kidney stone",
         "Radiologists marked a kidney stone."),
        (r"Ureteric_Stone/Images/", "abdomen_kidneys", "abnormal", "ureter stone (stone in the tube from kidney to bladder)",
         "Radiologists marked a stone in the ureter (the tube from the kidney to the bladder)."),
        (r"Bladder_Stone/Images/", "abdomen_kidneys", "abnormal", "bladder stone",
         "Radiologists marked a bladder stone."),
        (r"Normal_No_?Stone/Images/", "abdomen_kidneys", "normal", "", "Radiologists found no stone."),
    ]
    yield from folder_cases(ds, "KUB-StoneX.zip", "KUB-StoneX", rules,
                            "Consensus stone segmentation by two radiologists (KUB-StoneX)",
                            report_prefix="Kidney-ureter-bladder (KUB) abdominal X-ray, front view.")


# ---------------------------------------------------------------- knee osteoarthritis (OAI)
KL_GRADES = {
    "0": ("normal", "", "Kellgren-Lawrence grade 0: no signs of osteoarthritis."),
    "1": ("abnormal", "very early knee osteoarthritis (doubtful, KL grade 1)", "Kellgren-Lawrence grade 1: doubtful, very early joint wear."),
    "2": ("abnormal", "mild knee osteoarthritis (KL grade 2)", "Kellgren-Lawrence grade 2: mild osteoarthritis (small bone spurs)."),
    "3": ("abnormal", "moderate knee osteoarthritis (KL grade 3)", "Kellgren-Lawrence grade 3: moderate osteoarthritis (joint space narrowing)."),
    "4": ("abnormal", "severe knee osteoarthritis (KL grade 4)", "Kellgren-Lawrence grade 4: severe osteoarthritis (bone on bone)."),
}


def load_knee_oa(ds: Datasets) -> Iterator[Candidate]:
    if not have_file(ds, "KneeXrayData.zip"):
        return
    rules = [(rf"kneeKL299/(train|val|test|auto_test)/{g}/", "thigh_knee", lab, diag, text) for g, (lab, diag, text) in KL_GRADES.items()]
    yield from folder_cases(ds, "KneeXrayData.zip", "Knee OA (OAI)", rules,
                            "Kellgren-Lawrence grade read by the Osteoarthritis Initiative's trained readers",
                            report_prefix="Front-view X-ray of one knee joint, cropped to the joint.")


# ---------------------------------------------------------------- MURA (upper limb, normal vs abnormal)
MURA_PARTS = {
    "XR_SHOULDER": ("shoulder_upper_arm", "shoulder"), "XR_HUMERUS": ("shoulder_upper_arm", "upper arm (humerus)"),
    "XR_ELBOW": ("elbow_forearm", "elbow"), "XR_FOREARM": ("elbow_forearm", "forearm"),
    "XR_WRIST": ("wrist_hand", "wrist"), "XR_HAND": ("wrist_hand", "hand"), "XR_FINGER": ("wrist_hand", "finger"),
}


def load_mura(ds: Datasets) -> Iterator[Candidate]:
    if not have_file(ds, "MURA-v1.1.zip"):
        return
    rules = []
    for folder, (part, word) in MURA_PARTS.items():
        rules.append((rf"MURA-v1\.1/(train|valid)/{folder}/patient\d+/study\d+_negative/", part, "normal", "",
                      f"{word.capitalize()} X-ray study. Radiologists labeled the study normal."))
        rules.append((rf"MURA-v1\.1/(train|valid)/{folder}/patient\d+/study\d+_positive/", part, "abnormal",
                      "abnormal finding (MURA does not say which: fracture, hardware, lesion or joint change)",
                      f"{word.capitalize()} X-ray study. Radiologists labeled the study abnormal (MURA records only normal/abnormal, not the exact finding)."))
    yield from folder_cases(ds, "MURA-v1.1.zip", "MURA", rules, "Board-certified Stanford radiologists at the time of the exam (MURA v1.1)",
                            tag="mura", depth=2)


def load_pediashf(ds: Datasets) -> Iterator[Candidate]:
    try:
        zf = ds.zip("PediaSHF-DX.zip")
    except Exception as exc:
        print(f"  PediaSHF-DX: skipped ({exc})", flush=True)
        return
    labels = {PurePosixPath(n).stem: n for n in zf.namelist() if "/02.train_txt/" in n and n.endswith(".txt")}
    for info in zf.infolist():
        p = PurePosixPath(info.filename)
        if info.is_dir() or "/01.train/" not in info.filename or p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        txt = labels.get(p.stem)
        if txt is None:
            continue
        boxes = [ln for ln in zf.read(txt).decode("utf-8", "replace").splitlines() if ln.strip()]
        if not boxes:
            continue  # a handful of images carry no box: unknown status, not "normal"
        yield Candidate("PediaSHF-DX", "elbow_forearm", "abnormal", "fracture",
                        f"Elbow X-ray of a child. The dataset's annotators marked {len(boxes)} supracondylar humerus fracture "
                        f"{'site' if len(boxes) == 1 else 'sites'} (a break just above the elbow).",
                        "Fracture boxes drawn by the PediaSHF-DX annotators (figshare 29322869)",
                        "pediashf-" + p.name, zip_reader(zf, info.filename))


def load_uci_shoulder(ds: Datasets) -> Iterator[Candidate]:
    rules = [(r"(Cofield|Depuy|Tornier|Zimmer)", "shoulder_upper_arm", "abnormal", "surgical hardware or implant",
              "Shoulder X-ray showing a shoulder replacement (artificial joint). Label: implant present, manufacturer recorded.")]
    yield from folder_cases(ds, "UCI_shoulder_implants.zip", "UCI shoulder implants", rules,
                            "Implant manufacturer labels from the dataset authors (UCI ML Repository, dataset 517)")


# ---------------------------------------------------------------- brain MRI (.mat slices)
BRAIN_LABELS = {1: "brain tumor (meningioma)", 2: "brain tumor (glioma)", 3: "pituitary tumor"}


def mat_label(data: bytes) -> int:
    import h5py
    import numpy as np

    with h5py.File(io.BytesIO(data), "r") as f:
        return int(np.array(f["cjdata"]["label"]).ravel()[0])


def mat_slice_png(data: bytes) -> bytes:
    import h5py
    import numpy as np
    from PIL import Image

    with h5py.File(io.BytesIO(data), "r") as f:
        img = np.array(f["cjdata"]["image"], dtype=np.float32).T  # MATLAB stores column-major: transpose
    lo, hi = np.percentile(img, (0.5, 99.5))
    img = np.clip((img - lo) / max(hi - lo, 1e-6), 0, 1) * 255
    out = io.BytesIO()
    Image.fromarray(img.astype("uint8")).save(out, format="PNG")
    return out.getvalue()


def load_brain_tumor(ds: Datasets) -> Iterator[Candidate]:
    for i in range(1, 5):
        name = f"BrainTumor_{i}.zip"
        try:
            zf = ds.zip(name)
        except Exception as exc:
            print(f"  brain tumor {name}: skipped ({exc})", flush=True)
            continue
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".mat"):
                continue
            try:
                label = mat_label(zf.read(info.filename))
            except Exception:
                continue
            diagnosis = BRAIN_LABELS.get(label)
            if not diagnosis:
                continue
            report = f"Brain MRI slice (T1-weighted, with contrast). Labeled tumor type: {diagnosis}."
            member = info.filename
            yield Candidate("Brain tumor MRI", "skull_face_sinuses", "abnormal", diagnosis, report,
                            "Tumor type labeled by the dataset authors (Cheng et al., figshare 1512427; Nanfang Hospital and Tianjin Medical University General Hospital)",
                            "brain-" + PurePosixPath(member).stem + ".png",
                            (lambda m=member, z=zf: mat_slice_png(zip_reader(z, m)())), "mri")


# ---------------------------------------------------------------- neck: Cervical Spine X-ray Atlas
CSXA_CURVES = {1: "normal forward curve (lordotic)", 2: "straightened neck curve", 3: "S-shaped curve (sigmoid)",
               4: "reverse S-shaped curve (sigmoid)", 5: "reversed curve (kyphotic)"}


def load_csxa(ds: Datasets) -> Iterator[Candidate]:
    import openpyxl

    wb = openpyxl.load_workbook(ds.path("CSXA_labels.xlsx"), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    meta: dict[int, tuple] = {}
    for row in ws.iter_rows(min_row=3, values_only=True):
        try:
            meta[int(row[0])] = (row[1], row[2], int(row[3]), int(row[4]) if row[4] is not None else None)
        except (TypeError, ValueError):
            continue
    try:
        zf = ds.zip("CSXA_PNG.zip")
    except Exception as exc:
        print(f"  CSXA: skipped ({exc})", flush=True)
        return
    for info in zf.infolist():
        p = PurePosixPath(info.filename)
        if info.is_dir() or p.suffix.lower() != ".png" or not p.stem[:4].isdigit():
            continue
        m = meta.get(int(p.stem[:4]))
        if not m:
            continue
        sex_code, age, disease, curve = m
        sex = {0: "male", 1: "female"}.get(sex_code, "")
        who = ", ".join(x for x in (sex, f"age {age}" if age else "") if x)
        curve_text = CSXA_CURVES.get(curve, "")
        if disease == 1:
            label, diagnosis = "abnormal", "arthritis / joint wear"
            finding = "cervical spondylosis (age-related wear of the neck discs and joints)"
        elif disease == 2:
            label, diagnosis, finding = "normal", "", "healthy cervical spine"
        else:
            continue
        report = f"Side-view X-ray of the neck (cervical spine){', ' + who if who else ''}. Expert classification: {finding}." \
                 + (f" Neck alignment: {curve_text}." if curve_text else "")
        yield Candidate("CSXA", "neck_cervical_spine", label, diagnosis, report,
                        "Expert classification and measurements (Cervical Spine X-ray Atlas, Scientific Data 2024)",
                        p.name, zip_reader(zf, info.filename))


# ---------------------------------------------------------------- back: SPIDER lumbar MRI
def mha_mid_slice_png(data: bytes) -> bytes:
    """Middle sagittal slice of a spine MRI volume, shown the standard way: head up, front of the body left."""
    import SimpleITK as sitk
    import numpy as np
    from PIL import Image

    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"spider_{threading.get_ident()}.mha"
    tmp.write_bytes(data)
    try:
        img = sitk.ReadImage(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)
    img = sitk.DICOMOrient(img, "LPS")  # x: right->left, y: front->back, z: feet->head
    sx, sy, sz = img.GetSpacing()
    arr = sitk.GetArrayFromImage(img).astype(np.float32)  # (z, y, x)
    sl = np.flipud(arr[:, :, arr.shape[2] // 2])  # mid-sagittal slice, head at the top
    lo, hi = np.percentile(sl, (0.5, 99.5))
    sl = np.clip((sl - lo) / max(hi - lo, 1e-6), 0, 1) * 255
    im = Image.fromarray(sl.astype("uint8"))
    if sy > 0 and abs(sz / sy - 1) > 0.02:  # square pixels: correct the height for the slice spacing
        im = im.resize((im.width, max(1, round(im.height * sz / sy))))
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def load_spider(ds: Datasets) -> Iterator[Candidate]:
    if not have_file(ds, "SPIDER_images.zip"):
        return
    grades: dict[str, list[dict]] = defaultdict(list)
    for r in csv.DictReader(io.StringIO(ds.text("SPIDER_gradings.csv"))):
        grades[r["Patient"]].append(r)
    try:
        zf = ds.zip("SPIDER_images.zip")
    except Exception as exc:
        print(f"  SPIDER: skipped ({exc})", flush=True)
        return
    members = {PurePosixPath(n).stem: n for n in zf.namelist() if n.endswith(".mha")}
    for patient, rows in grades.items():
        member = members.get(f"{patient}_t2") or members.get(f"{patient}_t1")
        if not member:
            continue
        n = len(rows)
        hern = sum(flag(r["Disc herniation"]) for r in rows)
        listh = sum(flag(r["Spondylolisthesis"]) for r in rows)
        narrow = sum(flag(r["Disc narrowing"]) for r in rows)
        bulge = sum(flag(r["Disc bulging"]) for r in rows)
        modic = sum(1 for r in rows if r["Modic"].strip() not in ("", "0"))
        worn = sum(1 for r in rows if r["Pfirrman grade"].strip() in ("4", "5"))
        findings = []
        if hern:
            findings.append(f"{hern} herniated (slipped) disc{'s' if hern > 1 else ''}")
        if listh:
            findings.append(f"{listh} level{'s' if listh > 1 else ''} with a slipped vertebra (spondylolisthesis)")
        if narrow:
            findings.append(f"{narrow} narrowed disc{'s' if narrow > 1 else ''}")
        if bulge:
            findings.append(f"{bulge} bulging disc{'s' if bulge > 1 else ''}")
        if worn:
            findings.append(f"{worn} severely worn disc{'s' if worn > 1 else ''} (Pfirrmann grade 4-5)")
        if modic:
            findings.append(f"bone changes next to {modic} disc{'s' if modic > 1 else ''} (Modic changes)")
        if hern:
            diagnosis = "herniated (slipped) disc"
        elif listh:
            diagnosis = "slipped vertebra (spondylolisthesis)"
        elif narrow or bulge or worn or modic:
            diagnosis = "disc degeneration (worn discs)"
        else:
            diagnosis = ""
        seq = "T2-weighted" if member.endswith("_t2.mha") else "T1-weighted"
        report = f"Side-view (sagittal) MRI of the lower back (lumbar spine), {seq}. Expert per-disc grading of {n} discs: " \
                 + (", ".join(findings) + "." if findings else "no herniation, slip, narrowing or bulging.")
        yield Candidate("SPIDER lumbar MRI", "back_spine", "abnormal" if diagnosis else "normal", diagnosis, report,
                        "Per-disc radiological grading (SPIDER dataset, Zenodo 10159290)",
                        f"spider-{patient}-lumbar-mri.png", (lambda m=member, z=zf: mha_mid_slice_png(zip_reader(z, m)())), "mri")


LOADERS: dict[str, Callable[[Datasets], Iterator[Candidate]]] = {
    "fracatlas": load_fracatlas,
    "orthofrac": load_orthofrac,
    "btxrd": load_btxrd,
    "bdcxr": load_bdcxr,
    "iu_chest": load_iu_chest,
    "rocov2": load_rocov2,
    "mammo_mx": load_mammo_mx,
    # expansion sources (Nov 2026)
    "nih_chest": load_nih_chest,
    "kermany": load_kermany,
    "ng_pneumonia": load_ng_pneumonia,
    "grazpedwri": load_grazpedwri,
    "dentex": load_dentex,
    "dentiro": load_dentiro,
    "kub_stonex": load_kub_stonex,
    "knee_oa": load_knee_oa,
    "mura": load_mura,
    "pediashf": load_pediashf,
    "uci_shoulder": load_uci_shoulder,
    "brain_tumor": load_brain_tumor,
    "csxa": load_csxa,
    "spider": load_spider,
}


# ---------------------------------------------------------------- build
def case_slug(filename: str) -> str:
    """Same rule the library uses to name case folders, so we can tell what's already in it."""
    return re.sub(r"[^a-z0-9]+", "-", Path(filename).stem.lower()).strip("-")[:64] or "case"


def existing_slugs(out: Path) -> set[str]:
    return {p.name.rsplit("-", 1)[0] for p in out.glob("*/*/*") if p.is_dir()}


def sample_slugs(out: Path) -> set[str]:
    """The /try page's sample X-rays must stay out of the library (they are the held-out test images)."""
    manifest = out.parent / "samples" / "samples.json"
    try:
        items = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return set()
    return {case_slug(i["source_file"]) for i in items if isinstance(i, dict) and i.get("source_file")}


def plan_cases(
    names: list[str], ds: Datasets, per_class: int, seed: int, skip: set[str] | None = None
) -> list[Candidate]:
    rng = random.Random(seed)
    plan: list[Candidate] = []
    for name in names:
        started = time.monotonic()
        try:
            groups: dict[tuple[str, str, str], list[Candidate]] = defaultdict(list)
            for c in LOADERS[name](ds):
                if skip and case_slug(c.filename) in skip:
                    continue
                groups[(c.body_part, c.modality, c.diagnosis or "normal")].append(c)
        except Exception as exc:  # one broken source shouldn't stop the others
            print(f"! {name}: skipped ({exc})", flush=True)
            continue
        cap = max(1, round(per_class * CAP_FACTOR.get(name, 1.0)))
        picked = 0
        for key in sorted(groups):
            items = groups[key]
            rng.shuffle(items)
            plan.extend(items[:cap])
            picked += min(cap, len(items))
        total = sum(len(v) for v in groups.values())
        print(f"- {name}: {picked} of {total} labeled images picked ({time.monotonic() - started:.0f}s)", flush=True)
    return plan


def library_summary(lib: ReferenceLibrary, **extra) -> dict:
    """What the library holds, for data/reference/library_summary.json (informational, not used by the API)."""
    return {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "max_source_file_bytes": MAX_SOURCE_BYTES,
        "total_cases": len(lib.cases),
        **extra,
        "cases_by_modality": dict(Counter(c.modality for c in lib.cases).most_common()),
        "cases_by_source": dict(Counter(c.source for c in lib.cases).most_common()),
        "sources": {k: {"name": v[0], "license": v[1], "link": v[2]} for k, v in SOURCES_INFO.items()},
        "body_parts": {
            f"{part}/{modality}": dict(lib.conditions(part, modality))
            for part, modality in sorted({(c.body_part, c.modality) for c in lib.cases})
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasets", default="/tmp/datasets", help="folder for downloaded source files")
    ap.add_argument("--out", default=str(settings.reference_dir), help="reference library folder")
    ap.add_argument("--sources", default=",".join(LOADERS), help=f"comma list from: {', '.join(LOADERS)}")
    ap.add_argument("--per-class", type=int, default=25, help="max cases per body part + condition, per source")
    ap.add_argument("--max-side", type=int, default=768, help="stored image size (pixels, longest side)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--remote", action="store_true", help="read big zips over HTTP instead of downloading them")
    ap.add_argument("--clean", action="store_true", help="delete the existing library first")
    ap.add_argument("--dry-run", action="store_true", help="only count what would be added")
    args = ap.parse_args()

    names = [n.strip() for n in args.sources.split(",") if n.strip()]
    unknown = [n for n in names if n not in LOADERS]
    if unknown:
        sys.exit(f"Unknown sources: {unknown}. Choose from {list(LOADERS)}")

    ds = Datasets(Path(args.datasets), remote=args.remote)
    out = Path(args.out)
    skip = sample_slugs(out)
    if not args.clean:
        skip |= existing_slugs(out)  # keep the current library and only add new images
    print(f"Planning from {len(names)} sources (files up to {MAX_SOURCE_BYTES / 1e9:.0f} GB); skipping {len(skip)} images already used ...",
          flush=True)
    plan = plan_cases(names, ds, args.per_class, args.seed, skip)
    by_part = Counter(c.body_part for c in plan)
    print("\nPlanned cases per body part:")
    for part, n in sorted(by_part.items()):
        conds = Counter(c.diagnosis or "normal" for c in plan if c.body_part == part)
        print(f"  {part:22s} {n:5d}  " + ", ".join(f"{k} ({v})" for k, v in conds.most_common()))
    if args.dry_run:
        return

    if args.clean and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    lib = ReferenceLibrary(out)

    started = time.monotonic()
    ok, failed = Counter(), Counter()
    tally = threading.Lock()
    gate = threading.BoundedSemaphore(args.workers * 4)

    def work(c: Candidate) -> None:
        try:
            lib.add_case(image_bytes=c.read(), filename=c.filename, body_part=c.body_part, modality=c.modality,
                         report=c.report, label=c.label, diagnosis=c.diagnosis, source=c.source,
                         verified_by=c.verified_by, max_side=args.max_side, reload=False)
            with tally:
                ok[c.source] += 1
        except Exception as exc:
            with tally:
                failed[c.source] += 1
            if failed[c.source] <= 3:
                print(f"  ! {c.source} {c.filename}: {exc}", flush=True)
        finally:
            gate.release()

    with ThreadPoolExecutor(args.workers) as pool:
        for i, c in enumerate(plan, 1):
            gate.acquire()
            pool.submit(work, c)
            if i % 200 == 0:
                print(f"  {i}/{len(plan)} queued ({time.monotonic() - started:.0f}s)", flush=True)

    lib.reload()
    summary = library_summary(lib, added=dict(ok), failed=dict(failed))
    (out / "library_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nDone in {time.monotonic() - started:.0f}s: {sum(ok.values())} cases added, {sum(failed.values())} failed.")
    print(f"Library now holds {len(lib.cases)} verified cases. Summary: {out / 'library_summary.json'}")


if __name__ == "__main__":
    main()
