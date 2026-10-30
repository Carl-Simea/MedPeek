"""Supported body parts and imaging types, with the synonyms patients actually type."""

from __future__ import annotations

BODY_PARTS: dict[str, dict] = {
    "chest": {"label": "Chest / lungs", "synonyms": ["lung", "lungs", "thorax", "ribs", "heart"]},
    "abdomen": {"label": "Abdomen / belly", "synonyms": ["belly", "stomach", "abdominal", "kidney", "kidneys"]},
    "head": {"label": "Head / brain", "synonyms": ["brain", "skull", "face", "sinus", "sinuses"]},
    "cervical_spine": {"label": "Neck (cervical spine)", "synonyms": ["neck", "c-spine", "cervical"]},
    "thoracic_spine": {"label": "Upper/mid back (thoracic spine)", "synonyms": ["t-spine", "thoracic", "upper back", "mid back"]},
    "lumbar_spine": {"label": "Lower back (lumbar spine)", "synonyms": ["l-spine", "lumbar", "lower back", "back"]},
    "shoulder": {"label": "Shoulder", "synonyms": ["clavicle", "collarbone", "humerus"]},
    "elbow": {"label": "Elbow", "synonyms": ["forearm"]},
    "wrist": {"label": "Wrist", "synonyms": []},
    "hand": {"label": "Hand / fingers", "synonyms": ["finger", "fingers", "thumb"]},
    "hip": {"label": "Hip", "synonyms": ["femur", "thigh"]},
    "pelvis": {"label": "Pelvis", "synonyms": ["pelvic"]},
    "knee": {"label": "Knee", "synonyms": ["patella", "kneecap", "tibia", "leg"]},
    "ankle": {"label": "Ankle", "synonyms": ["fibula"]},
    "foot": {"label": "Foot / toes", "synonyms": ["toe", "toes", "heel"]},
    "other": {"label": "Other", "synonyms": []},
}

MODALITIES: dict[str, dict] = {
    "xray": {"label": "X-ray", "synonyms": ["x-ray", "x ray", "radiograph", "xr", "cr", "dx"]},
    "mri": {"label": "MRI", "synonyms": ["mr", "magnetic resonance"]},
    "ct": {"label": "CT scan", "synonyms": ["cat scan", "ct scan", "computed tomography"]},
}


def _normalize(value: str, table: dict[str, dict]) -> str | None:
    v = (value or "").strip().lower().replace("_", " ")
    if not v:
        return None
    for key, info in table.items():
        if v == key.replace("_", " ") or v == info["label"].lower() or v in info["synonyms"]:
            return key
    return None


def normalize_body_part(value: str) -> str | None:
    return _normalize(value, BODY_PARTS)


def normalize_modality(value: str) -> str | None:
    return _normalize(value, MODALITIES)


def body_part_label(key: str) -> str:
    return BODY_PARTS.get(key, {}).get("label", key)


def modality_label(key: str) -> str:
    return MODALITIES.get(key, {}).get("label", key)
