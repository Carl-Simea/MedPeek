# MedPeek backend (API v2.0): Gemma 4 chat, image reader and care finder

Three things happen here:

- **Chat without an image** (`POST /api/chat`): a patient describes what is going on in plain words. Gemma 4 answers
  like a careful nurse, asks for the scan when it would help, names the kind of doctor to see and the urgency, and
  asks for a ZIP code when nearby care would help. Health questions only; unrelated topics are declined.
- **Image reading** (`POST /api/analyze`): an X-ray, MRI or CT is cleaned and compared with verified reference cases,
  then explained by Gemma 4 (details below).
- **Care finder** (`POST /api/nearby`, `GET /api/zip/{zip}`): from a US ZIP code, the three closest emergency rooms,
  urgent care centers and the right kind of specialist, with distance, phone and directions links. Built only
  from free public data: CMS Hospital General Information (which hospitals have an emergency department),
  HIFLD hospital locations, the NPI Registry of licensed clinicians (live lookup) and GeoNames ZIP codes.
  No API key needed; the map in the frontend uses OpenStreetMap tiles.

## Image reading

A patient uploads an X-ray, MRI or CT image. The backend:

1. Cleans the image (JPG, PNG, WEBP or DICOM; names, IDs and other metadata are stripped).
2. Finds the most similar **verified reference cases** for the same body part (open, labeled datasets; see below).
3. Asks **Gemma 4** (running locally through Ollama, in this Codespace) to read the image against those cases
   and against the list of conditions our verified data covers for that body part.
4. Applies safety rules on the server:
   - **No confidence score.** Every report ends with the highlighted notice **"MedPeek is AI and can make mistakes."**
     (`ai_notice`).
   - The server double-checks whether the condition is in our verified data (`condition_in_reference_data`,
     explained to the patient in `coverage_note`). If it isn't, the patient is told to see a specialist to confirm.
   - **Every answer names the kind of doctor to see** (`referral.specialist`, e.g. "Orthopedic surgeon (orthopedics)",
     "Orthopedic oncologist", "Pulmonologist (lung doctor)"). `referral.required` is `true` when the condition isn't
     covered by our data, when something abnormal was found, or when it's urgent.
   - Emergencies point to the emergency department, with a "call 911" notice.
5. Returns plain-language JSON for the frontend.

It explains images for patients. It does not diagnose.

**Two Gemma 4 engines, same answers:**
- **Google-hosted Gemma 4** (`gemma-4-26b-a4b-it` on Google AI Studio): answers in seconds. Used automatically when
  `GEMINI_API_KEY` is set in `backend/.env` (get a free key at aistudio.google.com/apikey; never commit it).
- **Local Gemma 4** (`gemma4:latest` through Ollama in the Codespace): about 4 minutes on CPU. Used when there is no
  key, and as the automatic backup if a hosted call fails.
`GET /api/health` shows which engine is active.

## Run it (in the Codespace)

```bash
bash ~/start-gemma.sh                 # local Ollama + Gemma 4 backup (optional when GEMINI_API_KEY is set)
cd v1-cloud/backend && ./run.sh       # API on port 8000
cd v1-cloud/frontend && ./run.sh      # the web app on port 5000 (talks to the API on 8000)
```

Open the forwarded port 8000 (Ports tab):
- `/try` is a simple test page: upload an X-ray (or click a sample) and see the full answer. It's for testing, not the team's frontend.
- `/docs` lets you try every endpoint.

Sample X-rays for `/try` (held-out images that are not in the reference library):
`.venv/bin/python scripts/make_samples.py` (after building the library). `GET /api/samples` lists them.

**Frontend team, no Gemma needed:** `MOCK_GEMMA=1 ./run.sh` returns a realistic fake answer in 2 seconds.

Tests: `cd backend && .venv/bin/python -m pytest -q`

## API for the frontend

Base URL: the forwarded port 8000 URL (or `http://localhost:8000`). CORS is open.

### `GET /api/options`
Body parts and imaging types for the dropdowns, with how many verified cases exist for each.

