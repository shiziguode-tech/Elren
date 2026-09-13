"""Build the small bundled helper; never needed when running OCR.

Usage: python scripts/build_audiveris_bootstrap.py --javac /path/to/javac
Review the printed hashes and update pinned values only after real-JVM tests.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--javac", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "deepdesk/java/ElrenAudiverisBootstrap.java"
    jar = source.with_name("audiveris-bootstrap.jar")
    with tempfile.TemporaryDirectory(prefix="elren-bootstrap-build-") as temporary:
        classes = Path(temporary)
        subprocess.run([args.javac, "--release", "17", "-encoding", "UTF-8", "-g:none",
                        "-d", str(classes), str(source)], check=True, timeout=30)
        with zipfile.ZipFile(jar, "w", compression=zipfile.ZIP_STORED) as output:
            for file in sorted(classes.rglob("*.class")):
                info = zipfile.ZipInfo(file.relative_to(classes).as_posix(), (1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                output.writestr(info, file.read_bytes())
    # Normalize checkout line endings, not Java source content.
    print("source_sha256=" + hashlib.sha256(source.read_text(encoding="utf-8").encode()).hexdigest())
    print("jar_sha256=" + hashlib.sha256(jar.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
