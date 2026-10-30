"""A tiny in-memory job queue.

Gemma 4 runs on CPU in the Codespace and takes a minute or more per image, so
/api/analyze hands back a job id right away and the frontend polls for the result.
One worker runs at a time because the model can only do one image at a time.
Uploaded images live only in memory and are dropped once the job finishes.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

MAX_JOBS_KEPT = 200


class JobQueue:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gemma")
        self._jobs: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = threading.Lock()

    def submit(self, fn: Callable[[], dict]) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {"status": "queued", "created": time.monotonic(), "result": None, "error": None}
            while len(self._jobs) > MAX_JOBS_KEPT:
                self._jobs.popitem(last=False)
        self._executor.submit(self._run, job_id, fn)
        return job_id

    def waiting(self) -> int:
        """How many jobs are queued or running right now."""
        with self._lock:
            return sum(1 for j in self._jobs.values() if j["status"] in ("queued", "running"))

    def _run(self, job_id: str, fn: Callable[[], dict]) -> None:
        self._update(job_id, status="running", started=time.monotonic())
        try:
            result = fn()
            self._update(job_id, status="done", result=result, finished=time.monotonic())
        except Exception as exc:  # report any failure to the frontend instead of hanging
            self._update(job_id, status="error", error=str(exc) or exc.__class__.__name__, finished=time.monotonic())

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(fields)

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job = dict(job)
            waiting = [jid for jid, j in self._jobs.items() if j["status"] in ("queued", "running")]
        end = job.get("finished") or time.monotonic()
        out = {
            "job_id": job_id,
            "status": job["status"],
            "position_in_queue": waiting.index(job_id) if job_id in waiting and job["status"] == "queued" else None,
            "elapsed_seconds": round(end - job["created"], 1),
            "result": job["result"],
            "error": job["error"],
        }
        return out
