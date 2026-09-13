from __future__ import annotations

import os
import re
from collections.abc import Mapping

from deepdesk.tool_runtime import prepend_unique_path, tool_runtime_path_entries

_SENSITIVE_NAME = re.compile(
    r"(?i)(key|token|secret|password|passwd|credential|authorization|cookie|api[_-]?key)"
)


def credential_safe_environment(
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment without unrelated parent credentials.

    The service may hold credentials for several providers at once.  Helper
    processes receive ordinary runtime variables plus only the credentials a
    caller deliberately supplies through ``extra``.
    """

    environment = {
        key: value
        for key, value in os.environ.items()
        if not _SENSITIVE_NAME.search(key)
    }
    if extra:
        environment.update({key: str(value) for key, value in extra.items()})
    # Package-local command dependencies behave like a private container
    # filesystem without requiring Docker Desktop.  They precede the computer
    # PATH, while host-native applications remain available as a fallback.
    prepend_unique_path(environment, tool_runtime_path_entries())
    return environment
