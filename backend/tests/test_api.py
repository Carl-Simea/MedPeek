import json
import io
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.imaging import ImageError, prepare_image, thumbnail_vector
from app.main import app
from app.prompts import build_user_prompt
from app.references import ReferenceCase, ReferenceLibrary
from app.safety import condition_is_covered, finalize

client = TestClient(app)
ADMIN = {"X-Admin-Token": "test-token"}


def png_bytes(size=(256, 256), color=128, pattern=False) -> bytes:
    buf = io.BytesIO()
    if pattern:
        arr = (np.indices(size).sum(axis=0) % 64 * 4).astype("uint8")
        Image.fromarray(arr, "L").save(buf, format="PNG")
    else:
        Image.new("L", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_health_and_options():
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["mock"] is True
    opts = client.get("/api/options").json()
    ids = {b["id"] for b in opts["body_parts"]}
    assert {"chest_adult", "chest_child", "wrist_hand", "thigh_knee", "teeth_jaw", "breast"} <= ids
    assert len(ids) == 15
    assert {m["id"] for m in opts["modalities"]} == {"xray", "mri", "ct"}


def test_analyze_without_references_refers_and_adds_ai_notice():
    r = client.post(
        "/api/analyze?wait=true",
        files={"image": ("knee.png", png_bytes(), "image/png")},
        data={"body_part": "knee", "modality": "x-ray", "question": "Is it broken?"},
    )
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["body_part"] == "thigh_knee" and res["modality"] == "xray"
    assert "overall_confidence" not in res and all("confidence" not in f for f in res["findings"])
    assert res["condition_in_reference_data"] is False and "don't have verified cases" in res["coverage_note"]
    assert res["ai_notice"] == "MedPeek is AI and can make mistakes."
    assert res["referral"]["required"] is True
    assert res["referral"]["specialist"]
    assert res["disclaimer"] and res["summary"]


def test_analyze_job_polling():
    r = client.post("/api/analyze", files={"image": ("w.jpg", png_bytes(), "image/png")}, data={"body_part": "wrist"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "done":
            break
        time.sleep(0.1)
    assert job["status"] == "done"
    assert job["result"]["body_part"] == "wrist_hand"


def test_bad_inputs():
    r = client.post("/api/analyze", files={"image": ("x.png", b"not an image", "image/png")}, data={"body_part": "chest"})
    assert r.status_code == 400
    r = client.post("/api/analyze", files={"image": ("x.png", png_bytes(), "image/png")}, data={"body_part": "elbowz"})
    assert r.status_code == 422
    assert client.get("/api/jobs/nope").status_code == 404


def test_reference_library_flow_marks_covered_conditions():
    files = {"image": ("ref.png", png_bytes(color=90), "image/png")}
    data = {"body_part": "wrist", "modality": "xray", "report": "Distal radius fracture.", "label": "abnormal",
            "diagnosis": "fracture", "source": "FracAtlas"}
    assert client.post("/api/references", files=files, data=data).status_code == 401
    r = client.post("/api/references", files=files, data=data, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert "report" not in r.json() and "vector" not in r.json()
    assert client.get("/api/references/stats").json()["by_body_part"]["wrist_hand"]["xray"]["abnormal"] >= 1

    res = client.post(
        "/api/analyze?wait=true",
        files={"image": ("wrist.png", png_bytes(), "image/png")},
        data={"body_part": "wrist", "modality": "xray"},
    ).json()
    assert len(res["references_used"]) == 1
    assert res["condition_in_reference_data"] is True and "matches" in res["coverage_note"]
    assert res["findings"][0]["supported_by_reference_data"] is True
    assert res["findings"][1]["supported_by_reference_data"] is False  # that finding isn't backed by the data
    assert res["referral"]["specialist"].startswith("Orthopedic")


def test_retrieval_uses_similarity_and_label_pair(tmp_path):
    lib = ReferenceLibrary(tmp_path)
    lib.add_case(image_bytes=png_bytes(pattern=True), filename="a.png", body_part="chest_adult", modality="xray",
                 report="r", label="abnormal", diagnosis="pneumonia", reload=False)
    lib.add_case(image_bytes=png_bytes(color=30), filename="b.png", body_part="chest_adult", modality="xray",
                 report="r", label="abnormal", diagnosis="effusion", reload=False)
    lib.add_case(image_bytes=png_bytes(color=200), filename="c.png", body_part="chest_adult", modality="xray",
                 report="r", label="normal", reload=True)
    q = thumbnail_vector(png_bytes(pattern=True))
    picked = lib.retrieve("chest_adult", "xray", 2, query_vector=q)
    assert [c.label for c in picked] == ["abnormal", "normal"]
    assert picked[0].diagnosis == "pneumonia"  # the look-alike wins
    assert dict(lib.conditions("chest_adult", "xray"))["normal (no abnormality)"] == 1
    assert lib.retrieve("thigh_knee", "xray", 2, query_vector=q) == []


def _raw(**over):
    base = {
        "is_medical_image": True, "detected_body_part": "chest", "image_quality": "good",
        "primary_condition": "pneumonia", "condition_in_reference_data": True, "summary": "s",
        "findings": [{"finding": "f", "location": "l", "explanation": "e", "supported_by_reference_data": True,
                      "confidence": "high"}],
        "overall_confidence": "high", "confidence_reason": "r", "urgency": "urgent",
        "referral": {"specialist": "Pulmonologist (lung doctor)", "reason": "lungs", "timeframe": "1-2 days"},
        "reference_comparison": "", "next_steps": [], "questions_for_doctor": [],
    }
    base.update(over)
    return base


def test_finalize_rules():
    out = finalize(_raw(), body_part="chest_adult", modality="xray", references_used=2)
    assert out["condition_in_reference_data"] and out["urgency_notice"] and "overall_confidence" not in out
    assert out["referral"]["specialist"] == "Pulmonologist (lung doctor)" and out["referral"]["required"]

    out = finalize(_raw(condition_in_reference_data=False), body_part="chest_adult", modality="xray", references_used=2)
    assert not out["condition_in_reference_data"] and not out["findings"][0]["supported_by_reference_data"]
    assert out["referral"]["required"] and "isn't well covered" in out["coverage_note"]

    out = finalize(_raw(image_quality="limited"), body_part="chest_adult", modality="xray", references_used=2)
    assert any("limited" in n.lower() for n in out["safety_notes"])

    out = finalize(_raw(is_medical_image=False), body_part="chest_adult", modality="xray", references_used=2)
    assert out["findings"] == [] and out["urgency"] == "routine" and not out["condition_in_reference_data"]

    out = finalize(_raw(urgency="emergency"), body_part="chest_adult", modality="xray", references_used=2)
    assert out["referral"]["specialist"] == "Emergency department"

    out = finalize(_raw(referral={"specialist": "Wizard"}), body_part="breast", modality="xray", references_used=2)
    assert out["referral"]["specialist"] == "Breast surgeon"

    out = finalize(_raw(detected_body_part="left knee"), body_part="chest_adult", modality="xray", references_used=2)
    assert any("different body part" in n for n in out["safety_notes"])


def test_prompt_lists_covered_conditions_and_patient_image_last():
    refs = [
        ReferenceCase("a", "chest_adult", "xray", "normal", "", "Clear lungs.", "IU", "", "", "x"),
        ReferenceCase("b", "chest_adult", "xray", "abnormal", "pneumonia", "Right lower lobe consolidation.", "IU", "", "", "y"),
    ]
    text = build_user_prompt(body_part="chest_adult", modality="xray", question="", references=refs,
                             reference_images_attached=True,
                             covered_conditions=[("pneumonia", 40), ("normal (no abnormality)", 60)], sources=["IU"])
    assert "Image 3 (the last one) is the PATIENT'S image" in text
    assert "pneumonia (40)" in text and "Clear lungs." in text


def test_dicom_upload_is_converted():
    pytest.importorskip("pydicom")
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.1"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("x.dcm", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientName = "Should Not Leak"
    ds.Rows, ds.Columns = 128, 128
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
    ds.PixelData = (np.arange(128 * 128, dtype=np.uint16) % 4096).tobytes()
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    prepared = prepare_image(buf.getvalue(), "scan.dcm")
    assert prepared.source_format == "DICOM"
    assert b"Should Not Leak" not in prepared.png_bytes


def test_16_bit_png_is_not_washed_out():
    arr = (np.indices((128, 128)).sum(axis=0) * 100).astype("uint16")  # values up to ~25,000
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    out = Image.open(io.BytesIO(prepare_image(buf.getvalue(), "w.png").png_bytes))
    pixels = np.asarray(out)
    assert pixels.min() < 20 and pixels.max() > 235 and 60 < pixels.mean() < 200


def test_tiny_or_empty_images_rejected():
    with pytest.raises(ImageError):
        prepare_image(b"", "x.png")
    with pytest.raises(ImageError):
        prepare_image(png_bytes(size=(20, 20)), "x.png")


def test_server_double_checks_coverage():
    covered = [("pneumonia / consolidation", 50), ("cardiomegaly (enlarged heart)", 50), ("normal (no abnormality)", 53)]
    out = finalize(_raw(primary_condition="Lung cancer"), body_part="chest_adult", modality="xray", references_used=2,
                   covered_conditions=covered)
    assert not out["condition_in_reference_data"] and out["referral"]["required"]
    assert not out["findings"][0]["supported_by_reference_data"]
    out = finalize(_raw(primary_condition="Right lower lobe pneumonia",
                        findings=[{"finding": "Patchy opacity consistent with pneumonia", "supported_by_reference_data": True,
                                   "confidence": "high"}]),
                   body_part="chest_adult", modality="xray", references_used=2, covered_conditions=covered)
    assert out["condition_in_reference_data"] and out["findings"][0]["supported_by_reference_data"]
    assert condition_is_covered("Possible break of the distal radius", ["fracture", "normal (no abnormality)"])
    assert condition_is_covered("Enlarged heart", ["cardiomegaly (enlarged heart)"])
    assert not condition_is_covered("Soft-tissue swelling", ["fracture"])
    assert condition_is_covered("No acute abnormality", ["normal (no abnormality)", "fracture"])
    assert not condition_is_covered("No acute abnormality", ["fracture"])


def test_referral_wording_uses_the_right_article():
    out = finalize(_raw(condition_in_reference_data=False, referral={"specialist": "Orthopedic surgeon (orthopedics)"}),
                   body_part="thigh_knee", modality="xray", references_used=2)
    assert "please see an orthopedic surgeon to confirm" in out["coverage_note"]
    assert out["next_steps"][0].startswith("See an orthopedic surgeon")
    out = finalize(_raw(urgency="emergency", condition_in_reference_data=False), body_part="chest_adult", modality="xray",
                   references_used=2)
    assert "go to the emergency department" in out["coverage_note"]


def test_try_page_is_served():
    r = client.get("/try")
    assert r.status_code == 200 and "MedPeek" in r.text and "/api/analyze" in r.text


def test_samples_endpoint_is_safe():
    assert client.get("/api/samples").status_code == 200
    assert client.get("/api/samples/..%2F..%2Fapp%2Fmain.py").status_code == 404
    assert client.get("/api/samples/nope.jpg").status_code == 404


def test_auto_body_part_from_question_and_image():
    res = client.post(
        "/api/analyze?wait=true",
        files={"image": ("x.png", png_bytes(), "image/png")},
        data={"question": "I fell on my wrist yesterday"},
    ).json()
    assert res["body_part"] == "wrist_hand" and res["body_part_detected_by"] == "question"
    res = client.post("/api/analyze?wait=true", files={"image": ("x.png", png_bytes(), "image/png")}).json()
    assert res["body_part_detected_by"] == "image" and res["body_part"]


def test_rate_limit_and_health(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "analyze_per_minute", 1)
    from app import main as main_module

    main_module._recent_requests.clear()
    first = client.post("/api/analyze", files={"image": ("x.png", png_bytes(), "image/png")}, data={"body_part": "knee"})
    second = client.post("/api/analyze", files={"image": ("x.png", png_bytes(), "image/png")}, data={"body_part": "knee"})
    assert first.status_code == 202 and second.status_code == 429
    main_module._recent_requests.clear()
    h = client.get("/api/health")
    assert h.json()["service"] == "MedPeek API" and h.headers.get("x-request-id")


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code, self._payload = status_code, payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_google_engine_and_fallback(monkeypatch):
    from app import gemma
    from app.config import settings

    monkeypatch.setattr(settings, "mock_delay_seconds", 0)
    answer, _ = gemma.mock_answer("wrist_hand", "xray")
    monkeypatch.setattr(settings, "mock_gemma", False)
    monkeypatch.setattr(settings, "gemma_provider", "auto")
    monkeypatch.setattr(settings, "google_api_key", "test-key")
    assert gemma.engine() == "google"

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        if "generativelanguage" in url:
            text = "```json\n" + __import__("json").dumps(answer) + "\n```"
            return _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": text}]}}],
                                       "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 300}})
        return _FakeResponse(200, {"message": {"content": __import__("json").dumps(answer)}})

    monkeypatch.setattr(gemma.httpx, "post", fake_post)
    parsed, info = gemma.analyze("prompt", ["aGVsbG8="])
    assert parsed["primary_condition"] == "fracture" and info["engine"] == "google"
    assert calls[0].endswith("gemma-4-26b-a4b-it:generateContent")

    def failing_google(url, json=None, headers=None, timeout=None):
        if "generativelanguage" in url:
            return _FakeResponse(429, {"error": {"message": "quota"}})
        return fake_post(url, json, headers, timeout)

    monkeypatch.setattr(gemma.httpx, "post", failing_google)
    parsed, info = gemma.analyze("prompt", ["aGVsbG8="])
    assert info["engine"] == "ollama" and parsed["summary"]
