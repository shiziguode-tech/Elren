"""Harmless, bounded child for the real Windows Job cancellation regression."""
import json
import os
import sys
import time
from pathlib import Path

root = Path(sys.argv[1]).resolve()
assert root.is_dir() and root.name in {"owned", "neighbor"}
(root / "ready.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
deadline = time.monotonic() + 12
while not (root / "release").exists() and time.monotonic() < deadline:
    time.sleep(0.02)
if (root / "release").exists():
    (root / "after-release.txt").write_text("synthetic child output", encoding="utf-8")
