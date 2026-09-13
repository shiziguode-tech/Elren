"""An owned disk commit followed by non-awaiting owner-loop publication.

Callers must serialize conflicting mutations and pass detached write inputs.
This boundary is not a write queue and does not make mutable arguments safe.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from deepdesk.plugins.base import finish_owned_work, wait_owned_result

logger = logging.getLogger(__name__)
Result = TypeVar("Result")


async def run_owned_commit(
    write: Callable[..., Result],
    *arguments: Any,
    publish: Callable[[Result], None],
) -> Result:
    """Do not abandon a committed write without publishing its memory outcome.

    ``publish`` runs synchronously on the calling event loop, only after ``write``
    succeeds. It must be bounded and non-blocking. Once writing starts,
    cancellation waits for the transaction and publication, then propagates;
    it never reports a successful cancelled request. A failed write never
    invokes publication. Errors observed while stopping are logged by type only.
    """
    worker = asyncio.create_task(asyncio.to_thread(write, *arguments))
    try:
        result = await wait_owned_result(worker)
    except asyncio.CancelledError:
        try:
            result = await finish_owned_work(worker)
        except Exception as error:
            logger.warning("Persistence write failed while stopping (%s)", type(error).__name__)
        else:
            try:
                publish(result)
            except Exception as error:
                logger.error("Committed persistence publication failed while stopping (%s)",
                             type(error).__name__)
        raise
    publish(result)
    return result
