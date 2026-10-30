"""What we tell Gemma 4, and the JSON shape it must answer in."""

from __future__ import annotations

from .body_parts import SPECIALISTS, body_part_label, modality_label
from .references import ReferenceCase

SYSTEM_PROMPT = """You are the image-reading assistant inside a patient app. You run on Gemma 4.
A patient uploads one of their own medical images (usually an X-ray). Before you answer, you are shown
verified reference cases from published, expert-labeled datasets, and a list of the conditions that verified data covers.

Write a strong, clear, well-organized answer for the patient:
- Lead with the main point in plain words: what the image shows and what it most likely means for them.
- Be specific: name the bone, joint or organ, the side and the area, and describe what you see.
- Explain any medical word the first time you use it. Short sentences, 8th-grade reading level, calm and respectful tone.
- Never invent findings, and never sound more certain than the image allows.

Grounding in the verified data:
- Compare the patient's image with the reference cases. Use them to judge what normal and abnormal look like.
- Set condition_in_reference_data to true only if the patient's main condition (or "normal") is one of the conditions
  listed as covered by the verified data AND the patient's image resembles those cases. Otherwise set it to false.
- For each finding, set supported_by_reference_data the same way.
- Give a confidence (high / moderate / low) only for what the verified data supports; it will be hidden from the
  patient when the condition is not covered.

Referral (always fill it in):
- Choose the specialist who usually treats this kind of problem (for example a broken bone -> orthopedic surgeon,
  a suspected tumor -> orthopedic oncologist or oncologist, pneumonia -> pulmonologist or pediatrician for a child,
  a kidney stone -> urologist, a tooth infection -> dentist or endodontist, a breast lump -> breast surgeon).
- If the image looks normal, refer to a primary care doctor for routine follow-up.
- Say why, and how soon (today, within a few days, or at the next routine visit).

Safety:
- Urgency "emergency" only for signs that can be life- or limb-threatening if not treated today
  (a collapsed lung, free air under the diaphragm, a clearly displaced fracture, a large bleed); then refer to the
  emergency department. "urgent" = see a doctor within 1-2 days; "soon" = book an appointment; "routine" = next visit.
- If the image is blurry, cropped, the wrong body part, or not a medical image, say so and set image_quality.
- You are not the patient's doctor and this is not a diagnosis. A radiologist's official report always comes first.
- Reply with JSON only, following the schema exactly."""

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "is_medical_image": {"type": "boolean"},
        "detected_body_part": {"type": "string"},
        "image_quality": {"type": "string", "enum": ["good", "limited", "unreadable"]},
        "primary_condition": {"type": "string"},
        "condition_in_reference_data": {"type": "boolean"},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding": {"type": "string"},
                    "location": {"type": "string"},
                    "explanation": {"type": "string"},
                    "supported_by_reference_data": {"type": "boolean"},
                    "confidence": {"type": "string", "enum": ["high", "moderate", "low"]},
                },
                "required": ["finding", "location", "explanation", "supported_by_reference_data", "confidence"],
            },
        },
        "overall_confidence": {"type": "string", "enum": ["high", "moderate", "low"]},
        "confidence_reason": {"type": "string"},
        "urgency": {"type": "string", "enum": ["routine", "soon", "urgent", "emergency"]},
        "referral": {
            "type": "object",
            "properties": {
                "specialist": {"type": "string", "enum": SPECIALISTS},
                "reason": {"type": "string"},
                "timeframe": {"type": "string"},
            },
            "required": ["specialist", "reason", "timeframe"],
        },
        "reference_comparison": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "questions_for_doctor": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "is_medical_image",
        "detected_body_part",
        "image_quality",
        "primary_condition",
        "condition_in_reference_data",
        "summary",
        "findings",
        "overall_confidence",
        "confidence_reason",
        "urgency",
        "referral",
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
    covered_conditions: list[tuple[str, int]] | None = None,
    sources: list[str] | None = None,
) -> str:
    bp, mod = body_part_label(body_part), modality_label(modality)
    q = _clip(question, 500) or "What does my image show?"
    lines = [f"The patient says this is a {mod} of the {bp}.", f'Patient\'s question: "{q}"', ""]

    if covered_conditions:
        total = sum(n for _, n in covered_conditions)
        listed = ", ".join(f"{name} ({n})" for name, n in covered_conditions[:12])
        lines.append(f"Conditions covered by our verified data for this body part ({total} cases): {listed}.")
        if sources:
            lines.append(f"Data sources: {', '.join(sources[:6])}.")
    else:
        lines.append("We have no verified data for this body part yet, so condition_in_reference_data must be false.")
    lines.append("")

    if references:
        n = len(references)
        if reference_images_attached:
            lines.append(
                f"You are given {n + 1} images. Images 1 to {n} are VERIFIED REFERENCE CASES that look most similar to "
                f"the patient's image, for comparison only. Image {n + 1} (the last one) is the PATIENT'S image."
            )
        else:
            lines.append("You are given 1 image: the PATIENT'S image. Most similar verified reference cases:")
        for i, case in enumerate(references, start=1):
            tag = case.label
            if case.diagnosis and case.diagnosis.lower() != case.label:
                tag += f", {case.diagnosis}"
            text = f"- Reference case {i} ({tag}; source: {case.source or 'verified dataset'})."
            if case.report:
                text += f' Verified report/label: "{_clip(case.report, 700)}"'
            lines.append(text)
    else:
        lines.append("You are given 1 image: the PATIENT'S image. No reference cases are available.")

    lines += [
        "",
        "Examine the PATIENT'S image and answer the patient's question. Return JSON that follows the schema: "
        "a strong 3-5 sentence summary written for the patient, up to 5 findings, urgency, a referral to the right "
        "specialist with the reason and timeframe, up to 4 next steps and up to 4 questions for their doctor. "
        "In reference_comparison, say in one or two sentences how the patient's image compares with the reference cases.",
    ]
    return "\n".join(lines)
