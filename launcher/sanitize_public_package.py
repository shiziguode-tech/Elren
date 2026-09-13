"""Remove machine-local state from a synchronized public release folder."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launcher.build_release_archive import (
    OPENCLAW_PNPM_STORE_PREFIX,
    PNPM_MODULES_STATE_NAME,
    PNPM_WORKSPACE_STATE_NAME,
    PUBLIC_ALLOWED_ROOT_ENTRIES,
    PUBLIC_ENV_TEMPLATE_NAME,
    is_excluded_directory_name,
    is_filesystem_link,
    is_public_machine_local_venv_file,
    portable_pnpm_modules_state,
    portable_python_script,
    portable_venv_config,
    should_validate_first_party_text,
    validate_public_env_template,
    validate_release_text_portability,
)

VOLATILE_ROOTS = ("data", "outputs", "screenshots")
OBSOLETE_ROOTS = ("dist",)
CACHE_DIRECTORY_NAMES = {
    ".gradle",
    ".ignored",
    ".ignored_openclaw",
    ".kotlin",
    ".links",
    ".pnpm",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
PRIVATE_FILE_NAMES = {
    ".env",
    "deepdesk.db",
    "feishu-listener-token",
    "gateway.token",
    "local.properties",
    "openclaw.json",
    "provider-keys.json",
    "provider-secrets.vault",
    "runtime-settings.json",
    "skills-check.json",
    "_editable_impl_deepdesk_agent.pth",
    "_editable_impl_elren_agent.pth",
    "_editable_impl_milo_agent.pth",
    "_editable_impl_qelvane_desk_agent.pth",
}
ALLOWED_WORK_ENTRIES = {
    "browser-runtime",
    "node-runtime",
    "openclaw-runtime",
    "python-runtime",
    "runtime-bundle.json",
    "tool-runtime",
}
GENERATED_ANDROID_DIRECTORIES = (
    Path("mobile/ElrenMobile/build"),
    Path("mobile/ElrenMobile/app/build"),
)
VOLATILE_WORK_DIRECTORIES = (Path("work/sandbox"), Path("work/studio"))


def sanitize(package: Path) -> list[Path]:
    package = package.resolve(strict=True)
    if not package.is_dir() or package.name.casefold() != "package-v1.0-public":
        raise ValueError("Refusing to sanitize anything except package-v1.0-public")

    removed: list[Path] = []

    # Sanitizing a materialized folder must preserve the same package boundary
    # as the ZIP builders.  Leaving a link in an otherwise clean public folder
    # is unsafe because Explorer or a third-party archiver may later follow it
    # and copy data from outside the release tree.
    for path in package.rglob("*"):
        if is_filesystem_link(path):
            raise ValueError(
                "Public release folders do not permit symbolic links or junctions: "
                f"{path.relative_to(package)}"
            )

    def discard(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_dir() and not path.is_symlink():
            def make_writable_and_retry(function, blocked_path, _error) -> None:
                os.chmod(blocked_path, stat.S_IWRITE)
                function(blocked_path)

            shutil.rmtree(path, onerror=make_writable_and_retry)
        else:
            path.unlink()
        removed.append(path.relative_to(package))

    for name in VOLATILE_ROOTS:
        discard(package / name)
        (package / name).mkdir()
    for name in OBSOLETE_ROOTS:
        discard(package / name)
    allowed_roots = PUBLIC_ALLOWED_ROOT_ENTRIES | set(VOLATILE_ROOTS)
    for child in list(package.iterdir()):
        if child.name.casefold() not in allowed_roots:
            discard(child)
    discard(package / ".env")
    for relative in GENERATED_ANDROID_DIRECTORIES:
        discard(package / relative)
    for relative in VOLATILE_WORK_DIRECTORIES:
        discard(package / relative)
    work_root = package / "work"
    if work_root.is_dir():
        for child in list(work_root.iterdir()):
            if child.name.casefold() not in ALLOWED_WORK_ENTRIES:
                discard(child)
    for metadata_name in (
        "elren_agent-1.0.0.dist-info",
        "milo_agent-1.0.0.dist-info",
        "qelvane_desk_agent-1.0.0.dist-info",
        "deepdesk_agent-0.1.0.dist-info",
    ):
        discard(package / ".venv/Lib/site-packages" / metadata_name)
    for path in list((package / ".venv").rglob("*")) if (package / ".venv").is_dir() else []:
        lowered = tuple(part.casefold() for part in path.relative_to(package).parts)
        if path.is_file() and is_public_machine_local_venv_file(lowered):
            discard(path)
    venv_config = package / ".venv/pyvenv.cfg"
    if venv_config.is_file():
        venv_config.write_text(
            portable_venv_config(venv_config.read_text(encoding="utf-8")),
            encoding="utf-8",
            newline="\n",
        )
    scripts = package / ".venv/Scripts"
    if scripts.is_dir():
        for script in scripts.iterdir():
            if not script.is_file() or script.suffix.casefold() not in {"", ".py"}:
                continue
            contents = script.read_text(encoding="utf-8")
            portable = portable_python_script(contents)
            if portable != contents:
                script.write_text(portable, encoding="utf-8", newline="")

    openclaw_modules = package / "work/openclaw-runtime/node_modules"
    # ZIP releases materialize verified pnpm links and omit this virtual store.
    # Keep synchronized public folders on the same boundary.
    discard(package.joinpath(*OPENCLAW_PNPM_STORE_PREFIX))
    discard(openclaw_modules / PNPM_WORKSPACE_STATE_NAME)
    if openclaw_modules.is_dir():
        for bin_directory in list(openclaw_modules.rglob(".bin")):
            if bin_directory.is_dir():
                discard(bin_directory)
    modules_state = openclaw_modules / PNPM_MODULES_STATE_NAME
    if modules_state.is_file():
        modules_state.write_text(
            portable_pnpm_modules_state(modules_state.read_text(encoding="utf-8")),
            encoding="utf-8",
            newline="",
        )

    # Remove generated caches before the first-party UTF-8/portability scan.
    # Cache payloads can be binary even when their filenames have no suffix;
    # treating those implementation files as product text previously made a
    # clean public-folder sync fail with an unhelpful UnicodeDecodeError.
    directories = sorted(
        (path for path in package.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in directories:
        if path.name.casefold() in CACHE_DIRECTORY_NAMES or is_excluded_directory_name(path.name):
            discard(path)

    env_template = package / PUBLIC_ENV_TEMPLATE_NAME
    if env_template.is_file():
        validate_public_env_template(env_template.read_text(encoding="utf-8"))

    for path in package.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(package)
        if should_validate_first_party_text(relative):
            validate_release_text_portability(
                relative,
                path.read_text(encoding="utf-8"),
            )

    for path in list(package.rglob("*")):
        if path.is_file() and (
            (
                path.name.casefold().startswith(".env")
                and path.name.casefold() != PUBLIC_ENV_TEMPLATE_NAME
            )
            or path.name.casefold() in PRIVATE_FILE_NAMES
            or path.suffix.casefold() in {".log", ".pdb", ".pyc"}
        ):
            discard(path)
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Sanitize a Elren public release folder")
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    removed = sanitize(args.package)
    print(f"sanitized={args.package.resolve()} removed={len(removed)}")


if __name__ == "__main__":
    main()
