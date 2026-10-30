"""Checks for the reference-library builder's labeling rules (no downloads)."""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "build_reference_library", Path(__file__).resolve().parents[1] / "scripts" / "build_reference_library.py"
)
b = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = b  # dataclasses need the module registered
_spec.loader.exec_module(b)


def test_size_limit_is_1gb():
    assert b.MAX_SOURCE_BYTES == 1_000_000_000


def test_negated_findings_are_ignored():
    assert b.first_match(b.positive_text("No fracture. Mild cardiomegaly."), b.IU_DIAGNOSES) == "cardiomegaly (enlarged heart)"
    assert b.first_match(b.positive_text("No pneumothorax or pleural effusion."), b.IU_DIAGNOSES) is None
    assert b.first_match(b.positive_text("Heart size normal, without effusion."), b.IU_DIAGNOSES) is None


def test_rocov2_body_parts():
    assert b.roco_body_part({"C0037303"}, "Panoramic radiograph showing an impacted third molar") == "teeth_jaw"
    assert b.roco_body_part({"C0037303"}, "Skull X-ray with a frontal bone lesion") == "skull_face_sinuses"
    assert b.roco_body_part({"C0037949"}, "Lateral cervical spine X-ray") == "neck_cervical_spine"
    assert b.roco_body_part({"C0817096"}, "Chest X-ray of a 6-year-old boy") == "chest_child"
    assert b.roco_body_part({"C1140618"}, "X-ray of the distal radius after a fall") == "wrist_hand"


def test_rocov2_conditions_fit_the_body_part():
    assert b.roco_diagnosis("thigh_knee", "Knee X-ray showing a joint effusion") == ("abnormal", "joint effusion (fluid in the joint)")
    assert b.roco_diagnosis("shoulder_upper_arm", "Pneumonia in the right lung") is None
    assert b.roco_diagnosis("wrist_hand", "Normal wrist radiograph") == ("normal", "")
