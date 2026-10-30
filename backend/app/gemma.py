"""Talks to Gemma 4.

Two engines, same prompt and same JSON answer:
- "google": Gemma 4 hosted by Google AI Studio (Gemini API). Fast (seconds). Used when an API key is set.
- "ollama": the local Gemma 4 copy in this Codespace. Slow on CPU (minutes), but needs no key.
  It is also the automatic backup if the hosted call fails.
"""

from __future__ import annotations

import json
import logging
import re
import time

import httpx

from .config import settings
from .prompts import OUTPUT_SCHEMA, SYSTEM_PROMPT

log = logging.getLogger("medpeek.gemma")
GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GemmaUnavailable(RuntimeError):
    """The model couldn't be reached (no server, no key, timeout, quota)."""


class GemmaBadOutput(RuntimeError):
    """Gemma answered, but not with valid JSON."""


def engine() -> str:
    """Which engine the next request uses: mock, google or ollama."""
    if settings.mock_gemma:
        return "mock"
    if settings.gemma_provider == "google" or (settings.gemma_provider == "auto" and settings.google_api_key):
        return "google"
    return "ollama"


def _ollama_status() -> dict:
    try:
        r = httpx.get(f"{settings.ollama_url}/api/tags", timeout=3.0)
        r.raise_for_status()
        names = {m.get("name") for m in r.json().get("models", [])}
        return {"ollama": "up", "local_model": settings.gemma_model, "local_model_ready": settings.gemma_model in names}
    except Exception:
        return {"ollama": "down", "local_model": settings.gemma_model, "local_model_ready": False}


def status() -> dict:
    """Engine, model and readiness, for /api/health."""
    current = engine()
    if current == "mock":
        return {"engine": "mock", "model": settings.gemma_model, "model_ready": True}
    local = _ollama_status()
    if current == "google":
        return {"engine": "google", "model": settings.google_model, "model_ready": bool(settings.google_api_key), **local}
    return {"engine": "ollama", "model": settings.gemma_model, "model_ready": local["local_model_ready"], **local}


def analyze(user_prompt: str, images_b64: list[str]) -> tuple[dict, dict]:
    """Send the prompt + images to Gemma 4. Returns (parsed_json, info about the run)."""
    if engine() == "google":
        models = [m for m in dict.fromkeys([settings.google_model, settings.google_backup_model]) if m]
        last_error: Exception | None = None
        for model in models:
            try:
                return _google(user_prompt, images_b64, model)
            except (GemmaUnavailable, GemmaBadOutput) as exc:
                last_error = exc
                log.warning("Hosted Gemma 4 (%s) failed: %s", model, exc)
        if not settings.fallback_to_local and last_error:
            raise last_error
        log.warning("All hosted Gemma 4 models failed. Falling back to the local copy.")
    return _ollama(user_prompt, images_b64)


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise GemmaBadOutput("Gemma 4 returned an answer without JSON.")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise GemmaBadOutput("Gemma 4 returned an answer that wasn't valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise GemmaBadOutput("Gemma 4 returned JSON in an unexpected shape.")
    return parsed


def _google(user_prompt: str, images_b64: list[str], model: str) -> tuple[dict, dict]:
    if not settings.google_api_key:
        raise GemmaUnavailable("No Google AI Studio API key is set (GEMINI_API_KEY).")
    schema = json.dumps(OUTPUT_SCHEMA, separators=(",", ":"))
    parts: list[dict] = [{"inline_data": {"mime_type": "image/png", "data": b64}} for b64 in images_b64]
    parts.append(
        {
            "text": f"{user_prompt}\n\nReply with ONLY one JSON object, no markdown, that matches this JSON schema:\n{schema}"
        }
    )
    body: dict = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048, "thinkingConfig": {"thinkingLevel": "minimal"}},
    }
    url = GOOGLE_URL.format(model=model)
    headers = {"x-goog-api-key": settings.google_api_key}

    started = time.monotonic()
    r = _post(url, body, headers)
    if r.status_code == 400:
        log.warning("Google AI Studio said 400 (%s); retrying without the thinking setting.", r.text[:200])
        body["generationConfig"].pop("thinkingConfig", None)  # some model versions don't accept this field
        r = _post(url, body, headers)
    if r.status_code >= 400:
        raise GemmaUnavailable(f"Google AI Studio error {r.status_code}: {r.text[:300]}")

    data = r.json()
    candidates = data.get("candidates") or []
    text = "".join(
        p.get("text", "")
        for c in candidates[:1]
        for p in (c.get("content") or {}).get("parts", [])
        if not p.get("thought")
    )
    parsed = _parse_json(text)
    usage = data.get("usageMetadata") or {}
    return parsed, {
        "engine": "google",
        "model": model,
        "seconds": round(time.monotonic() - started, 1),
        "prompt_tokens": usage.get("promptTokenCount"),
        "output_tokens": usage.get("candidatesTokenCount"),
    }


