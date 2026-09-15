"""Task-owned browser downloads; never search a user's Downloads directory."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from uuid import uuid4


class BrowserDownloads:
    def __init__(self, workspace: str, task_id: str) -> None:
        self.root = Path(workspace).resolve()
        self.directory = self.root / 'outputs' / 'browser-downloads' / hashlib.sha256(task_id.encode()).hexdigest()[:20]
        self.pending: list = []
        self.records: list[dict] = []
        self.rejected = 0
        self.max_bytes = 32 * 1024 * 1024
        self.timeout = 20

    def receive(self, download) -> None:
        # A synchronous listener owns no detached input/network worker. The
        # execute/cleanup coroutine awaits persistence or cancellation itself.
        self.pending.append(download)

    @staticmethod
    async def _cancel(download) -> None:
        try:
            async with asyncio.timeout(2):
                await download.cancel()
        except Exception:
            # Closing the owning browser context remains the final cleanup.
            logging.getLogger(__name__).debug('Download cancellation deferred to browser context cleanup')

    async def collect(self) -> list[dict]:
        batch, self.pending = self.pending, []
        for download in batch:
            record = {'status': 'failed'}
            partial = None
            try:
                if len(self.records) >= 16:
                    self.rejected += 1
                    await self._cancel(download)
                    continue
                name = re.sub(r'[^\w. -]', '_', str(download.suggested_filename))[-100:].strip(' .') or 'download.bin'
                # A fresh prefix prevents collision/reserved filename attacks.
                target = self.directory / (uuid4().hex + '-' + name)
                if not target.resolve().is_relative_to(self.root):
                    raise PermissionError('Download directory escapes workspace')
                self.directory.mkdir(parents=True, exist_ok=True)
                partial = target.with_name(target.name + '.partial')
                async with asyncio.timeout(self.timeout):
                    temporary = await download.path()
                    if temporary is None:
                        raise OSError('Download did not produce a file')
                    if Path(temporary).stat().st_size > self.max_bytes:
                        raise ValueError('Download exceeds 32 MiB limit')
                    await download.save_as(str(partial))
                partial.replace(target)
                record = {
                    'status': 'saved', 'path': str(target), 'filename': name,
                    'bytes': target.stat().st_size,
                    'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            except asyncio.CancelledError:
                await self._cancel(download)
                # The rest of this batch must not survive cancellation either.
                for remaining in batch[batch.index(download) + 1:]:
                    await self._cancel(remaining)
                raise
            except Exception as exc:
                await self._cancel(download)
                record['error'] = type(exc).__name__
                record['message'] = 'Download was not saved; do not search personal directories or repeat submission.'
            finally:
                if partial is not None and partial.resolve().is_relative_to(self.root):
                    partial.unlink(missing_ok=True)
            self.records.append(record)
        records = list(self.records[-16:])
        if self.rejected:
            records.append({'status': 'rejected', 'error': 'DownloadLimitExceeded',
                            'count': self.rejected, 'message': 'At most 16 downloads per task session.'})
        return records

    async def close(self) -> None:
        pending, self.pending = self.pending, []
        for download in pending:
            await self._cancel(download)
