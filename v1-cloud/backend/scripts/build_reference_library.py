#!/usr/bin/env python3
"""Build the verified reference library that Gemma 4 checks every patient image against.

Sources are open, labeled X-ray datasets. Every source file is under 1 GB: the script checks
each file's size before downloading and refuses anything bigger. Images stay on this machine
under backend/data/reference/ (git-ignored); nothing from the datasets is committed.

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

MAX_SOURCE_BYTES = 1_000_000_000  # the "under 1 GB per file" rule
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
    """Finds each source file locally, or downloads it (only if it is under 1 GB)."""

    def __init__(self, folder: Path, remote: bool):
        self.folder = folder
        self.remote = remote
        self.folder.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        path = self.folder / name
        if path.exists() and path.stat().st_size > 0:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise RuntimeError(f"{name} is {path.stat().st_size / 1e9:.2f} GB, over the 1 GB limit")
            return path
        url = FILES[name]
        print(f"  downloading {name} ...", flush=True)
        tmp = path.with_name(path.name + ".part")
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60.0, read=300.0)) as r:
            r.raise_for_status()
            size = int(r.headers.get("content-length") or 0)
            if size > MAX_SOURCE_BYTES:
                raise RuntimeError(f"{name} is {size / 1e9:.2f} GB, over the 1 GB limit - skipped")
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    done += len(chunk)
                    if done > MAX_SOURCE_BYTES:
                        raise RuntimeError(f"{name} is over the 1 GB limit - skipped")
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
                raise RuntimeError(f"{name} is {size / 1e9:.2f} GB, over the 1 GB limit")
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


def load_rocov2(ds: Datasets) -> Iterator[Candidate]:
    for split in ("valid", "test"):
        captions = {r["ID"]: r["Caption"] for r in csv.DictReader(io.StringIO(ds.text(f"ROCOv2_{split}_captions.csv")))}
        wanted: dict[str, tuple[str, str]] = {}
        for r in csv.DictReader(io.StringIO(ds.text(f"ROCOv2_{split}_concepts_manual.csv"))):
            cuis = [c.strip() for c in r["CUIs"].split(";") if c.strip()]
            if not cuis or cuis[0] != ROCO_PLAIN_XRAY or r["ID"] not in captions:
                continue
            caption = clean(captions[r["ID"]], 500)
            part = roco_body_part(set(cuis) & ROCO_REGIONS, caption)
            if part:
                wanted[r["ID"]] = (part, caption)
        zf = ds.zip(f"ROCOv2_{split}_images.zip")
        idx = index_zip(zf)
        for image_id, (part, caption) in wanted.items():
            member = idx.get(norm(image_id))
            found = roco_diagnosis(part, caption) if member else None
            if not found:
                continue
            label, diagnosis = found
            yield Candidate("ROCOv2", part, label, diagnosis, f"Published case-report caption: {caption}",
                            "Caption written by the authors of a peer-reviewed case report (ROCOv2 / PubMed Central)",
                            PurePosixPath(member).name, zip_reader(zf, member))


# ---------------------------------------------------------------- Mammo-MX (mammograms, BI-RADS)
DENSITY = {"D1": "almost entirely fatty", "D2": "scattered dense areas", "D3": "dense in many areas",
           "D4": "extremely dense"}
BIRADS = {
    "B6": ("breast cancer (biopsy-proven)", "BI-RADS 6, cancer already confirmed by biopsy"),
    "B0": ("unclear mammogram that needs more imaging", "BI-RADS 0, incomplete: more imaging needed"),
}


def load_mammo_mx(ds: Datasets) -> Iterator[Candidate]:
    meta = {norm(r["ImageID"]): r for r in csv.DictReader(io.StringIO(ds.text("MammoMX_Metadata.csv")))}
    for name, code in (("MammoMX_B6.zip", "B6"), ("MammoMX_B0.zip", "B0")):
        zf = ds.zip(name)
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
            yield Candidate("Mammo-MX", "breast", "abnormal", diagnosis, report[0].upper() + report[1:],
                            "Radiologist BI-RADS assessment (Mammo-MX dataset)",
                            PurePosixPath(member).name + ".dcm", zip_reader(zf, member))


LOADERS: dict[str, Callable[[Datasets], Iterator[Candidate]]] = {
    "fracatlas": load_fracatlas,
    "orthofrac": load_orthofrac,
    "btxrd": load_btxrd,
    "bdcxr": load_bdcxr,
    "iu_chest": load_iu_chest,
    "rocov2": load_rocov2,
    "mammo_mx": load_mammo_mx,
}


# ---------------------------------------------------------------- build
def case_slug(filename: str) -> str:
    """Same rule the library uses to name case folders, so we can tell what's already in it."""
    return re.sub(r"[^a-z0-9]+", "-", Path(filename).stem.lower()).strip("-")[:40] or "case"


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
            groups: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
            for c in LOADERS[name](ds):
                if skip and case_slug(c.filename) in skip:
                    continue
                groups[(c.body_part, c.diagnosis or "normal")].append(c)
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
    print(f"Planning from {len(names)} sources (each file < 1 GB); skipping {len(skip)} images already used ...",
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
            lib.add_case(image_bytes=c.read(), filename=c.filename, body_part=c.body_part, modality="xray",
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
    summary = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "max_source_file_bytes": MAX_SOURCE_BYTES,
        "total_cases": len(lib.cases),
        "added": dict(ok),
        "failed": dict(failed),
        "sources": {k: {"name": v[0], "license": v[1], "link": v[2]} for k, v in SOURCES_INFO.items() if k in names},
        "body_parts": {
            part: dict(lib.conditions(part, "xray")) for part in sorted({c.body_part for c in lib.cases})
        },
    }
    (out / "library_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nDone in {time.monotonic() - started:.0f}s: {sum(ok.values())} cases added, {sum(failed.values())} failed.")
    print(f"Library now holds {len(lib.cases)} verified cases. Summary: {out / 'library_summary.json'}")


if __name__ == "__main__":
    main()