```json
{"body_parts": [{"id": "chest_adult", "label": "Chest (adults)", "reference_cases": 428}, ...],
 "modalities": [{"id": "xray", "label": "X-ray"}, {"id": "mri", "label": "MRI"}, {"id": "ct", "label": "CT scan"}]}
```

Body part ids: `skull_face_sinuses`, `teeth_jaw`, `neck_cervical_spine`, `chest_adult`, `chest_child`, `breast`,
`back_spine`, `abdomen_kidneys`, `pelvis_hip`, `shoulder_upper_arm`, `elbow_forearm`, `wrist_hand`, `thigh_knee`,
`lower_leg_foot`, `whole_body`. Plain words also work (`knee`, `wrist`, `chest`, `lumbar_spine`, `other`).

### `POST /api/chat` (JSON)

```json
{"messages": [{"role": "user", "content": "I fell on my wrist yesterday and it is swollen"}],
 "zip": "11218", "report": null, "find_care": true}
```

`messages` is the conversation so far (up to 30 turns, user/assistant). `report` is the last MedPeek image report in
this chat, so follow-up questions can refer to it. The reply:

```json
{"reply": "A swollen wrist after a fall should be checked for a fracture...", "topic_ok": true,
 "needs_image": true, "suggested_body_part": "wrist_hand", "suggested_body_part_label": "Wrist, hand and fingers",
 "specialist": "Orthopedic surgeon (orthopedics)", "urgency": "urgent", "urgency_notice": "...",
 "zip": "11218", "care": {"zip": "11218", "city": "Brooklyn", "state": "NY", "groups": [...]}, "care_error": "",
 "ai_notice": "MedPeek is AI and can make mistakes.", "engine": "google", "model": "gemma-4-26b-a4b-it", "processing_seconds": 4.6}
```

`care` is filled in when the patient asked for nearby help (or an emergency was detected) and a ZIP code is known
(from `zip` or typed in the message). Emergencies force `specialist` to the emergency department and list ERs only.
Limit: 20 chat turns per minute per visitor.

### `POST /api/nearby` (JSON) and `GET /api/zip/{zip_code}`

```json
{"zip": "11218", "specialist": "Orthopedic surgeon (orthopedics)", "urgency": "urgent", "limit": 3,
 "include": ["emergency", "urgent_care", "specialist"]}
```

Returns `{"zip", "city", "state", "lat", "lon", "groups": [{"kind": "specialist", "title": "...", "why": "...",
"places": [{"name", "address", "phone", "miles", "lat", "lon", "emergency", "directions_url", "apple_maps_url",
"approx"}]}], "note"}`. Groups come specialist first, then urgent care, then emergency rooms (emergency rooms first
when `urgency` is `emergency`). `approx: true` marks a place located by its ZIP centroid rather than an exact
address. US ZIP codes only (`422` otherwise). `GET /api/zip/11218` just validates a ZIP and returns city/state.
The hospital tables are built by `scripts/build_places.py` into `app/geo/` (small, committed); clinician lookups
hit the NPI Registry live and are cached for six hours.

### `POST /api/analyze` (multipart form)

| field | required | example |
| --- | --- | --- |
| `image` | yes | the file (JPG, PNG, WEBP or .dcm, max 25 MB) |
| `body_part` | no (default `auto`) | an id from `/api/options`, plain words (`knee`), or `auto`: MedPeek uses the body part named in the question; otherwise hosted Gemma 4 looks at the image first (a few seconds), and with only the local engine the most similar verified images decide |
| `modality` | no (default `xray`) | `xray`, `mri`, `ct`, or `auto` (hosted Gemma 4 tells X-ray, MRI and CT apart; X-ray without it). The web app sends `auto` unless the patient picks one |
| `question` | no | "Is my wrist broken?" |

Returns `202 {"job_id": "...", "status": "queued", "poll_url": "/api/jobs/<id>"}` right away.
A result takes seconds with the hosted engine, about 4 minutes with the local one. Jobs run one at a time;
`position_in_queue` says where you are. Limits for the public URL: 8 analyses per minute per visitor (`429`) and at
most 10 waiting jobs (`503`), both set in `.env`.

### `GET /api/jobs/{job_id}`
Poll every 3-5 seconds. `status` goes `queued` -> `running` -> `done` (or `error`).

