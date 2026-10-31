"""Drop-in analyzer for the X_ray_Examiner app: sends uploads to the MedPeek backend.

How to use it (3 small edits in X_ray_Examiner):

1. Replace X_ray_Examiner/services/analyzer.py with this file.
2. In app.py, pass the chat message along so MedPeek knows the body part:
       gemma_response = analyze_xray(file, question=user_text)
3. In templates/results.html, right under the bot message text
       <div class="message-text" ...>{{ message.text }}</div>
   add the highlighted notice:
       <div class="medpeek-notice">MedPeek is AI and can make mistakes.</div>
   and in static/style.css:
       .medpeek-notice { margin-top: 8px; padding: 8px 12px; border-radius: 8px; font-weight: 600;
                         background: #fff4d6; color: #7a5200; border: 1px solid #e3b341; }

Set MEDPEEK_API to the backend URL if it changes (default: the team's Codespace URL).
Needs nothing beyond the Python standard library.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

MEDPEEK_API = os.environ.get(
    "MEDPEEK_API", "https://filthy-spooky-mummy-5v4wgppj65j427rpv-8000.app.github.dev"
).rstrip("/")
MAX_WAIT_SECONDS = 360
AI_NOTICE = "MedPeek is AI and can make mistakes."


def _multipart(fields: dict[str, str], filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _call(url: str, body: bytes | None = None, content_type: str | None = None) -> dict:
    request = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    if content_type:
        request.add_header("Content-Type", content_type)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def format_report(r: dict, user_name: str = "there") -> str:
    """Turn the MedPeek JSON into the chat reply (plain text, one idea per line)."""
    lines = []
    if r.get("urgency_notice"):
        lines.append(f"⚠ {r['urgency_notice']}")
    lines.append(r.get("summary", ""))
    for f in r.get("findings", []):
        where = f" ({f['location']})" if f.get("location") else ""
        lines.append(f"• {f['finding']}{where}: {f.get('explanation', '')}")
    ref = r.get("referral") or {}
    if ref.get("specialist"):
        lines.append(f"Who to see: {ref['specialist']} ({ref.get('timeframe', '')}). {ref.get('reason', '')}")
    if r.get("coverage_note"):
        lines.append(r["coverage_note"])
    if r.get("next_steps"):
        lines.append("Next steps:")
        lines += [f"• {step}" for step in r["next_steps"]]
    lines.append(r.get("disclaimer", ""))
    return "\n".join(line for line in lines if line)


def analyze_xray(file, user_name: str = "there", question: str = "") -> str:
    """Same signature the app already uses. Returns the reply text for the chat."""
    data = file.read()
    body, content_type = _multipart(
        {"body_part": "auto", "modality": "xray", "question": question or ""}, file.filename or "xray.png", data
    )
    try:
        job = _call(f"{MEDPEEK_API}/api/analyze", body, content_type)
        started = time.monotonic()
        while time.monotonic() - started < MAX_WAIT_SECONDS:
            time.sleep(3)
            status = _call(f"{MEDPEEK_API}/api/jobs/{job['job_id']}")
            if status["status"] == "done":
                return format_report(status["result"], user_name)
            if status["status"] == "error":
                return f"Sorry {user_name}, MedPeek couldn't read this image: {status.get('error')}"
        return f"Sorry {user_name}, MedPeek is taking too long. Please try again in a minute."
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("detail")
        except Exception:
            detail = None
        return f"Sorry {user_name}, {detail or 'MedPeek could not process this image.'}"
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return f"Sorry {user_name}, I couldn't reach MedPeek. Is the backend running?"
