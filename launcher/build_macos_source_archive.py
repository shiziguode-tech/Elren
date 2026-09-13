from __future__ import annotations

import argparse
import os
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deepdesk.audiveris_tessdata import LOCK_PATH, RELATIVE_DATA, validate_directory
from launcher.build_release_archive import (
    PUBLIC_ALLOWED_ROOT_ENTRIES,
    PUBLIC_ENV_TEMPLATE_NAME,
    excluded,
    is_filesystem_link,
    reproducible_zip_info,
    should_validate_first_party_text,
    validate_public_env_template,
    validate_release_text_portability,
    write_reproducible_bytes,
    write_reproducible_file,
)

SKIP_ROOTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "data",
    "dist",
    "outputs",
    "screenshots",
    "work",
    ".venv",
    "memory",
}
SKIP_DIRECTORY_NAMES = {
    ".gradle",
    ".kotlin",
    ".pnpm",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
}
SKIP_FILE_NAMES = {
    ".env",
    "heartbeat.md",
    "identity.md",
    "local.properties",
    "memory.md",
    "openclaw-workspace-state.json",
    "soul.md",
    "tools.md",
    "user.md",
}
SKIP_WINDOWS_FILE_NAMES = {
    "elren.exe",
    "microsoft.web.webview2.core.dll",
    "microsoft.web.webview2.winforms.dll",
    "start.ps1",
    "webview2loader.dll",
}
EXECUTABLE_SUFFIXES = {".command", ".sh"}
ARCHIVE_ROOT = "Elren-v1.0-macOS-Build-Source"


def iter_macos_source_paths(source: Path) -> Iterator[tuple[Path, Path]]:
    """Yield allowed source entries without descending into excluded trees."""

    def entry_excluded(relative: Path) -> bool:
        return (
            excluded(relative)
            or relative.parts[0].casefold() not in PUBLIC_ALLOWED_ROOT_ENTRIES
            or relative.parts[0].casefold() in SKIP_ROOTS
            or relative.name.casefold() in SKIP_WINDOWS_FILE_NAMES
            or any(part.casefold() in SKIP_DIRECTORY_NAMES for part in relative.parts)
            or relative.name.casefold() in SKIP_FILE_NAMES
        )

    def walk(
        directory: Path,
        relative_directory: Path,
        active_directories: frozenset[Path],
    ) -> Iterator[tuple[Path, Path]]:
        canonical_directory = directory.resolve(strict=True)
        if canonical_directory != directory:
            raise ValueError(
                "macOS source traversal encountered a filesystem redirect: "
                f"{relative_directory}"
            )
        if canonical_directory in active_directories:
            raise ValueError(
                f"macOS source traversal encountered a cycle: {relative_directory}"
            )
        branch_directories = active_directories | {canonical_directory}
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda item: (item.name.casefold(), item.name))
        for entry in entries:
            path = Path(entry.path)
            relative = relative_directory / entry.name
            if entry_excluded(relative):
                # Excluded roots and directories are terminal: never enumerate
                # caches, QA workspaces, build trees, or unrelated repo data.
                continue
            if is_filesystem_link(path):
                raise ValueError(
                    "macOS source archives do not permit symbolic links or "
                    f"junctions: {relative}"
                )
            yield path, relative
            if entry.is_dir(follow_symlinks=False):
                yield from walk(path, relative, branch_directories)

    yield from walk(source, Path(), frozenset())
    # Only this reviewed runtime source belongs in the Mac source archive.
    # Do not enumerate work/: it also contains private/Windows runtime state.
    if (source / "deepdesk/mcp_runtime.py").is_file():
        relative = Path("work/openclaw-runtime/mcp-servers/elren-workspace.mjs")
        cursor = source
        for part in relative.parts:
            cursor /= part
            if is_filesystem_link(cursor):
                raise ValueError("macOS MCP source contains a filesystem redirect")
        if not cursor.is_file():
            raise ValueError("Missing bundled MCP server source for macOS archive")
        yield cursor, relative
    # Music-enabled source bundles need platform-neutral OCR data offline.
    # Never walk the Windows runtime tree: collect exactly the reviewed files.
    if (source / "deepdesk/audiveris_tessdata.py").is_file():
        data = source / RELATIVE_DATA
        cursor = source
        for part in RELATIVE_DATA.parts:
            cursor /= part
            if not cursor.exists():
                raise ValueError("Missing offline tessdata source for macOS archive")
            if is_filesystem_link(cursor):
                raise ValueError("macOS tessdata source contains a filesystem redirect")
        validate_directory(data)
        for name in ("eng.traineddata", "chi_sim.traineddata", "chi_tra.traineddata", "LICENSE", LOCK_PATH.name):
            yield data / name, RELATIVE_DATA / name


def build(source: Path, destination: Path) -> tuple[int, int]:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    if destination == source or source in destination.parents:
        raise ValueError("The macOS archive destination must be outside the source tree")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=destination.stem + "-", suffix=".building", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    count = 0
    total = 0
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr(
                reproducible_zip_info(f"{ARCHIVE_ROOT}/", is_dir=True), b""
            )
            for path, relative in iter_macos_source_paths(source):
                archive_name = (Path(ARCHIVE_ROOT) / relative).as_posix()
                if path.is_dir():
                    archive.writestr(
                        reproducible_zip_info(archive_name, is_dir=True), b""
                    )
                    continue
                first_party_contents: str | None = None
                if should_validate_first_party_text(relative):
                    first_party_contents = path.read_text(encoding="utf-8")
                    validate_release_text_portability(relative, first_party_contents)
                if relative.name.casefold() == PUBLIC_ENV_TEMPLATE_NAME:
                    data = (
                        first_party_contents.encode("utf-8")
                        if first_party_contents is not None
                        else path.read_bytes()
                    )
                    validate_public_env_template(data.decode("utf-8"))
                    write_reproducible_bytes(archive, archive_name, data)
                else:
                    write_reproducible_file(
                        archive,
                        path,
                        archive_name,
                        executable=path.suffix.lower() in EXECUTABLE_SUFFIXES,
                    )
                count += 1
                total += path.stat().st_size
        with zipfile.ZipFile(temporary) as archive:
            broken = archive.testzip()
            if broken:
                raise RuntimeError(f"CRC verification failed for {broken}")
        os.replace(temporary, destination)
        return count, total
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a macOS source bundle on Windows")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    count, total = build(args.source, args.destination)
    print(f"archive={args.destination.resolve()}")
    print(f"files={count}")
    print(f"source_bytes={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
