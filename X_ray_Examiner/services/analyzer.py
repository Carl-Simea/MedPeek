"""Reads an uploaded X-ray with Gemma (through Ollama) and compares it with a normal one from images.db.

Step 1 checks the upload: is it an X-ray, which body part, which view, child or adult.
Step 2 looks for breaks. Adults are compared side by side with the closest normal X-ray
from the database. Children are checked on their own, because the normals are adult
bones and a child's open growth plates would look like breaks next to them.
The reply is built here in Python, so the safety rules can't be skipped by the model:
no cause is ever given for a child.
"""
import base64
import json
import math
import os
import urllib.error
import urllib.request

from services.db import reference_images, supported_parts

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/chat"
MODEL = os.environ.get("XRAY_MODEL", "gemma4")
TIMEOUT_SECONDS = 600

BODY_PARTS = ["hand", "chest", "arm", "shoulder", "hip", "leg", "foot", "spine", "skull", "other"]
SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_xray": {"type": "boolean"},
        "body_part": {"type": "string", "enum": BODY_PARTS},
        "view": {"type": "string", "enum": ["frontal", "lateral", "oblique", "unsure"]},
        "age_group": {"type": "string", "enum": ["adult", "child", "unsure"]},
        "growth_plates_visible": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["is_xray", "body_part", "view", "age_group", "growth_plates_visible", "reason"],
}


def break_schema(with_cause):
    item = {
        "bone": {"type": "string"},
        "location": {"type": "string"},
        "type": {"type": "string"},
        "displaced": {"type": "string", "enum": ["yes", "no", "unsure"]},
    }
    if with_cause:
        item["typical_cause"] = {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "breaks": {
                "type": "array",
                "items": {"type": "object", "properties": item, "required": list(item)},
            },
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "notes": {"type": "string"},
        },
        "required": ["breaks", "confidence", "notes"],
    }


ADULT_SCHEMA = break_schema(with_cause=True)
CHILD_SCHEMA = break_schema(with_cause=False)

TRIAGE_SYSTEM = """You check uploads for an educational X-ray examiner app. Answer only with the JSON you are asked for.
- is_xray: false if the image is not a medical X-ray (a photo, a drawing, a document).
- body_part: the main body part shown. Use "hand" for fingers, hand and wrist.
- view: frontal (front or back view), lateral (side view), oblique (angled view), or unsure.
- age_group: decide from the growth plates. In a child, the end of each long bone is separated from the rest of the bone by a thin dark line, an open growth plate. In the hand you see them near the ends of the finger and hand bones and at the wrist end of the radius and ulna. Young children also have small, widely spaced wrist bones, and some wrist bones are missing. Open growth plates mean "child". Bone ends fully joined with no dark lines mean "adult". If you cannot tell, answer "unsure".
- growth_plates_visible: true if you see open growth plates.
- reason: one short sentence saying what you based the age group on."""

ADULT_SYSTEM = """You compare X-rays for an educational X-ray examiner app. Answer only with the JSON you are asked for.
Image 1 is a NORMAL {part} X-ray from the app's reference database. Image 2 is the patient's {part} X-ray.
Go through image 2 bone by bone and compare each bone with the same bone in image 1. Signs of a break: a dark line or gap through a bone, a step or kink in the bone's outline, pieces of bone out of line, a bone that looks squashed or shorter, or a small piece of bone pulled off near a joint.
Differences in size, position, rotation or brightness between the two images are not breaks.
For each break give:
- bone: the exact bone, for example "fifth metacarpal", "proximal phalanx of the index finger", "scaphoid", "distal radius".
- location: where on that bone (base, shaft, neck, head, or next to which joint).
- type: transverse, oblique, spiral, comminuted, avulsion, impacted, intra-articular, or hairline.
- displaced: yes if the pieces have moved out of line, no if they are still in line, unsure if you cannot tell.
- typical_cause: the kind of injury that usually causes this pattern, for example "punching a hard object", "a fall on an outstretched hand", "a twisting force", "a crush injury". Describe what usually causes it, not what happened to this person.
If you find no break, return an empty breaks list. Set confidence honestly. notes: anything else worth mentioning, or an empty string."""

CHILD_SYSTEM = """You examine X-rays for an educational X-ray examiner app. Answer only with the JSON you are asked for.
The image is a child's {part} X-ray. Children have open growth plates: thin dark lines near the ends of the bones. They are normal and are not breaks, unless one looks wider than the others, shifted, or has a break line running into it.
Look for breaks, including the kinds common in children: buckle (a small bulge in the bone's outline), greenstick (a crack through one side of a bent bone), and breaks through a growth plate.
For each break give:
- bone: the exact bone, for example "fifth metacarpal", "proximal phalanx of the index finger", "distal radius".
- location: where on that bone (base, shaft, neck, head, growth plate, or next to which joint).
- type: buckle, greenstick, growth plate, transverse, oblique, spiral, comminuted, avulsion, or hairline.
- displaced: yes if the pieces have moved out of line, no if they are still in line, unsure if you cannot tell.
Do not say what caused the injury. If you find no break, return an empty breaks list. Set confidence honestly. notes: anything else worth mentioning, or an empty string."""


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
    """Pick the normal X-ray whose shape (width/height) is closest to the upload's."""
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


