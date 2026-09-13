"""Inventory actual signed macOS runtime files; no host fallbacks count as bundled."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def write_manifests(bundle: Path) -> None:
    root = bundle / "work/tool-runtime"
    definitions = [
        ("python", "native/python/bin/python3.12", "3.12.14"),
        ("node", "native/node/bin/node", ""),
        ("npm", "native/node/bin/npm", ""),
        ("npx", "native/node/bin/npx", ""),
        ("openclaw", "native/node/openclaw", "2026.7.1-2"),
        ("lilypond", "native/lilypond/lilypond-2.26.0/bin/lilypond", "2.26.0"),
        ("java", "native/audiveris/runtime/bin/java", ""),
        ("jq", "native/cli/bin/jq", "1.8.2"),
        ("rg", "native/cli/bin/rg", "15.2.0"),
        ("gh", "native/cli/bin/gh", "2.97.0"),
        ("git", "native/git/bin/git", "2.53.0"),
        ("git-lfs", "native/git/libexec/git-core/git-lfs", "3.7.1"),
        ("git-credential-manager", "native/git/libexec/git-core/git-credential-manager", "2.9.0"),
    ]
    tools = []
    for name, relative, version in definitions:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root.resolve())
        if not path.is_file():
            raise ValueError(f"Missing bundled command: {name}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        tools.append({"id": name, "executable": relative, "version": version, "sha256": digest})
    components = {t["id"]: {"executable": "work/tool-runtime/" + t["executable"], "version": t["version"]}
                  for t in tools if t["id"] in ("python", "node", "openclaw")}
    manifest = {
        "schema": 1, "bundle": "elren-tool-runtime", "product_version": "1.0",
        "platform": "darwin-arm64",
        "bin_directories": ["native/python/bin", "native/node/bin", "native/node", "native/cli/bin", "native/git/bin"],
        "tools": tools,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (bundle / "work/runtime-bundle.json").write_text(json.dumps({
        "schema": 1, "bundle": "elren-portable-runtime",
        "isolation": "package-first-with-host-native-fallback", "components": components,
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    write_manifests(parser.parse_args().bundle)