def _post(url: str, body: dict, headers: dict) -> httpx.Response:
    try:
        return httpx.post(url, json=body, headers=headers, timeout=httpx.Timeout(settings.google_timeout, connect=10.0))
    except httpx.TimeoutException as exc:
        raise GemmaUnavailable("Google AI Studio took too long to answer.") from exc
    except httpx.HTTPError as exc:
        raise GemmaUnavailable(f"Couldn't reach Google AI Studio: {exc}") from exc


def _ollama(user_prompt: str, images_b64: list[str]) -> tuple[dict, dict]:
    payload = {
        "model": settings.gemma_model,
        "stream": False,
        "think": settings.think,
        "format": OUTPUT_SCHEMA,
        "keep_alive": -1,
        "options": {
            "temperature": 0.2,
            "num_ctx": settings.num_ctx,
            **({"num_thread": settings.num_thread} if settings.num_thread > 0 else {}),
        },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt, "images": images_b64},
        ],
    }
    started = time.monotonic()
    try:
        r = httpx.post(
            f"{settings.ollama_url}/api/chat",
            json=payload,
            timeout=httpx.Timeout(settings.ollama_timeout, connect=10.0),
        )
    except httpx.ConnectError as exc:
        raise GemmaUnavailable("Gemma 4 isn't running (Ollama not reachable). Run ~/start-gemma.sh in the Codespace.") from exc
    except httpx.TimeoutException as exc:
        raise GemmaUnavailable("Gemma 4 took too long to answer. The server may be busy; try again.") from exc

    if r.status_code == 404:
        raise GemmaUnavailable(f"Model {settings.gemma_model} isn't pulled. Run: ollama pull {settings.gemma_model}")
    if r.status_code >= 400:
        raise GemmaUnavailable(f"Ollama error {r.status_code}: {r.text[:300]}")

    data = r.json()
    parsed = _parse_json((data.get("message") or {}).get("content", ""))
    return parsed, {
        "engine": "ollama",
        "model": settings.gemma_model,
        "seconds": round(time.monotonic() - started, 1),
        "prompt_tokens": data.get("prompt_eval_count"),
        "output_tokens": data.get("eval_count"),
    }


def mock_answer(body_part: str, modality: str) -> tuple[dict, dict]:
    """A realistic fake answer so the frontend team can build without Gemma running."""
    time.sleep(settings.mock_delay_seconds)
    answer = {
        "is_medical_image": True,
        "detected_body_part": body_part.replace("_", " "),
        "image_quality": "good",
        "primary_condition": "fracture",
        "condition_in_reference_data": True,
        "summary": (
            "This is a mock answer for frontend testing. There appears to be a thin break across the end of the "
            "radius, the larger forearm bone, just above the wrist. The bone pieces still line up well, which "
            "usually means it can heal in a cast. An orthopedic doctor should confirm this and decide on treatment."
        ),
        "findings": [
            {
                "finding": "Possible break (fracture) of the distal radius",
                "location": "Lower end of the forearm, near the wrist",
                "explanation": "A thin dark line runs across the bone where it should look solid.",
                "supported_by_reference_data": True,
            },
            {
                "finding": "Soft-tissue swelling",
                "location": "Around the wrist",
                "explanation": "The shadow of the skin and muscle looks thicker than usual, which often happens after an injury.",
                "supported_by_reference_data": False,
            },
        ],
        "urgency": "urgent",
        "referral": {
            "specialist": "Orthopedic surgeon (orthopedics)",
            "reason": "Orthopedic surgeons treat broken bones and decide whether a cast or surgery is needed.",
            "timeframe": "Within 1-2 days",
        },
        "reference_comparison": "The image looks closest to the verified fracture case and differs from the normal case.",
        "next_steps": ["Keep the wrist still and raised until your appointment.", "Use ice for 15 minutes at a time."],
        "questions_for_doctor": ["Do I need a cast or a splint?", "Will I need another X-ray to check healing?"],
    }
    return answer, {"engine": "mock", "model": settings.gemma_model, "seconds": settings.mock_delay_seconds}
