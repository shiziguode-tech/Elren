"""Verify metadata OCR models without importing an OCR engine or downloading."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
from pathlib import Path

# Reviewed RapidOCR 3.9.2 default_models.yaml digests. Existence alone is not
# enough: its downloader also fetches replacements for mismatched model bytes.
MODEL_SHA256 = {
    "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
}
MODELS = tuple(MODEL_SHA256)


def model_hashes(package: Path) -> dict[str, str]:
    package = package.resolve(strict=True)
    result = {}
    for name in MODELS:
        path = package / "models" / name
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f"Required offline RapidOCR model is missing: {name}")
        if not path.resolve(strict=True).is_relative_to(package):
            raise ValueError(f"RapidOCR model path escapes its package: {name}")
        with path.open("rb") as stream:
            result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        if result[name] != MODEL_SHA256[name]:
            raise ValueError(f"Offline RapidOCR model failed reviewed SHA-256 verification: {name}")
    return result


def verify_frozen_models(package: Path, backend: Path) -> None:
    original = model_hashes(package)
    frozen = model_hashes(backend / "_internal/rapidocr")
    if original != frozen:
        raise ValueError("Frozen RapidOCR models differ from the checked build dependency")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path)
    args = parser.parse_args()
    try:
        spec = importlib.util.find_spec("rapidocr")
        if spec is None or not spec.origin:
            raise ValueError("The locked RapidOCR package is not installed")
        package = Path(spec.origin).parent
        if args.backend:
            verify_frozen_models(package, args.backend)
        else:
            model_hashes(package)
    except (ValueError, OSError) as error:
        parser.exit(2, f"Offline score metadata OCR is incomplete: {error}. "
                    "Provision the reviewed models in rapidocr/models before building; "
                    "this check does not download models.\n")


if __name__ == "__main__":
    main()
