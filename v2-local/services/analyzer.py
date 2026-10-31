"""Reads an uploaded X-ray with Gemma and compares it with a normal one from images.db.

One model call does everything, so answers come back faster:
  - is it an X-ray, which body part and view, child or adult (from the growth plates)
  - any breaks: bone, location, type, displaced, and the injury that usually causes that pattern
The normal reference is sent first and the upload second. Safety rules are applied here in
Python, not left to the model: a child (or an unsure age) never gets a cause.
"""
import base64
import json
import math

from services import db
from services.ollama_client import MODEL, OllamaError, chat

BODY_PARTS = ["hand", "chest", "arm", "shoulder", "hip", "leg", "foot", "spine", "skull", "other"]
SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}

SCHEMA = {
    "type": "object",
    "properties": {
        "is_xray": {"type": "boolean"},
        "body_part": {"type": "string", "enum": BODY_PARTS},
        "view": {"type": "string", "enum": ["frontal", "lateral", "oblique", "unsure"]},
        "age_group": {"type": "string", "enum": ["adult", "child", "unsure"]},
        "growth_plates_visible": {"type": "boolean"},
        "breaks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "bone": {"type": "string"},
                    "location": {"type": "string"},
                    "type": {"type": "string"},
                    "displaced": {"type": "string", "enum": ["yes", "no", "unsure"]},
                    "typical_cause": {"type": "string"},
                },
                "required": ["bone", "location", "type", "displaced", "typical_cause"],
            },
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "notes": {"type": "string"},
    },
    "required": ["is_xray", "body_part", "view", "age_group", "growth_plates_visible", "breaks", "confidence", "notes"],
}

SYSTEM = """You are the image reader for MedPeek, an educational X-ray app. Answer only with the JSON you are asked for.
You get two images. Image 1 is a NORMAL {part} X-ray from MedPeek's reference database. Image 2 is the user's upload.
Describe image 2:
- is_xray: false if image 2 is not a medical X-ray (a photo, a drawing, a document).
- body_part: the main body part in image 2. Use "hand" for fingers, hand and wrist.
- view: frontal, lateral, oblique, or unsure.
- age_group: decide from the growth plates. In a child, the end of each long bone is separated from the rest of the bone by a thin dark line, an open growth plate: near the ends of the finger and hand bones, and at the wrist end of the radius and ulna. Young children also have small, widely spaced wrist bones. Open growth plates mean "child", fully joined bone ends mean "adult", otherwise "unsure".
- growth_plates_visible: true if you see open growth plates.
- breaks: go through image 2 bone by bone and compare each bone with the same bone in image 1. Signs of a break: a dark line or gap through a bone, a step or kink in the outline, pieces out of line, a squashed or shortened bone, a small piece pulled off near a joint. Differences in size, position, rotation or brightness are not breaks. Open growth plates in a child are not breaks unless one looks widened, shifted or has a break line running into it. If image 2 is a child's X-ray, do not compare its growth plates with image 1.
  For each break: bone (exact, e.g. "fifth metacarpal", "proximal phalanx of the index finger", "scaphoid", "distal radius"); location (base, shaft, neck, head, growth plate, or next to which joint); type (transverse, oblique, spiral, comminuted, avulsion, impacted, intra-articular, hairline, buckle, greenstick, or growth plate); displaced (yes, no, unsure); typical_cause (the kind of injury that usually causes this pattern, e.g. "punching a hard object", "a fall on an outstretched hand", "a twisting force"; describe what usually causes it, not what happened to this person).
- If there is no break, return an empty breaks list.
- confidence: low, medium or high, honestly.
- notes: anything else worth mentioning in one short sentence, or an empty string."""

CHAT_SYSTEM = """You are MedPeek, a friendly educational assistant inside an X-ray app. The user's first name is {name}; use it naturally.
MedPeek can examine these X-rays right now: {parts}. To get one examined, the user attaches it with the paperclip or drags it into the chat.
Answer questions about bones, fractures, X-rays and the results shown earlier in this chat in plain, simple language, in 2 to 5 sentences.
You are not a doctor and this is not a diagnosis; if someone describes a real injury, tell them to see a doctor.
Never guess how a child was injured."""


def image_size(data):
    """(width, height) of a PNG or JPEG, or None if it isn't one."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i += 2
            continue
        if marker in SOF_MARKERS:
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def closest_reference(upload, references):
    """The normal X-ray whose shape (width/height) is closest to the upload's."""
    size = image_size(upload)
    if not size or not all(size):
        return references[0]
    target = size[0] / size[1]

    def distance(reference):
        ref_size = image_size(reference[1])
        if not ref_size or not all(ref_size):
            return float("inf")
        return abs(math.log(ref_size[0] / ref_size[1] / target))

    return min(references, key=distance)


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def _tidy(text):
    return str(text or "").strip().rstrip(".")


