"""Talks to Ollama, and gets Gemma ready as soon as MedPeek starts.

On startup (in a background thread, so the website opens right away):
  1. if Ollama isn't answering, run `ollama serve`
  2. if the model isn't downloaded, pull it (same as `ollama pull gemma4`)
  3. load the model into memory and keep it there, so the first X-ray isn't slow
The website shows the progress through STATUS.
"""
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

OLLAMA_URL = (os.environ.get("OLLAMA_URL") or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = "http://" + OLLAMA_URL
MODEL = os.environ.get("MEDPEEK_MODEL", "gemma4")
KEEP_ALIVE = "2h"  # how long Ollama keeps the model loaded after the last request
OPTIONS = {"temperature": 0, "num_ctx": 8192}  # same settings for warm-up and chat, so Ollama never reloads

STATUS = {"state": "starting", "message": "Starting Gemma...", "progress": None}
_lock = threading.Lock()
_started = False


class OllamaError(Exception):
    pass


def _set(state, message, progress=None):
    STATUS.update(state=state, message=message, progress=progress)
    print(f"[MedPeek] {message}")


def _request(path, body=None, timeout=10):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(OLLAMA_URL + path, data=data, headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def server_up():
    try:
        with _request("/api/version", timeout=3):
            return True
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False


def _model_names():
    with _request("/api/tags") as response:
        return {m["name"] for m in json.load(response).get("models", [])}


def _has_model():
    names = _model_names()
    return MODEL in names or (":" not in MODEL and f"{MODEL}:latest" in names)


def _start_server():
    if server_up():
        return
    if not shutil.which("ollama"):
        raise OllamaError(f"Ollama isn't installed where MedPeek is running, and nothing answers at {OLLAMA_URL}.")
    _set("starting", "Starting Ollama (ollama serve)...")
    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        time.sleep(0.5)
        if server_up():
            return
    raise OllamaError("Started ollama serve, but it didn't answer within 30 seconds.")


def _pull_model():
    if _has_model():
        return
    _set("pulling", f"Downloading {MODEL} (ollama pull {MODEL})...", 0)
    with _request("/api/pull", {"model": MODEL, "stream": True}, timeout=3600) as response:
        for line in response:
            if not line.strip():
                continue
            update = json.loads(line)
            if "error" in update:
                raise OllamaError(f"Couldn't download {MODEL}: {update['error']}")
            if update.get("total") and update.get("completed"):
                percent = int(update["completed"] * 100 / update["total"])
                _set("pulling", f"Downloading {MODEL}: {percent}%", percent)


def _warm_up():
    _set("loading", f"Loading {MODEL} into memory...")
    body = {"model": MODEL, "prompt": "", "keep_alive": KEEP_ALIVE, "options": OPTIONS}
    with _request("/api/generate", body, timeout=600):
        pass


def _ollama_reason(error):
    """Ollama puts the real reason in the response body, e.g. {"error": "model requires more system memory"}."""
    try:
        body = error.read().decode("utf-8", "replace")
        return json.loads(body).get("error") or body
    except (ValueError, OSError, AttributeError):
        return str(error)


def _explain(reason):
    """Turn Ollama's most common errors into a fix."""
    text = reason.lower()
    if "requires more system memory" in text or "out of memory" in text:
        return (f"{reason}. Not enough RAM for {MODEL}: run MedPeek on a computer with more memory, "
                f"or use a smaller model (MEDPEEK_MODEL=gemma4:e2b).")
    if "process has terminated" in text:
        return (f"Ollama couldn't start {MODEL} on this machine ({reason[:160]}). This usually means it doesn't "
                f"have enough memory: run MedPeek on a computer with more RAM, or update Ollama.")
    if "architecture" in text or "unsupported" in text or "not supported" in text:
        return f"{reason}. Your Ollama is too old for {MODEL}: update Ollama and restart."
    if "not found" in text:
        return f"{reason}. Run: ollama pull {MODEL}"
    return reason


def prepare():
    try:
        _start_server()
        _pull_model()
        _warm_up()
        _set("ready", f"{MODEL} is ready")
    except urllib.error.HTTPError as error:
        _set("error", _explain(_ollama_reason(error)))
    except (OllamaError, urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as error:
        _set("error", str(getattr(error, "reason", error)))


def start_in_background():
    """Run prepare() once per process, without blocking the website."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=prepare, daemon=True).start()


def retry():
    global _started
    with _lock:
        _started = False
    _set("starting", "Retrying...")
    start_in_background()


def chat(messages, schema=None, timeout=600):
    """Send a chat to the model and return the reply text."""
    body = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": OPTIONS,
    }
    if schema:
        body["format"] = schema
    try:
        with _request("/api/chat", body, timeout=timeout) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as error:
        reason = _explain(_ollama_reason(error))
        _set("error", reason)
        raise OllamaError(f"Ollama said: {reason}") from error
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        start_in_background()
        raise OllamaError(f"Couldn't reach Ollama at {OLLAMA_URL}.") from error
    if STATUS["state"] != "ready":
        _set("ready", f"{MODEL} is ready")
    return reply["message"]["content"]