```json
{
  "job_id": "1cbeea059e39", "status": "done", "position_in_queue": null, "elapsed_seconds": 142.0, "error": null,
  "result": {
    "body_part": "wrist_hand", "body_part_label": "Wrist, hand and fingers", "modality": "xray", "modality_label": "X-ray",
    "question": "Is my wrist broken?",
    "is_medical_image": true, "image_quality": "good",
    "primary_condition": "fracture",
    "condition_in_reference_data": true,
    "coverage_note": "This matches conditions in our verified wrist, hand and fingers cases.",
    "summary": "Plain-language explanation for the patient...",
    "findings": [
      {"finding": "Possible break of the distal radius", "location": "Near the wrist",
       "explanation": "A thin dark line runs across the bone...", "supported_by_reference_data": true}
    ],
    "urgency": "urgent",
    "urgency_notice": "Contact a doctor within the next day or two to go over this image.",
    "referral": {"specialist": "Orthopedic surgeon (orthopedics)",
                 "reason": "Orthopedic surgeons treat broken bones...", "timeframe": "Within 1-2 days", "required": true},
    "reference_comparison": "How it compares with the verified cases",
    "next_steps": ["See an orthopedic surgeon (within 1-2 days).", "..."],
    "questions_for_doctor": ["..."],
    "references_used": [{"case_id": "...", "label": "abnormal", "diagnosis": "fracture", "source": "FracAtlas",
                         "verified_by": "Expert-annotated fracture labels (FracAtlas, Scientific Data 2023)"}],
    "reference_library_size": 154,
    "safety_notes": ["..."],
    "disclaimer": "MedPeek explains medical images with Gemma 4... It is not a diagnosis...",
    "ai_notice": "MedPeek is AI and can make mistakes.",
    "body_part_detected_by": "user",
    "engine": "google", "model": "gemma-4-26b-a4b-it", "mock": false, "processing_seconds": 12.4
  }
}
```

Display tips:
- Always show `summary`, the `referral` card, `coverage_note` and `disclaimer`. Highlight the referral when
  `referral.required` is true.
- **End every report with `ai_notice` ("MedPeek is AI and can make mistakes.") in a highlighted box.**
- Show `urgency_notice` in a red banner when it isn't `null`.
- List `safety_notes` under the result; `references_used` can go in a "Compared with" section.
- For the X_ray_Examiner Flask app, `examples/x_ray_examiner_analyzer.py` is a drop-in `analyze_xray()` that calls
  this API (instructions at the top of the file).

### JavaScript example

```js
const API = "https://<your-codespace>-8000.app.github.dev"; // or "" if served by this backend

async function analyze(file, bodyPart, modality, question) {
  const form = new FormData();
  form.append("image", file);
  form.append("body_part", bodyPart);
  form.append("modality", modality);
  form.append("question", question);
  const { job_id } = await (await fetch(`${API}/api/analyze`, { method: "POST", body: form })).json();
  while (true) {
    await new Promise((r) => setTimeout(r, 4000));
    const job = await (await fetch(`${API}/api/jobs/${job_id}`)).json();
    if (job.status === "done") return job.result;
    if (job.status === "error") throw new Error(job.error);
  }
}
```

If the frontend lives in `frontend/` (or a built `frontend/dist/`) in this repo, the backend serves it at `/`,
so the whole app runs from one URL and `API` can be `""`.

## Reference library (the verified data Gemma checks first)

Gemma 4 isn't retrained (that needs GPUs). Instead, every answer is grounded in verified cases: the most
similar abnormal and normal case for that body part are shown to Gemma (image + verified label/report), along
with the list of conditions our data covers. If the patient's condition isn't on that list, the report says so and
the patient is referred to the right specialist.

Build it. Source archives are downloaded to `/tmp/datasets` (resumable; `MAX_SOURCE_GB`, default 25, caps any one
file) and are never committed. The big optional archives are only used once they are on disk, so download first:

