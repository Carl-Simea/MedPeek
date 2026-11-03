"""What we tell Gemma 4, and the JSON shape it must answer in."""

from __future__ import annotations

from .body_parts import SPECIALISTS, body_part_label, modality_label
from .references import ReferenceCase

SYSTEM_PROMPT = """You are MedPeek, the image-reading assistant inside a patient app. You run on Gemma 4.
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
                },
                "required": ["finding", "location", "explanation", "supported_by_reference_data"],
            },
        },
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
        listed = ", ".join(f"{name} ({n})" for name, n in covered_conditions[:25])
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
                f"You are given {n + 1} images. Images 1 to {n} are VERIFIED REFERENCE CASES from other people that look "
                f"most similar to the patient's image, for comparison only. Image {n + 1} (the last one) is the PATIENT'S "
                "image. Talk to the patient only about their own image; never describe a reference image as theirs."
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
        "a strong 3-5 sentence summary written for the patient, up to 3 findings, urgency, a referral to the right "
        "specialist with the reason and timeframe, up to 3 next steps and up to 3 questions for their doctor. "
        "In reference_comparison, say in one or two sentences how the patient's image compares with the reference cases.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- text chat (no image)
CHAT_SYSTEM_PROMPT = """You are MedPeek, a calm, clear health assistant in a patient app. You run on Gemma 4.
You talk with patients about symptoms, injuries, X-rays, MRIs and CT scans, what their report means, which kind of
doctor treats what, and what to do next. You do not diagnose.

How to answer:
- Plain words, 8th-grade reading level, short paragraphs. Be warm and direct. Answer the question first.
- If a scan would help (they describe an injury, a lump, a cough that won't go away, or ask what an image shows), say
  so and set needs_image to true: they can attach the X-ray, MRI or CT in this chat and you will read it.
- Set body_part to the body area the conversation is about, when clear.
- Always set specialist to the kind of doctor who usually treats what they describe (a broken bone -> orthopedic
  surgeon, a tooth -> dentist, chest infection -> pulmonologist, a child -> pediatrician, nothing specific -> primary
  care doctor). Set urgency honestly: emergency only for signs that can't wait (chest pain, trouble breathing, a bone
  out of place, heavy bleeding, weakness or numbness, a head injury with confusion); then tell them to call 911 or go
  to the nearest emergency room, and set emergency to true.
- Nearby care: if they ask where to go, for a hospital, urgent care, ER or a doctor near them, set wants_nearby_care to
  true. If their message contains a US ZIP code, copy it into zip_code. NEVER invent clinic names, addresses or phone
  numbers: the app looks up real places from government data and shows them under your reply. If they want nearby
  care and you don't know their ZIP code, ask for it.
- If a MedPeek report is provided in the conversation, use it to explain and answer follow-ups about that report.
- Stay on health. For unrelated requests (code, homework, politics, jokes, other products), set topic_ok to false and
  reply with one friendly sentence that you can only help with health questions and medical images.
- Never claim certainty. Remind them, briefly, when something needs a doctor's eyes.
- Reply with JSON only, following the schema exactly."""

CHAT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "topic_ok": {"type": "boolean"},
        "needs_image": {"type": "boolean"},
        "body_part": {"type": "string"},
        "specialist": {"type": "string", "enum": SPECIALISTS + [""]},
        "urgency": {"type": "string", "enum": ["routine", "soon", "urgent", "emergency"]},
        "emergency": {"type": "boolean"},
        "wants_nearby_care": {"type": "boolean"},
        "zip_code": {"type": "string"},
    },
    "required": ["reply", "topic_ok", "needs_image", "body_part", "specialist", "urgency", "emergency",
                 "wants_nearby_care", "zip_code"],
}


def report_context(report: dict | None) -> str:
    """A compact text version of a MedPeek report, so the chat can answer follow-up questions about it."""
    if not isinstance(report, dict) or not report.get("summary"):
        return ""
    ref = report.get("referral") or {}
    lines = [
        "MedPeek report the patient is asking about:",
        f"- Image: {report.get('modality_label', '')} of the {str(report.get('body_part_label', '')).lower()}",
        f"- Main finding: {report.get('primary_condition') or 'none stated'}",
        f"- Summary: {_clip(str(report.get('summary', '')), 700)}",
    ]
    findings = [f.get("finding", "") for f in (report.get("findings") or []) if isinstance(f, dict)]
    if findings:
        lines.append("- Findings: " + "; ".join(findings[:4]))
    if ref.get("specialist"):
        lines.append(f"- Referral: {ref['specialist']} ({ref.get('timeframe', '')}); urgency {report.get('urgency', '')}")
    if report.get("coverage_note"):
        lines.append(f"- Data note: {report['coverage_note']}")
    return "\n".join(lines)
