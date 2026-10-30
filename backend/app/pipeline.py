"""One analysis, end to end: similar verified cases -> prompt -> Gemma 4 -> safety rules."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from . import gemma
from .body_parts import body_part_label, modality_label
from .config import settings
from .imaging import PreparedImage, thumbnail_vector
from .prompts import build_user_prompt
from .references import ReferenceLibrary
from .safety import finalize


def run_analysis(
    *,
    image: PreparedImage,
    body_part: str,
    modality: str,
    question: str,
    library: ReferenceLibrary,
) -> dict:
    started = time.monotonic()
    refs = library.retrieve(body_part, modality, settings.max_reference_cases, query_vector=thumbnail_vector(image.png_bytes))
    covered = library.conditions(body_part, modality)

    if settings.mock_gemma:
        raw = gemma.mock_answer(body_part, modality)
    else:
        attach = settings.send_reference_images and bool(refs)
        images = [library.image_b64(c) for c in refs] if attach else []
        images.append(image.b64)  # the patient's image always goes last
        prompt = build_user_prompt(
            body_part=body_part,
            modality=modality,
            question=question,
            references=refs,
            reference_images_attached=attach,
            covered_conditions=covered,
            sources=library.sources(body_part, modality),
        )
        raw, _timing = gemma.analyze(prompt, images)

    final = finalize(raw, body_part=body_part, modality=modality, references_used=len(refs))
    return {
        "id": uuid.uuid4().hex[:12],
        "body_part": body_part,
        "body_part_label": body_part_label(body_part),
        "modality": modality,
        "modality_label": modality_label(modality),
        "question": question,
        **final,
        "references_used": [c.public() for c in refs],
        "reference_library_size": sum(n for _, n in covered),
        "model": settings.gemma_model,
        "mock": settings.mock_gemma,
        "processing_seconds": round(time.monotonic() - started, 1),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
