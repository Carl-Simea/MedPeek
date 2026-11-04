#!/usr/bin/env python3
"""Download reference-library source files in parallel (resumable), before running the builder.

    .venv/bin/python scripts/fetch_sources.py --list                 # what is planned, with sizes
    .venv/bin/python scripts/fetch_sources.py --groups all           # ~88 GB into /tmp/datasets
    .venv/bin/python scripts/fetch_sources.py --groups chest,wrist --parallel 3
    .venv/bin/python scripts/fetch_sources.py --files MammoMX_B5.zip  # any key from FILES

Groups are the "8 GB per body part" expansion plan. Everything is skipped if it is already on disk,
and partial downloads (.part) resume. Nothing here is ever committed to git.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_reference_library as b  # noqa: E402

GROUPS: dict[str, list[str]] = {
    # body-part group: source files (approx. sizes in the comments; sizes checked 2026-11-02)
    "chest": ["NIH_Data_Entry_2017_v2020.csv", "NIH_images_002.tar.gz", "NIH_images_003.tar.gz"],  # 7.9 GB
    "chest_child": ["ZhangLabData.zip", "NG_Pneum_Dataset.zip"],  # 8.6 GB
    "breast": ["MammoMX_Metadata.csv", "MammoMX_B1.zip", "MammoMX_B3.zip"],  # 8.8 GB (B5/B4 optional)
    "wrist": ["GRAZPEDWRI_dataset.csv", "GRAZPEDWRI_images_part1.zip", "GRAZPEDWRI_images_part2.zip"],  # 8.2 GB
    "teeth": ["DENTEX_training_data.zip", "DentIRO.zip"],  # 11.6 GB
    "abdomen": ["KUB-StoneX.zip"],  # 20.8 GB
    "knee": ["KneeXrayData.zip"],  # 7.2 GB
    "arm": ["MURA-v1.1.zip", "PediaSHF-DX.zip", "UCI_shoulder_implants.zip"],  # 4.3 GB
    "head": ["BrainTumor_1.zip", "BrainTumor_2.zip", "BrainTumor_3.zip", "BrainTumor_4.zip",
             "CSXA_PNG.zip", "CSXA_labels.xlsx"],  # 2.3 GB
    "spine": ["SPIDER_images.zip", "SPIDER_gradings.csv", "SPIDER_overview.csv"],  # 3.7 GB
    "roco": ["ROCOv2_train_images.zip", "ROCOv2_train_captions.csv", "ROCOv2_train_concepts_manual.csv"],  # 4.6 GB
}
SIZE_HINT_GB = {"chest": 7.9, "chest_child": 8.6, "breast": 8.8, "wrist": 8.2, "teeth": 11.6, "abdomen": 20.8,
                "knee": 7.2, "arm": 4.3, "head": 2.3, "spine": 3.7, "roco": 4.6}


def fetch(ds: b.Datasets, name: str, attempts: int = 6) -> tuple[str, float, str]:
    started = time.monotonic()
    for i in range(1, attempts + 1):
        try:
            path = ds.path(name)
            return name, path.stat().st_size / 1e9, f"ok in {time.monotonic() - started:.0f}s"
        except Exception as exc:  # network hiccups: wait and resume
            if i == attempts:
                return name, 0.0, f"FAILED: {exc}"
            time.sleep(min(60, 5 * i))
    return name, 0.0, "FAILED"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasets", default="/tmp/datasets")
    ap.add_argument("--groups", default="", help="comma list of groups, or 'all'")
    ap.add_argument("--files", default="", help="comma list of FILES keys")
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for g, names in GROUPS.items():
            print(f"{g:12s} ~{SIZE_HINT_GB[g]:5.1f} GB  {', '.join(names)}")
        print(f"{'total':12s} ~{sum(SIZE_HINT_GB.values()):5.1f} GB")
        return

    groups = list(GROUPS) if args.groups.strip() == "all" else [g.strip() for g in args.groups.split(",") if g.strip()]
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        sys.exit(f"Unknown groups {unknown}; choose from {list(GROUPS)} or 'all'")
    names: list[str] = []
    for g in groups:
        names += [n for n in GROUPS[g] if n not in names]
    for n in (x.strip() for x in args.files.split(",") if x.strip()):
        if n not in b.FILES:
            sys.exit(f"Unknown file {n}; choose from {list(b.FILES)}")
        if n not in names:
            names.append(n)
    if not names:
        sys.exit("Nothing to do: pass --groups and/or --files (or --list)")

    ds = b.Datasets(Path(args.datasets), remote=False)
    free = shutil.disk_usage(ds.folder).free / 1e9
    todo = [n for n in names if not (ds.folder / n).exists()]
    print(f"{len(todo)} of {len(names)} files to download into {ds.folder} ({free:.0f} GB free), {args.parallel} at a time", flush=True)
    started = time.monotonic()
    done_gb = 0.0
    with ThreadPoolExecutor(args.parallel) as pool:
        futures = [pool.submit(fetch, ds, n) for n in todo]
        for fut in as_completed(futures):
            name, gb, status = fut.result()
            done_gb += gb
            print(f"  {name}: {gb:.2f} GB {status}  [{done_gb:.1f} GB total, {time.monotonic() - started:.0f}s]", flush=True)
    present = [n for n in names if (ds.folder / n).exists()]
    total = sum((ds.folder / n).stat().st_size for n in present) / 1e9
    print(f"\n{len(present)}/{len(names)} files present, {total:.1f} GB on disk; {shutil.disk_usage(ds.folder).free / 1e9:.0f} GB free.")


if __name__ == "__main__":
    main()
