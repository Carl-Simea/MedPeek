#!/usr/bin/env python3
"""Copy a few held-out X-rays (images NOT in the reference library) into data/samples/ for the /try page.

Run from backend/ after build_reference_library.py:
    .venv/bin/python scripts/make_samples.py [/tmp/datasets]
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.config import settings  # noqa: E402


def flag(v) -> bool:
    return str(v).strip().lower() in ("1", "1.0", "true")


def slug(filename: str) -> str:  # same rule the library uses for case folders
    return re.sub(r"[^a-z0-9]+", "-", Path(filename).stem.lower()).strip("-")[:40] or "case"


def main() -> None:
    datasets = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/datasets")
    library = settings.reference_dir
    used = {p.name.rsplit("-", 1)[0] for p in library.glob("*/*/*") if p.is_dir()}
    out = library.parent / "samples"
    out.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []

    def add(name: str, data: bytes, title: str, body_part: str, question: str, credit: str, source_file: str) -> None:
        (out / name).write_bytes(data)
        items.append({"file": name, "title": title, "body_part": body_part, "question": question, "credit": credit,
                      "source_file": source_file})
        print(f"added {name}")

    # 1. Wrist after a fall (FracAtlas, fracture)
    z = zipfile.ZipFile(datasets / "FracAtlas.zip")
    rows = list(csv.DictReader(io.StringIO(z.read("FracAtlas/dataset.csv").decode("utf-8-sig"))))
    picks = [r for r in rows if flag(r["hand"]) and flag(r["fractured"]) and not flag(r["mixed"])
             and slug(r["image_id"]) not in used]
    r = picks[min(3, len(picks) - 1)]
    add("wrist_after_fall.jpg", z.read(f"FracAtlas/images/Fractured/{r['image_id']}"), "Wrist after a fall",
        "wrist_hand", "I fell on my hand and it hurts. Is it broken?", "FracAtlas (CC BY 4.0)", r["image_id"])

    # 2. Child with cough and fever (BDCXR-3257, bacterial pneumonia)
    z = zipfile.ZipFile(datasets / "BDCXR-3257.zip")
    names = {slug(n): n for n in z.namelist() if n.lower().endswith(".jpg")}
    rows = list(csv.DictReader(io.StringIO(z.read("BDCXR-3257/metadata.csv").decode("utf-8-sig"))))
    for r in rows:
        key = slug(r["Filename"].replace("-", "_"))
        if r["Class"] == "Pneumonia Bacterial" and key in names and key not in used:
            add("child_cough_fever.jpg", z.read(names[key]), "Child with cough and fever", "chest_child",
                "My child has had a cough and fever for 3 days. What does the X-ray show?", "BDCXR-3257 (CC BY 4.0)",
                Path(names[key]).name)
            break

    # 3. Lump above the knee (BTXRD, bone tumor of the thigh bone)
    import openpyxl

    z = zipfile.ZipFile(datasets / "BTXRD.zip")
    ws = openpyxl.load_workbook(io.BytesIO(z.read("BTXRD/dataset.xlsx")), read_only=True).active
    it = ws.iter_rows(values_only=True)
    header = [str(h).strip() for h in next(it)]
    rows = [dict(zip(header, v)) for v in it]
    picks = [r for r in rows if flag(r.get("osteosarcoma")) and flag(r.get("femur")) and slug(str(r["image_id"])) not in used]
    if picks:
        r = picks[min(2, len(picks) - 1)]
        add("lump_above_knee.jpeg", z.read(f"BTXRD/images/{r['image_id']}"), "Lump above the knee", "thigh_knee",
            "There is a hard, painful lump above my knee. What is it?", "BTXRD (CC BY 4.0)", str(r["image_id"]))

    (out / "samples.json").write_text(json.dumps(items, indent=2))
    print(f"{len(items)} samples in {out}")


if __name__ == "__main__":
    main()
