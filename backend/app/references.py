"""Reference library: verified cases (image + report or expert label) that Gemma 4 compares against.

This is how the project "trains" Gemma without GPUs: before answering, the backend
finds the verified cases that look most like the patient's image (same body part and
imaging type) and puts them in the prompt as examples, together with a summary of
which conditions the verified data covers (retrieval-augmented generation).

On disk (kept out of git, see .gitignore):

    data/reference/<body_part>/<modality>/<case_id>/
        image.png     normalized image (metadata stripped)
        vec.npy       small picture fingerprint for similarity search
        case.json     {"case_id", "body_part", "modality", "label", "diagnosis",
                       "report", "source", "verified_by", "added_at"}
"""

from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .imaging import prepare_image, thumbnail_vector

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
    vector: np.ndarray | None = field(default=None, repr=False, compare=False)

    def public(self) -> dict:
        d = asdict(self)
        for private in ("image_path", "report", "vector"):
            d.pop(private, None)
        return d


def condition_name(case: ReferenceCase) -> str:
    if case.label == "normal":
        return "normal (no abnormality)"
    return (case.diagnosis or case.label or "unknown").strip().lower()


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
                    vec_file = meta_file.parent / "vec.npy"
                    vector = np.load(vec_file) if vec_file.exists() else None
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
                            vector=vector,
                        )
                    )
                except (KeyError, ValueError, OSError):
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

    def conditions(self, body_part: str, modality: str) -> list[tuple[str, int]]:
        """Which conditions the verified data covers for this body part, most common first."""
        counts = Counter(condition_name(c) for c in self.cases if c.body_part == body_part and c.modality == modality)
        return counts.most_common()

    def sources(self, body_part: str, modality: str) -> list[str]:
        return sorted({c.source for c in self.cases if c.body_part == body_part and c.modality == modality and c.source})

    # ---------- retrieval ----------
    def retrieve(self, body_part: str, modality: str, k: int, query_vector: np.ndarray | None = None) -> list[ReferenceCase]:
        """Pick up to k verified cases for the same body part and imaging type.

        With a picture fingerprint, the most similar abnormal case and the most similar
        normal case come first (so Gemma sees the contrast), then the next most similar.
        Without one, fall back to one abnormal + one normal, most recently added.
        """
        if k <= 0:
            return []
        pool = [c for c in self.cases if c.body_part == body_part and c.modality == modality]
        if not pool:
            return []

        if query_vector is not None and all(c.vector is not None and c.vector.shape == query_vector.shape for c in pool):
            sims = np.stack([c.vector for c in pool]) @ query_vector
            ranked = [pool[i] for i in np.argsort(-sims)]
        else:
            ranked = sorted(pool, key=lambda c: c.added_at, reverse=True)

        picked: list[ReferenceCase] = []
        for label in ("abnormal", "normal"):
            match = next((c for c in ranked if c.label == label and c not in picked), None)
            if match and len(picked) < k:
                picked.append(match)
        for c in ranked:
            if len(picked) >= k:
                break
            if c not in picked:
                picked.append(c)
        return picked

    def guess_body_part(self, modality: str, query_vector: np.ndarray, k: int = 25) -> str | None:
        """Vote among the k most similar verified images (any body part) to guess what the image shows."""
        pool = [c for c in self.cases if c.modality == modality and c.vector is not None
                and c.vector.shape == query_vector.shape]
        if not pool:
            return None
        sims = np.stack([c.vector for c in pool]) @ query_vector
        votes: dict[str, float] = {}
        for i in np.argsort(-sims)[:k]:
            votes[pool[i].body_part] = votes.get(pool[i].body_part, 0.0) + max(float(sims[i]), 0.0)
        return max(votes, key=votes.get) if votes else None

    def image_b64(self, case: ReferenceCase) -> str:
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
        reload: bool = True,
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
        np.save(folder / "vec.npy", thumbnail_vector(prepared.png_bytes))
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
        case = ReferenceCase(**meta, image_path=str(folder / "image.png"), vector=np.load(folder / "vec.npy"))
        if reload:
            self.reload()
        return case
