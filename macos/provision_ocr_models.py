"""Explicit build-time model provisioning; never called at application startup."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from macos.check_ocr_models import MODEL_SHA256, model_hashes

BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/"
MODEL_URLS = {
    "PP-OCRv6_det_small.onnx": BASE + "PP-OCRv6/det/PP-OCRv6_det_small.onnx",
    "PP-OCRv6_rec_small.onnx": BASE + "PP-OCRv6/rec/PP-OCRv6_rec_small.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx": BASE + "PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
}


class HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise ValueError("Refusing an insecure model redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_model(url: str):
    return urllib.request.build_opener(HTTPSOnlyRedirect()).open(url, timeout=60)


def download(cache: Path) -> None:
    models = cache / "models"
    models.mkdir(parents=True, exist_ok=True)
    for name, url in MODEL_URLS.items():
        target = models / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == MODEL_SHA256[name]:
            continue
        with tempfile.NamedTemporaryFile(dir=models, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            digest = hashlib.sha256()
            total = 0
            with open_model(url) as response, temporary.open("wb") as output:
                if not response.url.startswith("https://"):
                    raise ValueError("Refusing an insecure model redirect")
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 512 * 1024 * 1024:
                        raise ValueError("Model exceeds download size limit")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != MODEL_SHA256[name]:
                raise ValueError(f"Model checksum mismatch: {name}")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    model_hashes(cache)


def install(cache: Path, package: Path) -> None:
    # Validate the complete cache before writing anything into a build environment.
    model_hashes(cache)
    (package / "models").mkdir(parents=True, exist_ok=True)
    for name in MODEL_SHA256:
        target = package / "models" / name
        if target.is_symlink() or not target.resolve().is_relative_to(package.resolve()):
            raise ValueError("Refusing a redirected model destination")
        shutil.copyfile(cache / "models" / name, target)
    model_hashes(package)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--download-to", type=Path)
    group.add_argument("--install-from", type=Path)
    args = parser.parse_args()
    if args.download_to:
        download(args.download_to)
    else:
        spec = importlib.util.find_spec("rapidocr")
        if spec is None or not spec.origin:
            parser.error("Install the locked RapidOCR package first")
        install(args.install_from, Path(spec.origin).parent)


if __name__ == "__main__":
    main()
