"""Build MedPeek's database (images.db). Run once from the MedPeek folder:

    python3 create_db.py

It creates the users / messages / images tables, downloads FracAtlas (about 320 MB, once),
and loads 20 normal adult-style hand X-rays as the references Gemma compares against.
It also saves 5 broken hand X-rays in hands/broken/ to test uploads with, plus
hands/broken/answers.csv saying where each break is.

If a reference looks like a child's hand (growth plates show as dark gaps near the ends of
the bones), put its IMG id in hands/skip.txt, one per line, and run this again.

Images: Abedeen et al., "FracAtlas", Scientific Data 10, 521 (2023), CC BY 4.0.
"""
import csv
import io
import json
import random
import shutil
import sqlite3
import urllib.request
import zipfile
from pathlib import Path

from services import db

ARTICLE_API = "https://api.figshare.com/v2/articles/22363012"
ZIP_PATH = Path("FracAtlas.zip")
OUT = Path("hands")
N_NORMAL = 20
N_BROKEN = 5
SEED = 7
HEADERS = {"User-Agent": "MedPeek/1.0"}
SOURCE = "FracAtlas (Abedeen et al., Scientific Data 2023), CC BY 4.0"
SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def download_zip():
    if ZIP_PATH.exists():
        print(f"Using {ZIP_PATH} that is already here")
        return
    print("Looking up FracAtlas on figshare...")
    with urllib.request.urlopen(urllib.request.Request(ARTICLE_API, headers=HEADERS), timeout=60) as r:
        files = json.load(r)["files"]
    info = next(f for f in files if f["name"].lower().endswith(".zip"))
    print(f"Downloading {info['name']} ({info['size'] / 1e6:.0f} MB), this takes a minute...")
    part = ZIP_PATH.with_suffix(".part")
    request = urllib.request.Request(info["download_url"], headers=HEADERS)
    with urllib.request.urlopen(request, timeout=120) as r, open(part, "wb") as out:
        shutil.copyfileobj(r, out, 1 << 20)
    part.rename(ZIP_PATH)


def jpeg_size(data):
    """(width, height) of a JPEG, or None if the file is damaged."""
    if data[:2] != b"\xff\xd8" or b"\xff\xd9" not in data[-4096:]:
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i += 2
            continue
        if marker in SOF_MARKERS:
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            return (width, height) if width and height else None
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def is_clean_hand(row):
    """Hand only, one front view, no metal implants."""
    return (row["hand"] == "1" and row["leg"] == "0" and row["hip"] == "0" and row["shoulder"] == "0"
            and row["mixed"] == "0" and row["multiscan"] == "0" and row["hardware"] == "0"
            and row["frontal"] == "1" and row["lateral"] == "0" and row["oblique"] == "0")


def pick(z, images, ids, count, rng):
    """Shuffle the ids and return the first `count` that are readable JPEGs."""
    ids = sorted(ids)
    rng.shuffle(ids)
    chosen = []
    for image_id in ids:
        if image_id not in images:
            continue
        data = z.read(images[image_id])
        size = jpeg_size(data)
        if size:
            chosen.append((image_id, data, size))
        if len(chosen) == count:
            break
    return chosen


def main():
    db.init_db()
    download_zip()
    skip_file = OUT / "skip.txt"
    skip = set(skip_file.read_text().split()) if skip_file.exists() else set()
    skip = {s if s.lower().endswith(".jpg") else s + ".jpg" for s in skip}

    with zipfile.ZipFile(ZIP_PATH) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        csv_path = min((n for n in names if n.endswith("dataset.csv")), key=len)
        images = {Path(n).name: n for n in names if "/images/" in n and n.lower().endswith(".jpg")}
        labels = {Path(n).name: n for n in names if "/YOLO/" in n and n.endswith(".txt")}
        with z.open(csv_path) as f:
            rows = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))
        hands = [r for r in rows if is_clean_hand(r) and r["image_id"] not in skip]

        normal = pick(z, images, [r["image_id"] for r in hands if r["fractured"] == "0"], N_NORMAL,
                      random.Random(SEED))
        broken = pick(z, images, [r["image_id"] for r in hands if r["fractured"] == "1"], N_BROKEN,
                      random.Random(SEED + 1))

        for folder in ("normal", "broken"):
            shutil.rmtree(OUT / folder, ignore_errors=True)
            (OUT / folder).mkdir(parents=True)

        with db.connect() as conn:
            conn.execute("DELETE FROM images WHERE body_part = 'hand'")
            for n, (image_id, data, _) in enumerate(normal, start=1):
                title = f"normal-{n:02d}_{image_id}"
                (OUT / "normal" / title).write_bytes(data)
                conn.execute("INSERT INTO images (title, body_part, image, source) VALUES (?, 'hand', ?, ?)",
                             (title, sqlite3.Binary(data), SOURCE))

        with open(OUT / "broken" / "answers.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["file", "width", "height", "breaks", "boxes_x1_y1_x2_y2"])
            for n, (image_id, data, (w, h)) in enumerate(broken, start=1):
                file_name = f"broken-{n:02d}_{image_id}"
                (OUT / "broken" / file_name).write_bytes(data)
                label_name = Path(image_id).with_suffix(".txt").name
                lines = z.read(labels[label_name]).decode().split("\n") if label_name in labels else []
                boxes = []
                for line in lines:
                    parts = line.split()
                    if len(parts) != 5:
                        continue
                    xc, yc, bw, bh = (float(p) for p in parts[1:])
                    boxes.append(f"{round((xc - bw / 2) * w)},{round((yc - bh / 2) * h)},"
                                 f"{round((xc + bw / 2) * w)},{round((yc + bh / 2) * h)}")
                writer.writerow([file_name, w, h, len(boxes), ";".join(boxes)])

    (OUT / "CREDITS.txt").write_text(f"Images from {SOURCE}. Data: https://doi.org/10.6084/m9.figshare.22363012\n")

    with db.connect() as conn:
        counts = conn.execute("SELECT body_part, count(*) FROM images GROUP BY body_part").fetchall()
    print(f"Database ready: {db.DB_PATH}")
    for part, count in counts:
        print(f"  {count} normal {part} X-rays")
    print(f"{len(broken)} broken hands to test with in {OUT / 'broken'} (answers.csv says where the breaks are)")
    if skip:
        print(f"Skipped {len(skip)} id(s) listed in {skip_file}")


if __name__ == "__main__":
    main()