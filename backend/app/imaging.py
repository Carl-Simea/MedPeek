"""Turn an uploaded file (JPG, PNG, WEBP or DICOM) into a clean PNG for Gemma 4.

Re-encoding drops EXIF and DICOM header data, so names or IDs embedded in the
file are never passed to the model or saved.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 80_000_000  # refuse decompression bombs


class ImageError(ValueError):
    """The upload is not an image we can read."""


@dataclass
class PreparedImage:
    png_bytes: bytes
    width: int
    height: int
    source_format: str

    @property
    def b64(self) -> str:
        return base64.b64encode(self.png_bytes).decode("ascii")


def _is_dicom(data: bytes, filename: str) -> bool:
    return (len(data) > 132 and data[128:132] == b"DICM") or filename.lower().endswith((".dcm", ".dicom"))


def _dicom_to_image(data: bytes) -> Image.Image:
    try:
        import numpy as np
        import pydicom
    except ImportError as exc:  # pragma: no cover
        raise ImageError("DICOM support is not installed on the server.") from exc

    try:
        ds = pydicom.dcmread(io.BytesIO(data), force=True)
        arr = ds.pixel_array
    except Exception as exc:
        raise ImageError(
            "Could not read this DICOM file (it may use a compression we don't support). "
            "Please export the image as PNG or JPG and upload that."
        ) from exc

    try:  # apply the scanner's display window when present
        try:
            from pydicom.pixels import apply_voi_lut
        except ImportError:  # older pydicom
            from pydicom.pixel_data_handlers.util import apply_voi_lut
        arr = apply_voi_lut(arr, ds)
    except Exception:
        pass

    arr = np.asarray(arr)
    if arr.ndim == 4:  # frames x rows x cols x channels
        arr = arr[arr.shape[0] // 2]
    elif arr.ndim == 3 and arr.shape[-1] not in (3, 4):  # multi-frame grayscale: take middle slice
        arr = arr[arr.shape[0] // 2]

    arr = arr.astype("float64")
    lo, hi = float(np.percentile(arr, 0.5)), float(np.percentile(arr, 99.5))
    if hi <= lo:
        lo, hi = float(arr.min()), float(arr.max()) or 1.0
    arr = np.clip((arr - lo) / (hi - lo), 0, 1) * 255.0
    if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
        arr = 255.0 - arr
    arr = arr.astype("uint8")
    return Image.fromarray(arr)


def _high_bit_depth_to_l(img: Image.Image) -> Image.Image:
    import numpy as np

    arr = np.asarray(img, dtype="float64")
    lo, hi = float(np.percentile(arr, 0.5)), float(np.percentile(arr, 99.5))
    if hi <= lo:
        lo, hi = float(arr.min()), float(arr.max()) or 1.0
    arr = np.clip((arr - lo) / (hi - lo), 0, 1) * 255.0
    return Image.fromarray(arr.astype("uint8"), mode="L")


def thumbnail_vector(png_bytes: bytes, size: int = 32):
    """A small, normalized picture fingerprint used to find visually similar reference cases."""
    import numpy as np

    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    img = ImageOps.equalize(img.resize((size, size), Image.BILINEAR))
    v = np.asarray(img, dtype="float32").ravel()
    v -= v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n else v


def prepare_image(data: bytes, filename: str = "", max_side: int = 1024) -> PreparedImage:
    if not data:
        raise ImageError("The uploaded file is empty.")

    if _is_dicom(data, filename):
        img = _dicom_to_image(data)
        source_format = "DICOM"
    else:
        try:
            img = Image.open(io.BytesIO(data))
            if img.format == "JPEG" and max(img.size) > max_side * 2:
                # decode big JPEGs at a reduced scale: much faster, and still larger than we keep
                w, h = img.size
                img.draft(img.mode, (max(1, w * max_side // max(w, h)), max(1, h * max_side // max(w, h))))
            img.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise ImageError("This file isn't a supported image. Upload a JPG, PNG, WEBP or DICOM (.dcm) file.") from exc
        source_format = img.format or "image"
        img = ImageOps.exif_transpose(img)

    if img.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        img = _high_bit_depth_to_l(img)  # 12/16-bit X-rays would otherwise turn white
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB") if img.mode in ("RGBA", "P", "CMYK", "LA") else img.convert("L")
    if img.width < 64 or img.height < 64:
        raise ImageError("The image is too small to read. Please upload a larger, clearer picture.")

    img.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)  # fresh PNG, no metadata carried over
    return PreparedImage(png_bytes=out.getvalue(), width=img.width, height=img.height, source_format=source_format)
