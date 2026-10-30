"""Body-part groups (the same 15 groups as the team's dataset research), with the
words patients actually type, and the specialist who usually handles each area."""

from __future__ import annotations

import re

BODY_PARTS: dict[str, dict] = {
    "skull_face_sinuses": {
        "label": "Skull, face and sinuses",
        "synonyms": ["skull", "head", "face", "facial", "sinus", "sinuses", "nose", "nasal", "orbit", "cephalogram"],
        "specialist": "ENT specialist (otolaryngologist)",
    },
    "teeth_jaw": {
        "label": "Teeth and jaw",
        "synonyms": ["teeth", "tooth", "dental", "jaw", "mandible", "panoramic", "opg", "orthopantomogram", "bitewing"],
        "specialist": "Dentist",
    },
    "neck_cervical_spine": {
        "label": "Neck and cervical spine",
        "synonyms": ["neck", "c-spine", "cervical", "cervical spine"],
        "specialist": "Orthopedic spine specialist",
    },
    "chest_adult": {
        "label": "Chest (adults)",
        "synonyms": ["chest", "lung", "lungs", "thorax", "ribs", "heart", "adult chest"],
        "specialist": "Pulmonologist (lung doctor)",
    },
    "chest_child": {
        "label": "Chest (children)",
        "synonyms": ["child chest", "children chest", "pediatric chest", "kid chest", "baby chest", "infant chest"],
        "specialist": "Pediatrician",
    },
    "breast": {
        "label": "Breast (mammogram)",
        "synonyms": ["breast", "mammogram", "mammography", "mammo"],
        "specialist": "Breast surgeon",
    },
    "back_spine": {
        "label": "Back (thoracic and lumbar spine)",
        "synonyms": ["back", "lower back", "upper back", "spine", "lumbar", "thoracic", "l-spine", "t-spine", "lumbar spine",
                     "thoracic spine"],
        "specialist": "Orthopedic spine specialist",
    },
    "abdomen_kidneys": {
        "label": "Abdomen and kidneys",
        "synonyms": ["abdomen", "abdominal", "belly", "stomach", "kidney", "kidneys", "bladder", "kub", "bowel"],
        "specialist": "Urologist",
    },
    "pelvis_hip": {
        "label": "Pelvis and hip",
        "synonyms": ["pelvis", "pelvic", "hip", "hips"],
        "specialist": "Orthopedic surgeon (orthopedics)",
    },
    "shoulder_upper_arm": {
        "label": "Shoulder, collarbone and upper arm",
        "synonyms": ["shoulder", "collarbone", "clavicle", "upper arm", "humerus", "scapula"],
        "specialist": "Orthopedic surgeon (orthopedics)",
    },
    "elbow_forearm": {
        "label": "Elbow and forearm",
        "synonyms": ["elbow", "forearm", "radius", "ulna"],
        "specialist": "Orthopedic surgeon (orthopedics)",
    },
    "wrist_hand": {
        "label": "Wrist, hand and fingers",
        "synonyms": ["wrist", "hand", "finger", "fingers", "thumb", "palm"],
        "specialist": "Hand surgeon",
    },
    "thigh_knee": {
        "label": "Thigh and knee",
        "synonyms": ["knee", "knees", "thigh", "femur", "kneecap", "patella"],
        "specialist": "Orthopedic surgeon (orthopedics)",
    },
    "lower_leg_foot": {
        "label": "Lower leg, ankle and foot",
        "synonyms": ["ankle", "foot", "feet", "toe", "toes", "heel", "shin", "tibia", "fibula", "lower leg", "leg"],
        "specialist": "Orthopedic surgeon (orthopedics)",
    },
    "whole_body": {
        "label": "Mixed / whole body",
        "synonyms": ["other", "whole body", "multiple", "mixed", "full body"],
        "specialist": "Primary care doctor",
    },
}

MODALITIES: dict[str, dict] = {
    "xray": {"label": "X-ray", "synonyms": ["x-ray", "x ray", "radiograph", "xr", "cr", "dx", "mammogram"]},
    "mri": {"label": "MRI", "synonyms": ["mr", "magnetic resonance"]},
    "ct": {"label": "CT scan", "synonyms": ["cat scan", "ct scan", "computed tomography"]},
}

# Doctors Gemma 4 may refer a patient to. Kept to a fixed list so the frontend gets clean values.
SPECIALISTS = [
    "Emergency department",
    "Primary care doctor",
    "Orthopedic surgeon (orthopedics)",
    "Orthopedic spine specialist",
    "Hand surgeon",
    "Pediatric orthopedic surgeon",
    "Sports medicine doctor",
    "Rheumatologist",
    "Orthopedic oncologist",
    "Oncologist (cancer specialist)",
    "Pulmonologist (lung doctor)",
    "Cardiologist (heart doctor)",
    "Pediatrician",
    "Pediatric pulmonologist",
    "Gastroenterologist",
    "Urologist",
    "Nephrologist (kidney doctor)",
    "General surgeon",
    "Breast surgeon",
    "Dentist",
    "Endodontist (root canal specialist)",
    "Oral and maxillofacial surgeon",
    "Orthodontist",
    "ENT specialist (otolaryngologist)",
    "Neurologist",
    "Neurosurgeon",
]


def _normalize(value: str, table: dict[str, dict]) -> str | None:
    v = " ".join((value or "").strip().lower().replace("_", " ").split())
    if not v:
        return None
    for key, info in table.items():
        if v == key.replace("_", " ") or v == info["label"].lower():
            return key
    for key, info in table.items():
        if v in info["synonyms"]:
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


def default_specialist(body_part: str) -> str:
    return BODY_PARTS.get(body_part, {}).get("specialist", "Primary care doctor")


_CHILD_WORDS = re.compile(r"\b(child|children|kid|kids|baby|babies|infant|toddler|son|daughter|boy|girl|newborn)\b")


def body_part_from_text(text: str) -> str | None:
    """Find a body part named in the patient's own words ('I fell on my wrist' -> wrist_hand)."""
    t = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
    best: tuple[str, int] | None = None
    for key, info in BODY_PARTS.items():
        if key == "whole_body":
            continue
        for word in (info["label"].lower(), *info["synonyms"]):
            w = re.sub(r"[^a-z0-9]+", " ", word.lower()).strip()
            if w and f" {w} " in t and (best is None or len(w) > best[1]):
                best = (key, len(w))
    if best is None:
        return None
    if best[0] == "chest_adult" and _CHILD_WORDS.search(t):
        return "chest_child"
    return best[0]
