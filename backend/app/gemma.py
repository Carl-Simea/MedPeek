"""Talks to Gemma 4 through the local Ollama server."""

from __future__ import annotations

import json
import time

import httpx

from .config import settings
from .prompts import OUTPUT_SCHEMA, SYSTEM_PROMPT


class GemmaUnavailable(RuntimeError):
    """Ollama is not running or the Gemma 4 model is not pulled."""


class GemmaBadOutput(RuntimeError):
    """Gemma answered, but not with valid JSON."""


def status() -> dict:
    """Is Ollama up, and is the Gemma 4 model available?"""
    if settings.mock_gemma:
        return {"ollama": "mock", "model": settings.gemma_model, "model_ready": True}
    try:
        r = httpx.get(f"{settings.ollama_url}/api/tags", timeout=3.0)
        r.raise_for_status()
        names = {m.get("name") for m in r.json().get("models", [])}
        return {"ollama": "up", "model": settings.gemma_model, "model_ready": settings.gemma_model in names}
    except Exception:
        return {"ollama": "down", "model": settings.gemma_model, "model_ready": False}


def analyze(user_prompt: str, images_b64: list[str]) -> tuple[dict, dict]:
    """Send the prompt + images to Gemma 4. Returns (parsed_json, timing_info)."""
    payload = {
        "model": settings.gemma_model,
        "stream": False,
        "think": settings.think,
        "format": OUTPUT_SCHEMA,
        "keep_alive": -1,
        "options": {"temperature": 0.2, "num_ctx": settings.num_ctx},
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
    content = (data.get("message") or {}).get("content", "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise GemmaBadOutput("Gemma 4 returned an answer that wasn't valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise GemmaBadOutput("Gemma 4 returned JSON in an unexpected shape.")

    timing = {
        "seconds": round(time.monotonic() - started, 1),
        "prompt_tokens": data.get("prompt_eval_count"),
        "output_tokens": data.get("eval_count"),
    }
    return parsed, timing


def mock_answer(body_part: str, modality: str) -> dict:
    """A realistic fake answer so the frontend team can build without Gemma running."""
    time.sleep(settings.mock_delay_seconds)
    return {
        "is_medical_image": True,
        "detected_body_part": body_part,
        "image_quality": "good",
        "summary": (
            "This is a mock answer for frontend testing. The bones line up normally and there is no clear break. "
            "The joint space looks even on both sides."
        ),
        "findings": [
            {
                "finding": "No clear fracture",
                "location": "Main bones in view",
                "confidence": "moderate",
                "explanation": "The outer edges of the bones look smooth and unbroken.",
            },
            {
                "finding": "Mild soft-tissue swelling",
                "location": "Around the joint",
                "confidence": "low",
                "explanation": "The skin and muscle shadow looks slightly thicker than usual, which can happen after an injury.",
            },
        ],
        "overall_confidence": "moderate",
        "confidence_reason": "The image is clear, but small cracks can be hard to see on a single view.",
        "urgency": "soon",
        "reference_comparison": "The image looks closer to the normal reference case than to the abnormal one.",
        "next_steps": ["Ask your doctor for the radiologist's official report.", "Rest and ice the area until your appointment."],
        "questions_for_doctor": ["Do I need another view or an MRI?", "When can I go back to normal activity?"],
    }