```bash
cd backend
uv pip install --python .venv/bin/python -r scripts/requirements-build.txt   # h5py + SimpleITK, build only
.venv/bin/python scripts/fetch_sources.py --list                  # the download plan, by body part (~88 GB)
.venv/bin/python scripts/fetch_sources.py --groups all --parallel 4
.venv/bin/python scripts/build_reference_library.py --dry-run     # see what would be added
.venv/bin/python scripts/build_reference_library.py --per-class 400 --workers 3   # add cases, keeping current ones
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/api/references/reload   # or restart the API
```

The original hackathon sources (small files):

| Source | What it adds | Size | License |
| --- | --- | --- | --- |
| FracAtlas | fracture / no fracture: hand-wrist, leg, hip, shoulder | 0.34 GB | CC BY 4.0 |
| OrthoFrac-XR | fractures, healing fractures, malunion, normal: arm, leg, hip, wrist | 0.52 GB | CC BY 4.0 |
| BTXRD | benign and malignant bone tumors vs. no tumor, by location | 0.84 GB | CC BY 4.0 |
| BDCXR-3257 | children's chest: normal, bacterial and viral pneumonia | 0.92 GB | CC BY 4.0 |
| Indiana University chest X-rays (test + 2 train shards) | adult chest images with real radiologist reports | 0.96 GB | CC BY-NC-ND 4.0 |
| ROCOv2 (validation + test images) | X-rays from published case reports with author captions | 1.7 GB | CC BY-NC 4.0 |
| Mammo-MX (BI-RADS 0 and 6 sets) | mammograms: biopsy-proven cancer, "needs more imaging" | 0.52 GB | CC BY 4.0 |

The November 2026 expansion (about 8 GB of source data per body part wherever open data exists):

| Body part | Source | What it adds | Download | License |
| --- | --- | --- | --- | --- |
| Chest (adults) | NIH ChestX-ray14, tarballs 002-003 (+ labels) | 14 findings labeled from radiology reports: effusion, pneumothorax, cardiomegaly, nodule, mass, edema, emphysema... and "No Finding" | 7.9 GB | NIH open data |
| Chest (children) | Kermany pediatric chest X-rays; Nigerian under-5 set; NIH images under 16 | normal, bacterial and viral pneumonia | 8.6 GB | CC BY 4.0 |
| Breast | Mammo-MX BI-RADS 1 (normal) and 3 sets | normal mammograms, probably-benign findings | 8.8 GB (+ B5/B4 optional) | CC BY 4.0 |
| Wrist and hand | GRAZPEDWRI-DX parts 1-2 (+ labels); MURA wrist/hand/finger | children's wrist fractures (AO codes), metal, normal; adult normal/abnormal | 8.2 GB | CC BY 4.0 |
| Teeth and jaw | DENTEX; DentIRO | panoramic X-rays with dentist-marked caries, deep caries, root infections, impacted teeth; single-tooth X-rays | 11.6 GB | CC BY 4.0 |
| Abdomen and kidneys | KUB-StoneX | kidney, ureter and bladder stones vs. no stone, marked by two radiologists | 20.8 GB | CC BY 4.0 |
| Thigh and knee | Knee osteoarthritis grading (OAI) | Kellgren-Lawrence grades 0-4 | 7.2 GB | CC BY 4.0 |
| Shoulder, elbow, forearm | MURA (Stanford); PediaSHF-DX; UCI shoulder implants | normal vs. abnormal upper-limb studies; children's elbow fractures; shoulder replacements | 4.3 GB | MURA research-use agreement; CC BY 4.0 |
| Skull (brain MRI) | Brain tumor MRI (Cheng et al.) | meningioma, glioma, pituitary tumor slices (MRI) | 0.9 GB | CC BY 4.0 |
| Neck | Cervical Spine X-ray Atlas (CSXA) | cervical spondylosis vs. healthy, neck curvature | 1.4 GB | CC BY 4.0 (per the Hugging Face copy) |
| Back | SPIDER lumbar spine MRI | herniated discs, slipped vertebrae, disc degeneration (MRI, per-disc radiologist grading) | 3.7 GB | CC BY 4.0 |
| Many parts, incl. CT and MRI | ROCOv2 train split | published case-report images: X-ray, CT and MRI | 4.6 GB | CC BY-NC 4.0 |

