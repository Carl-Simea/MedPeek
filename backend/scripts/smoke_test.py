"""Send one image to the running API and print the result.

    python scripts/smoke_test.py path/to/xray.png --body-part chest --modality xray
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--body-part", required=True)
    ap.add_argument("--modality", default="xray")
    ap.add_argument("--question", default="What does my image show?")
    ap.add_argument("--api", default="http://localhost:8000")
    args = ap.parse_args()

    with open(args.image, "rb") as fh:
        r = httpx.post(
            f"{args.api}/api/analyze",
            files={"image": (args.image.split("/")[-1], fh)},
            data={"body_part": args.body_part, "modality": args.modality, "question": args.question},
            timeout=60,
        )
    if r.status_code != 202:
        print(r.status_code, r.text)
        return 1
    job_id = r.json()["job_id"]
    print(f"job {job_id} queued", flush=True)
    start = time.monotonic()
    while True:
        job = httpx.get(f"{args.api}/api/jobs/{job_id}", timeout=30).json()
        if job["status"] in ("done", "error"):
            break
        print(f"  {job['status']}... {time.monotonic() - start:.0f}s", flush=True)
        time.sleep(5)
    print(json.dumps(job, indent=2))
    return 0 if job["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