def _parse_json(content):
    start, end = content.find("{"), content.rfind("}")
    return json.loads(content[start:end + 1])


def text_reply(text):
    return {"kind": "text", "text": text, "data": None}


def analyze_xray(data, user_name, note=""):
    """Analyze one upload. Returns {'kind': 'analysis' or 'text', 'text': ..., 'data': ...}."""
    if not image_size(data):
        return text_reply(f"{user_name}, I can only read X-rays saved as JPEG or PNG. Please upload it in one of those formats.")

    parts = db.supported_parts()
    if not parts:
        return text_reply(f"{user_name}, my reference database is empty. Run create_db.py first so I have normal X-rays to compare with.")

    # Compare against the first supported part (hand) unless the user's note names another one.
    part = next((p for p in parts if p in note.lower()), "hand" if "hand" in parts else parts[0])
    reference_title, reference = closest_reference(data, db.reference_images(part))
    prompt = "Image 1 is the normal reference. Image 2 is the upload. Describe image 2."
    if note:
        prompt += f" The user wrote: {note[:300]}"
    messages = [
        {"role": "system", "content": SYSTEM.format(part=part)},
        {"role": "user", "content": prompt, "images": [_b64(reference), _b64(data)]},
    ]
    try:
        result = _parse_json(chat(messages, schema=SCHEMA))
        seen = str(result.get("body_part", "other")).lower()
        if not result.get("is_xray"):
            return text_reply(f"{user_name}, this doesn't look like an X-ray to me. Please upload an X-ray image.")
        if seen not in parts:
            what = "I couldn't tell which body part this is" if seen == "other" else f"this looks like a {seen} X-ray"
            return text_reply(f"{user_name}, {what}. Right now I can only examine {' or '.join(parts)} X-rays.")
        if seen != part:  # the note pointed us at the wrong reference; this only happens with several parts
            reference_title = None
    except OllamaError as error:
        return text_reply(f"{user_name}, I couldn't reach Gemma. {error} Check the status light at the top.")
    except (KeyError, TypeError, ValueError, IndexError):
        return text_reply(f"{user_name}, I couldn't read Gemma's answer for this image. Please try it again.")

    age = result.get("age_group")
    child_path = age != "adult"
    breaks = []
    for item in result.get("breaks") or []:
        cause = _tidy(item.get("typical_cause"))
        breaks.append({
            "bone": _tidy(item.get("bone")),
            "location": _tidy(item.get("location")),
            "type": _tidy(item.get("type")),
            "displaced": item.get("displaced", "unsure"),
            # Never a cause for a child or an unsure age, whatever the model said.
            "cause": "" if child_path or not cause else cause[0].lower() + cause[1:],
        })
    data_out = {
        "name": user_name,
        "body_part": seen,
        "view": result.get("view", "unsure"),
        "age_group": age if age in ("adult", "child") else "unsure",
        "growth_plates": bool(result.get("growth_plates_visible")) and age == "child",
        "child_path": child_path,
        "breaks": breaks,
        "confidence": result.get("confidence", "low"),
        "notes": _tidy(result.get("notes")),
        "reference": None if child_path else reference_title,  # children aren't compared with adult bones
        "model": MODEL,
    }
    return {"kind": "analysis", "text": summary(data_out), "data": data_out}


def summary(d):
    """Plain-text version of an analysis, used as chat context for follow-up questions."""
    who = {"adult": "an adult's", "child": "a child's"}.get(d["age_group"], "a")
    found = "; ".join(f"{b['type']} break of the {b['bone']} ({b['location']})" for b in d["breaks"]) or "no break found"
    return f"{d['name']}'s upload: {who} {d['body_part']} X-ray, {found}, confidence {d['confidence']}."


def chat_reply(user_name, text, history):
    """Answer a typed question, with the recent chat as context."""
    messages = [{"role": "system", "content": CHAT_SYSTEM.format(
        name=user_name, parts=", ".join(db.supported_parts()) or "none yet")}]
    for message in history:
        content = message["text"] or (f"[uploaded {message['filename']}]" if message.get("filename") else "")
        if content:
            messages.append({"role": "user" if message["sender"] == "user" else "assistant", "content": content})
    messages.append({"role": "user", "content": text[:2000]})
    try:
        return text_reply(chat(messages).strip())
    except OllamaError as error:
        return text_reply(f"{user_name}, I couldn't reach Gemma. {error} Check the status light at the top.")
    except (KeyError, TypeError, ValueError):
        return text_reply(f"{user_name}, something went wrong reading Gemma's answer. Please ask again.")