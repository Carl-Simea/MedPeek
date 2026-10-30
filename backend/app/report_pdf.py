"""MedPeek PDF report: the analysis, the date and the scan details, for the patient to bring to a doctor.

The verification code is a fingerprint of the report's contents, so a copy can be checked against
MedPeek's answer. It shows the document came from MedPeek unchanged; it is not a medical certification.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from fpdf import FPDF

TEAL = (15, 118, 110)
AMBER_BG, AMBER_INK = (255, 244, 214), (122, 82, 0)
GRAY = (90, 98, 110)

_REPLACEMENTS = {
    "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...",
    "•": "-", " ": " ", "≥": ">=", "≤": "<=", "°": " deg", "⚠": "!", "→": "->",
}


def _t(value) -> str:
    """Core PDF fonts only know Latin-1, so swap common symbols and drop the rest."""
    text = str(value or "")
    for a, b in _REPLACEMENTS.items():
        text = text.replace(a, b)
    return text.encode("latin-1", "replace").decode("latin-1").replace("?", "?")


def verification_code(result: dict) -> str:
    keys = ("id", "created_at", "body_part", "modality", "summary", "primary_condition", "referral", "findings")
    digest = hashlib.sha256(json.dumps({k: result.get(k) for k in keys}, sort_keys=True).encode()).hexdigest()
    return f"MP-{digest[:4]}-{digest[4:8]}-{digest[8:12]}".upper()


class _Doc(FPDF):
    def header(self) -> None:
        self.set_fill_color(*TEAL)
        self.rect(0, 0, 210, 18, "F")
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 15)
        self.set_xy(12, 5)
        self.cell(0, 8, "MedPeek Report")
        self.set_font("Helvetica", "", 9)
        self.set_xy(-82, 6)
        self.cell(70, 6, "AI explanation of a medical image", align="R")
        self.set_text_color(0, 0, 0)
        self.set_y(24)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*GRAY)
        self.cell(0, 5, _t("MedPeek is AI and can make mistakes. Not a diagnosis. Page %s" % self.page_no()), align="C")


def build_pdf(result: dict, privacy_note: str = "") -> bytes:
    pdf = _Doc(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(12, 12, 12)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def heading(text: str) -> None:
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*TEAL)
        pdf.cell(0, 7, _t(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    def para(text: str, size: int = 10, style: str = "", color=(0, 0, 0)) -> None:
        if not text:
            return
        pdf.set_font("Helvetica", style, size)
        pdf.set_text_color(*color)
        pdf.multi_cell(width, 5, _t(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    def bullets(items) -> None:
        for item in items or []:
            para(f"- {item}")

    created = result.get("created_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        created_text = datetime.fromisoformat(created).strftime("%B %d, %Y at %H:%M UTC")
    except ValueError:
        created_text = created
    image = result.get("image_info") or {}
    size = f"{image.get('width')} x {image.get('height')} px" if image.get("width") else "not recorded"

    rows = [
        ("Report ID", result.get("id", "")),
        ("Date", created_text),
        ("Verification code", verification_code(result)),
        ("Scan type", result.get("modality_label", result.get("modality", ""))),
        ("Body part", f"{result.get('body_part_label', '')}"
                      + (f" (detected from the {result.get('body_part_detected_by')})"
                         if result.get("body_part_detected_by") not in (None, "", "user") else "")),
        ("Image", f"{image.get('format', 'image')} file, {size}, quality: {result.get('image_quality', '')}"),
        ("Read by", f"Gemma 4 ({result.get('model', '')}), compared with {result.get('reference_library_size', 0)} "
                    f"verified {str(result.get('body_part_label', '')).lower()} cases"),
    ]
    pdf.set_font("Helvetica", "", 9)
    for label, value in rows:
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(38, 6, _t(label))
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(width - 38, 6, _t(value), new_x="LMARGIN", new_y="NEXT")

    if result.get("question"):
        heading("Your question")
        para(result["question"], style="I")
    if result.get("urgency_notice"):
        pdf.ln(2)
        pdf.set_fill_color(253, 236, 236)
        pdf.set_text_color(160, 20, 30)
        pdf.set_font("Helvetica", "B", 10)
        pdf.multi_cell(width, 6, _t(result["urgency_notice"]), fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    heading("What the image shows")
    para(result.get("summary", ""))
    para(result.get("coverage_note", ""), size=9, color=GRAY)

    if result.get("findings"):
        heading("Findings")
        for f in result["findings"]:
            para(f.get("finding", ""), style="B")
            para(" - ".join(x for x in (f.get("location", ""), f.get("explanation", "")) if x), size=9)

    ref = result.get("referral") or {}
    if ref.get("specialist"):
        heading("Who to see")
        para(f"{ref['specialist']} - {ref.get('timeframe', '')}", style="B")
        para(ref.get("reason", ""))

    if result.get("next_steps"):
        heading("Next steps")
        bullets(result["next_steps"])
    if result.get("questions_for_doctor"):
        heading("Questions to ask your doctor")
        bullets(result["questions_for_doctor"])
    if result.get("references_used"):
        heading("Compared with verified cases")
        bullets(
            f"{'Normal' if c.get('label') == 'normal' else 'Abnormal'} case"
            f"{': ' + c['diagnosis'] if c.get('diagnosis') else ''} ({c.get('source', '')})"
            for c in result["references_used"]
        )
    if result.get("safety_notes"):
        heading("Limitations")
        bullets(result["safety_notes"])

    pdf.ln(4)
    pdf.set_fill_color(*AMBER_BG)
    pdf.set_text_color(*AMBER_INK)
    pdf.set_font("Helvetica", "B", 11)
    pdf.multi_cell(width, 9, _t(result.get("ai_notice") or "MedPeek is AI and can make mistakes."), fill=True,
                   align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)
    para(result.get("disclaimer", ""), size=8, color=GRAY)
    para(privacy_note or result.get("privacy_note", ""), size=8, color=GRAY)
    para("The verification code shows this document came from MedPeek unchanged. It is not a medical certification "
         "and this report has not been reviewed by a clinician.", size=8, color=GRAY)
    return bytes(pdf.output())
