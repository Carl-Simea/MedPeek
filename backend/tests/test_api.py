import io
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.imaging import ImageError, prepare_image
from app.main import app, library
from app.prompts import build_user_prompt
from app.safety import finalize

client = TestClient(app)
ADMIN = {"X-Admin-Token": "test-token"}


def png_bytes(size=(256, 256), color=128) -> bytes:
    buf = io.BytesIO()
    Image.new("L", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_health_and_options():
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["mock"] is True
    opts = client.get("/api/options").json()
    ids = {b["id"] for b in opts["body_parts"]}
    assert {"chest", "knee", "wrist", "lumbar_spine"} <= ids
    assert {m["id"] for m in opts["modalities"]} == {"xray", "mri", "ct"}


def test_analyze_wait_returns_result_with_guardrails():
    r = client.post(
        "/api/analyze?wait=true",
        files={"image": ("knee.png", png_bytes(), "image/png")},
        data={"body_part": "Knee", "modality": "x-ray", "question": "Is it broken?"},
    )
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["body_part"] == "knee" and res["modality"] == "xray"
    assert res["disclaimer"] and res["summary"]
    assert res["overall_confidence"] in ("moderate", "low")  # no references -> capped
    assert any("doctor" in s.lower() for s in res["next_steps"])
    assert res["references_used"] == []


def test_analyze_job_polling():
    r = client.post(
        "/api/analyze",
        files={"image": ("wrist.jpg", png_bytes(), "image/png")},
        data={"body_part": "wrist"},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "done":
            break
        time.sleep(0.1)
    assert job["status"] == "done"
    assert job["result"]["body_part"] == "wrist"


def test_bad_inputs():
    r = client.post("/api/analyze", files={"image": ("x.png", b"not an image", "image/png")}, data={"body_part": "chest"})
    assert r.status_code == 400
    r = client.post("/api/analyze", files={"image": ("x.png", png_bytes(), "image/png")}, data={"body_part": "elbowz"})
    assert r.status_code == 422
    assert client.get("/api/jobs/nope").status_code == 404


def test_reference_library_flow():
    files = {"image": ("ref.png", png_bytes(color=90), "image/png")}
    data = {"body_part": "knee", "modality": "xray", "report": "No fracture. Joint spaces preserved.", "label": "normal"}
    assert client.post("/api/references", files=files, data=data).status_code == 401
    r = client.post("/api/references", files=files, data=data, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert "report" not in r.json()
    assert client.get("/api/references/stats").json()["by_body_part"]["knee"]["xray"]["normal"] >= 1

    res = client.post(
        "/api/analyze?wait=true",
        files={"image": ("knee.png", png_bytes(), "image/png")},
        data={"body_part": "knee", "modality": "xray"},
    ).json()
    assert len(res["references_used"]) == 1
    assert res["references_used"][0]["label"] == "normal"


def test_retrieval_prefers_normal_and_abnormal_pair(tmp_path):
    from app.references import ReferenceLibrary

    lib = ReferenceLibrary(tmp_path)
    for i, label in enumerate(["normal", "normal", "abnormal"]):
        lib.add_case(image_bytes=png_bytes(color=50 + i), filename=f"c{i}.png", body_part="chest",
                     modality="xray", report=f"report {i}", label=label)
    picked = lib.retrieve("chest", "xray", 2)
    assert sorted(c.label for c in picked) == ["abnormal", "normal"]
    assert lib.retrieve("knee", "xray", 2) == []


def test_finalize_rules():
    base = {
        "is_medical_image": True, "detected_body_part": "chest", "image_quality": "good", "summary": "s",
        "findings": [{"finding": "f", "location": "l", "confidence": "high", "explanation": "e"}],
        "overall_confidence": "high", "confidence_reason": "r", "urgency": "emergency",
        "reference_comparison": "", "next_steps": [], "questions_for_doctor": [],
    }
    out = finalize(base, body_part="chest", modality="xray", references_used=2)
    assert out["overall_confidence"] == "high" and out["urgency_notice"]
    assert any("doctor" in s.lower() for s in out["next_steps"])

    out = finalize({**base, "image_quality": "limited"}, body_part="chest", modality="xray", references_used=2)
    assert out["overall_confidence"] == "moderate" and out["findings"][0]["confidence"] == "moderate"

    out = finalize({**base, "is_medical_image": False}, body_part="chest", modality="xray", references_used=2)
    assert out["overall_confidence"] == "low" and out["findings"] == [] and out["urgency"] == "routine"

    out = finalize({**base, "detected_body_part": "left knee"}, body_part="chest", modality="xray", references_used=2)
    assert out["overall_confidence"] == "moderate" and out["safety_notes"]


def test_prompt_mentions_patient_image_last():
    from app.references import ReferenceCase

    refs = [
        ReferenceCase("a", "chest", "xray", "normal", "", "Clear lungs.", "lab", "", "", "x"),
        ReferenceCase("b", "chest", "xray", "abnormal", "pneumonia", "Right lower lobe consolidation.", "lab", "", "", "y"),
    ]
    text = build_user_prompt(body_part="chest", modality="xray", question="", references=refs, reference_images_attached=True)
    assert "Image 3 (the last one) is the PATIENT'S image" in text
    assert "pneumonia" in text and "Clear lungs." in text


def test_dicom_upload_is_converted():
    pydicom = pytest.importorskip("pydicom")
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


def test_tiny_or_empty_images_rejected():
    with pytest.raises(ImageError):
        prepare_image(b"", "x.png")
    with pytest.raises(ImageError):
        prepare_image(png_bytes(size=(20, 20)), "x.png")
