"""Publish complete attachments without assuming an NTFS-only filesystem."""
from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _write_upload(target: Path, payload: bytes | bytearray) -> None:
    # Follow the vault/filesystem tools' sibling-replacement pattern, but do not
    # require the Windows vault's NTFS DACL operation for ordinary attachments:
    # portable workspaces may live on exFAT. Inherit the upload directory ACL.
    descriptor, name = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, target)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def save_upload(target: Path, payload: bytes | bytearray) -> None:
    # The caller supplies a fresh UUID directory. Never clean another request's
    # directory if its exclusive creation failed.
    target.parent.mkdir(parents=True, exist_ok=False)
    try:
        _write_upload(target, payload)
    except BaseException:
        # A permissions failure may occur after replacement. This request must
        # still fail without advertising an incomplete/unprotected attachment.
        # Only remove the exact file owned by this upload, never recursively.
        try:
            target.unlink(missing_ok=True)
            target.parent.rmdir()
        except OSError:
            logger.warning("Failed upload cleanup is pending (%s)", target.parent.name)
        raise
