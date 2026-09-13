"""Explicit local project selection; never infer a directory from model text."""
from __future__ import annotations

import ipaddress
import stat
from pathlib import Path


def normalize_project_path(value: str | None) -> str:
    if value is None or not value.strip():
        return ""
    raw = value.strip()
    path = Path(raw)
    if raw.startswith(("\\\\", "//")):
        raise ValueError("Select a local project directory, not a network or device path")
    if "\x00" in raw or not path.is_absolute() or ".." in path.parts:
        raise ValueError("Project path must be an existing absolute directory without parent traversal")
    if path == Path(path.anchor) or path == Path.home():
        raise ValueError("Select a project folder, not a drive root or home directory")
    try:
        for item in [*reversed(path.parents), path]:
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Project paths cannot contain symbolic links or junctions")
        if not path.is_dir():
            raise ValueError("Project path must be an existing directory")
        return str(path.resolve(strict=True))
    except OSError as exc:
        raise ValueError("Project directory does not exist or cannot be accessed") from exc


def is_loopback_client(request) -> bool:
    try:
        return bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        return False
