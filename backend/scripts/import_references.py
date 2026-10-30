"""Bulk-import verified cases into the reference library.

Two ways to point it at your dataset:

1) A folder where each image has a report next to it with the same name:
       dataset/
         case001.jpg   case001.txt
         case002.dcm   case002.txt
   python scripts/import_references.py dataset/ --body-part knee --modality xray \
       --source "Example Imaging Lab" --label abnormal

2) A CSV manifest with one row per case:
       image,report,body_part,modality,label,diagnosis,source,verified_by
       imgs/a.png,reports/a.txt,chest,xray,normal,,NIH,Dr. Smith
   ("report" can be a path to a .txt/.md/.pdf file or the report text itself)
   python scripts/import_references.py --csv manifest.csv

Images are re-encoded to PNG without metadata. Nothing here is uploaded to GitHub:
the library folder (backend/data/) is in .gitignore.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.body_parts import normalize_body_part, normalize_modality  # noqa: E402
from app.config import settings  # noqa: E402
from app.imaging import ImageError  # noqa: E402
from app.references import ReferenceLibrary  # noqa: E402

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".dcm", ".dicom", ".bmp", ".tif", ".tiff"}
REPORT_EXTS = [".txt", ".md", ".pdf"]


def read_report(value: str, base: Path) -> str:
    candidate = (base / value) if value and not Path(value).is_absolute() else Path(value or "")
    if value and candidate.suffix.lower() in REPORT_EXTS and candidate.exists():
        return read_report_file(candidate)
    return value


def read_report_file(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise SystemExit("Reading PDF reports needs pypdf: pip install pypdf")
        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages).strip()
    return path.read_text(errors="replace").strip()


def find_report(image: Path) -> Path | None:
    for ext in REPORT_EXTS:
        p = image.with_suffix(ext)
        if p.exists():
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", help="folder with images and same-name report files")
    ap.add_argument("--csv", help="CSV manifest instead of a folder")
    ap.add_argument("--body-part", help="body part for every case in the folder (e.g. knee)")
    ap.add_argument("--modality", default="xray", help="xray, mri or ct (default xray)")
    ap.add_argument("--label", default="unknown", help="normal, abnormal or unknown")
    ap.add_argument("--diagnosis", default="")
    ap.add_argument("--source", default="", help="lab, hospital or dataset name")
    ap.add_argument("--verified-by", default="")
    ap.add_argument("--library", default=str(settings.reference_dir), help="reference library folder")
    args = ap.parse_args()

    lib = ReferenceLibrary(Path(args.library))
    rows: list[dict] = []

    if args.csv:
        manifest = Path(args.csv)
        base = manifest.parent
        with manifest.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                rows.append({**row, "_image": base / row["image"], "_report": read_report(row.get("report", ""), base)})
    elif args.folder:
        if not args.body_part:
            ap.error("--body-part is required when importing a folder")
        for image in sorted(Path(args.folder).rglob("*")):
            if image.suffix.lower() not in IMAGE_EXTS:
                continue
            report_file = find_report(image)
            rows.append(
                {
                    "_image": image,
                    "_report": read_report_file(report_file) if report_file else "",
                    "body_part": args.body_part,
                    "modality": args.modality,
                    "label": args.label,
                    "diagnosis": args.diagnosis,
                    "source": args.source,
                    "verified_by": args.verified_by,
                }
            )
    else:
        ap.error("give a folder or --csv")

    added = skipped = 0
    for row in rows:
        image: Path = row["_image"]
        bp = normalize_body_part(row.get("body_part", ""))
        mod = normalize_modality(row.get("modality", "") or "xray")
        if not bp or not mod:
            print(f"skip {image}: unknown body part/modality ({row.get('body_part')}/{row.get('modality')})")
            skipped += 1
            continue
        if not row["_report"]:
            print(f"skip {image}: no report found (add {image.stem}.txt next to it)")
            skipped += 1
            continue
        try:
            case = lib.add_case(
                image_bytes=image.read_bytes(),
                filename=image.name,
                body_part=bp,
                modality=mod,
                report=row["_report"],
                label=row.get("label") or "unknown",
                diagnosis=row.get("diagnosis") or "",
                source=row.get("source") or "",
                verified_by=row.get("verified_by") or "",
                max_side=settings.max_image_side,
            )
        except (ImageError, OSError) as exc:
            print(f"skip {image}: {exc}")
            skipped += 1
            continue
        added += 1
        print(f"added {case.case_id}  [{bp}/{mod}/{case.label}]")

    print(f"\nDone: {added} added, {skipped} skipped. Library now has {len(lib.cases)} cases.")
    print("If the API is running, reload it: curl -X POST -H \"X-Admin-Token: $ADMIN_TOKEN\" localhost:8000/api/references/reload")
    return 0 if added or not rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
