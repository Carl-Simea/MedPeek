"""What we tell Gemma 4, and the JSON shape it must answer in."""

from __future__ import annotations

from .body_parts import body_part_label, modality_label
from .references import ReferenceCase

SYSTEM_PROMPT = """You are the image-reading assistant inside a patient app. You run on Gemma 4.
A patient uploads one of their own medical images (X-ray, MRI or CT). You explain, in plain language, what is visible.

How to answer:
- Be direct and specific about what you can actually see: name the structures, say what looks normal and what does not.
- Give an honest confidence for every finding and overall:
  high = clearly visible and matches the verified reference cases;
  moderate = likely, but the image or your certainty is limited;
  low = unclear, subtle, or you cannot tell from this image.
  Never sound more certain than the image allows, and never invent findings to have something to say.
- Use the verified reference cases only to compare what normal and abnormal look like for this body part.
  Do not copy their findings unless you see the same thing on the patient's image.
- If the image is blurry, cropped, a photo of a screen, the wrong body part, or not a medical image at all, say so,
  set image_quality accordingly and keep confidence low.
- Urgency: "emergency" only for signs that can be life- or limb-threatening if not treated today
  (for example a collapsed lung, free air under the diaphragm, a clearly displaced fracture, a large bleed);
  "urgent" = see a doctor within 1-2 days; "soon" = book an appointment; "routine" = discuss at the next visit.
- You are not the patient's doctor and this is not a diagnosis. A radiologist's official report always takes priority.
  Suggest concrete next steps and good questions to ask their doctor.
- Write at an 8th-grade reading level. Short sentences. Explain any medical word you use.
- Reply with JSON only, following the schema exactly."""

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "is_medical_image": {"type": "boolean"},
        "detected_body_part": {"type": "string"},
        "image_quality": {"type": "string", "enum": ["good", "limited", "unreadable"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding": {"type": "string"},
                    "location": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "moderate", "low"]},
                    "explanation": {"type": "string"},
                },
                "required": ["finding", "location", "confidence", "explanation"],
            },
        },
        "overall_confidence": {"type": "string", "enum": ["high", "moderate", "low"]},
        "confidence_reason": {"type": "string"},
        "urgency": {"type": "string", "enum": ["routine", "soon", "urgent", "emergency"]},
        "reference_comparison": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "questions_for_doctor": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "is_medical_image",
        "detected_body_part",
        "image_quality",
        "summary",
        "findings",
        "overall_confidence",
        "confidence_reason",
        "urgency",
        "reference_comparison",
        "next_steps",
        "questions_for_doctor",
    ],
}


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def build_user_prompt(
    *,
    body_part: str,
    modality: str,
    question: str,
    references: list[ReferenceCase],
    reference_images_attached: bool,
) -> str:
    bp, mod = body_part_label(body_part), modality_label(modality)
    q = _clip(question, 500) or "What does my image show?"
    lines = [
        f"The patient says this is a {mod} of the {bp}.",
        f'Patient\'s question: "{q}"',
        "",
    ]
    if references:
        n = len(references)
        if reference_images_attached:
            lines.append(
                f"You are given {n + 1} images. Images 1 to {n} are VERIFIED REFERENCE CASES from accredited labs, "
                f"for comparison only. Image {n + 1} (the last one) is the PATIENT'S image."
            )
        else:
            lines.append("You are given 1 image: the PATIENT'S image. Verified reference reports for comparison:")
        for i, case in enumerate(references, start=1):
            tag = case.label
            if case.diagnosis and case.diagnosis.lower() != case.label:
                tag += f", {case.diagnosis}"
            text = f"- Reference case {i} ({tag})."
            if case.report:
                text += f' Verified report: "{_clip(case.report, 900)}"'
            lines.append(text)
    else:
        lines.append(
            "You are given 1 image: the PATIENT'S image. There are no verified reference cases for this body part yet, "
            "so be extra careful with confidence."
        )
    lines += [
        "",
        "Examine the PATIENT'S image and answer the patient's question. Return JSON that follows the schema: "
        "a 2-4 sentence summary, up to 5 findings, honest confidence, urgency, up to 4 next steps and "
        "up to 4 questions for their doctor. In reference_comparison, say in one or two sentences how the "
        "patient's image compares with the reference cases (or say none were available).",
    ]
    return "\n".join(lines)