def ask_model(system, prompt, images, schema):
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(image).decode("ascii") for image in images],
            },
        ],
        "format": schema,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": 8192},
    }
    request = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        content = json.load(response)["message"]["content"]
    start, end = content.find("{"), content.rfind("}")
    return json.loads(content[start:end + 1])


def tidy(text):
    return str(text).strip().rstrip(".")


def format_reply(name, triage, result, child_path, reference_title):
    part = triage["body_part"]
    age = triage.get("age_group")
    who = {"adult": "an adult's", "child": "a child's"}.get(age, "a")
    view = triage.get("view")
    view_text = f" ({view} view)" if view in ("frontal", "lateral", "oblique") else ""
    lines = [f"{name}, this is {who} {part} X-ray{view_text}."]

    if age == "child" and triage.get("growth_plates_visible"):
        lines.append("The thin dark lines near the ends of the bones are growth plates. "
                     "They're normal in children, not breaks.")
    if age != "adult" and age != "child":
        lines.append("I can't tell whether this is a child or an adult, "
                     "so I'm treating it as a child's X-ray to be safe.")

    breaks = result.get("breaks") or []
    if not breaks:
        lines.append("I don't see a break.")
    moved_text = {
        "yes": "The pieces have moved out of line.",
        "no": "The pieces are still in line.",
        "unsure": "I can't tell if the pieces have moved.",
    }
    for number, item in enumerate(breaks, start=1):
        label = f"Break {number}" if len(breaks) > 1 else "Break"
        lines.append(f"{label}: {tidy(item['bone'])}, {tidy(item['location'])}. "
                     f"Type: {tidy(item['type'])}. {moved_text.get(item.get('displaced'), '')}".rstrip())
        cause = tidy(item.get("typical_cause", ""))
        if cause and not child_path:
            lines.append(f"This kind of break usually comes from {cause[0].lower() + cause[1:]}.")

    if child_path:
        lines.append("I don't guess how a child got hurt, and a doctor should check any injury in a child.")
    notes = tidy(result.get("notes", ""))
    if notes:
        lines.append(f"Also: {notes}.")
    lines.append(f"How sure I am: {result.get('confidence', 'low')}.")
    if reference_title:
        lines.append(f"Compared with a normal {part} X-ray from the database ({reference_title}).")
    lines.append("This is an educational tool, not a diagnosis.")
    return "\n".join(lines)


def analyze_xray(file, user_name="there"):
    data = file.read()
    if not image_size(data):
        return f"{user_name}, I can only read X-rays saved as JPEG or PNG. Please upload it in one of those formats."

    try:
        triage = ask_model(TRIAGE_SYSTEM, "Check this image.", [data], TRIAGE_SCHEMA)
        if not triage.get("is_xray"):
            return f"{user_name}, this doesn't look like an X-ray to me. Please upload an X-ray image."

        part = str(triage.get("body_part", "other")).lower()
        triage["body_part"] = part
        supported = supported_parts()
        if not supported:
            return f"{user_name}, I don't have any normal X-rays to compare with yet."
        if part not in supported:
            options = " or ".join(supported)
            seen = "I couldn't tell which body part this is" if part == "other" else f"this looks like a {part} X-ray"
            return f"{user_name}, {seen}. Right now I can only examine {options} X-rays."

        child_path = triage.get("age_group") != "adult"
        if child_path:
            reference_title = None
            result = ask_model(CHILD_SYSTEM.format(part=part), "Find any breaks in this X-ray.", [data], CHILD_SCHEMA)
        else:
            reference_title, reference = closest_reference(data, reference_images(part))
            result = ask_model(
                ADULT_SYSTEM.format(part=part),
                "Image 1 is the normal reference. Image 2 is the patient's X-ray. Find any breaks in image 2.",
                [reference, data],
                ADULT_SCHEMA,
            )
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return (f"{user_name}, I couldn't reach the AI model. Make sure Ollama is running "
                f"(ollama serve) and the {MODEL} model is pulled.")
    except (KeyError, TypeError, ValueError, IndexError):
        return f"{user_name}, I couldn't read the model's answer for this image. Please try uploading it again."

    return format_reply(user_name, triage, result, child_path, reference_title)
