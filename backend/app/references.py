"""Reference library: verified cases (image + report) that Gemma 4 compares against.

This is how the project "trains" Gemma without GPUs: before answering, the backend
pulls verified cases for the same body part and imaging type and puts them in
the prompt as examples (retrieval-augmented generation).

On disk (kept out of git, see .gitignore):

    data/reference/<body_part>/<modality>/<case_id>/
        image.png     normalized image (metadata stripped)
        case.json     {"case_id", "body_part", "modality", "label", "diagnosis",
                       "report", "source", "verified_by", "added_at"}
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .imaging import prepare_image

LABELS = {"normal", "abnormal", "unknown"}


@dataclass
class ReferenceCase:
    case_id: str
    body_part: str
    modality: str
    label: str
    diagnosis: str
    report: str
    source: str
    verified_by: str
    added_at: str
    image_path: str

    def public(self) -> dict:
        d = asdict(self)
        d.pop("image_path")
        d.pop("report")
        return d


class ReferenceLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._cases: list[ReferenceCase] = []
        self._lock = threading.Lock()
        self.reload()

    # ---------- loading ----------
    def reload(self) -> None:
        cases: list[ReferenceCase] = []
        if self.root.exists():
            for meta_file in sorted(self.root.glob("*/*/*/case.json")):
                try:
                    meta = json.loads(meta_file.read_text())
                    image = meta_file.parent / "image.png"
                    if not image.exists():
                        continue
                    cases.append(
                        ReferenceCase(
                            case_id=meta.get("case_id", meta_file.parent.name),
                            body_part=meta["body_part"],
                            modality=meta["modality"],
                            label=meta.get("label", "unknown"),
                            diagnosis=meta.get("diagnosis", ""),
                            report=meta.get("report", ""),
                            source=meta.get("source", ""),
                            verified_by=meta.get("verified_by", ""),
                            added_at=meta.get("added_at", ""),
                            image_path=str(image),
                        )
                    )
                except (KeyError, ValueError):
                    continue
        with self._lock:
            self._cases = cases

    @property
    def cases(self) -> list[ReferenceCase]:
        with self._lock:
            return list(self._cases)

    def stats(self) -> dict:
        out: dict[str, dict[str, dict[str, int]]] = {}
        for c in self.cases:
            bucket = out.setdefault(c.body_part, {}).setdefault(c.modality, {"total": 0, "normal": 0, "abnormal": 0})
            bucket["total"] += 1
            if c.label in ("normal", "abnormal"):
                bucket[c.label] += 1
        return out

    def count(self, body_part: str, modality: str | None = None) -> int:
        return sum(1 for c in self.cases if c.body_part == body_part and (modality is None or c.modality == modality))

    # ---------- retrieval ----------
    def retrieve(self, body_part: str, modality: str, k: int) -> list[ReferenceCase]:
        """Pick up to k cases for the same body part and imaging type.

        When both normal and abnormal examples exist, include one of each first so
        Gemma sees the contrast, then fill the rest by most recently added.
        """
        if k <= 0:
            return []
        pool = [c for c in self.cases if c.body_part == body_part and c.modality == modality]
        if not pool:
            return []
        pool.sort(key=lambda c: c.added_at, reverse=True)
        picked: list[ReferenceCase] = []
        for label in ("abnormal", "normal"):
            match = next((c for c in pool if c.label == label and c not in picked), None)
            if match and len(picked) < k:
                picked.append(match)
        for c in pool:
            if len(picked) >= k:
                break
            if c not in picked:
                picked.append(c)
        return picked

    def image_b64(self, case: ReferenceCase) -> str:
        import base64

        return base64.b64encode(Path(case.image_path).read_bytes()).decode("ascii")

    # ---------- adding ----------
    def add_case(
        self,
        *,
        image_bytes: bytes,
        filename: str,
        body_part: str,
        modality: str,
        report: str,
        label: str = "unknown",
        diagnosis: str = "",
        source: str = "",
        verified_by: str = "",
        max_side: int = 1024,
    ) -> ReferenceCase:
        label = (label or "unknown").strip().lower()
        if label not in LABELS:
            label = "unknown"
        prepared = prepare_image(image_bytes, filename, max_side=max_side)
        slug = re.sub(r"[^a-z0-9]+", "-", Path(filename).stem.lower()).strip("-")[:40] or "case"
        case_id = f"{slug}-{uuid.uuid4().hex[:6]}"
        folder = self.root / body_part / modality / case_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "image.png").write_bytes(prepared.png_bytes)
        meta = {
            "case_id": case_id,
            "body_part": body_part,
            "modality": modality,
            "label": label,
            "diagnosis": diagnosis.strip(),
            "report": report.strip(),
            "source": source.strip(),
            "verified_by": verified_by.strip(),
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (folder / "case.json").write_text(json.dumps(meta, indent=2))
        self.reload()
        return next(c for c in self.cases if c.case_id == case_id)