Honest gaps, after checking every open repository we could find (Zenodo, figshare, Mendeley, Hugging Face, NIH, NLM):
no open, labeled X-ray set of real size exists for **pelvis/hip** (~3 GB total, keypoints only), **lower leg/foot**,
**neck** (1.4 GB) or **skull/face/sinuses** (cephalograms carry landmarks, not diagnoses); those parts rely on
ROCOv2 case reports plus the sets above. Chest (children) has about 1.2 GB of real pediatric images inside the
8.4 GB Kermany archive (the rest is eye OCT). MURA labels only normal vs. abnormal, not the exact finding.
NIH ChestX-ray14 labels were mined from reports by NIH's text tool (about 90% accurate), which every case says.
CT and MRI cases from ROCOv2 take their body part and finding from the published caption, so they are
broad ("mass, nodule or cyst", "bleeding in the brain") rather than a radiologist's full read.

The library lives in `backend/data/reference/`, which is **git-ignored**: medical images and reports are never
pushed to this public repo. `backend/data/reference/library_summary.json` lists what's inside (cases per body part
and imaging type, conditions covered, sources and licenses). Current build: **39,706 verified cases** (31,190 X-ray · 5,302 CT · 3,214 MRI) from 21 sources, built 2026-11-04.

| Body part | X-ray | CT | MRI | Most common findings |
| --- | ---: | ---: | ---: | --- |
| Skull, face and sinuses | 271 | 518 | 1,452 | brain tumor, pituitary tumor, mass, nodule or cyst |
| Teeth and jaw | 2,977 | 95 | 14 | tooth decay or tooth infection, dental crown, root canal filling |
| Neck and cervical spine | 733 | 303 | 172 | arthritis / joint wear, mass, nodule or cyst, surgical hardware or implant |
| Chest (adults) | 7,533 | 1,996 | 64 | pneumonia / consolidation, pleural effusion, mass, nodule or cyst |
| Chest (children) | 1,808 | 15 | 1 | pneumonia, pneumonia / consolidation, pleural effusion |
| Breast (mammogram) | 945 | 59 | 48 | probably benign finding, mass, nodule or cyst, breast cancer |
| Back (thoracic and lumbar spine) | 325 | 143 | 502 | mass, nodule or cyst, disc degeneration, surgical hardware or implant |
| Abdomen and kidneys | 2,608 | 1,668 | 320 | mass, nodule or cyst, kidney stone, bladder stone |
| Pelvis and hip | 1,432 | 356 | 311 | mass, nodule or cyst, fracture, surgical hardware or implant |
| Shoulder, collarbone and upper arm | 2,345 | 81 | 72 | surgical hardware or implant, fracture, mass, nodule or cyst |
| Elbow and forearm | 2,185 | 6 | 24 | fracture, healing fracture, osteochondroma |
| Wrist, hand and fingers | 3,926 | 10 | 35 | fracture of the radius near the wrist, fracture of both forearm bones near the wrist, fracture of the ulna near the wrist |
| Thigh and knee | 2,897 | 30 | 137 | very early knee osteoarthritis, mild knee osteoarthritis, moderate knee osteoarthritis |
| Lower leg, ankle and foot | 1,205 | 22 | 62 | fracture, osteochondroma, healing fracture |

Add your own verified cases (images with same-name `.txt`/`.pdf` reports next to them):

```bash
.venv/bin/python scripts/import_references.py /path/to/knee_fractures --body-part thigh_knee --modality xray \
    --label abnormal --diagnosis fracture --source "Example Imaging Lab"
.venv/bin/python scripts/import_references.py --csv /path/to/manifest.csv   # image,report,body_part,modality,label,diagnosis,source,verified_by
```

Or one at a time through `POST /api/references` (header `X-Admin-Token`, set `ADMIN_TOKEN` in `backend/.env`).

## Settings (`backend/.env`)

See `.env.example`: model name, mock mode, how many reference cases per answer (`MAX_REFERENCE_CASES`, default 2),
whether reference images are sent along with their reports (`SEND_REFERENCE_IMAGES`), upload size limit and admin token.
