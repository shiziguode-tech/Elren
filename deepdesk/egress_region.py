from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EgressRegion:
    """Transient country classification for the current task's public egress.

    The public IP returned by a probe is deliberately never stored on this
    object, returned to callers, or written to logs.
    """

    country_code: str = "UNKNOWN"

    @property
    def is_mainland_china(self) -> bool:
        return self.country_code == "CN"


class EgressRegionDetector:
    """Probe the public egress afresh for every task execution."""

    _country_pattern = re.compile(r"(?m)^loc=([A-Z]{2})$")
    _china_names = {"中国", "中国大陆", "中华人民共和国"}

    async def detect(self) -> EgressRegion:
        # Sequential probes avoid disclosing the public egress to more than one
        # service when the primary succeeds. trust_env=True deliberately follows
        # the machine's current VPN/proxy route, which is the egress the user means.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.5, connect=2.5),
            follow_redirects=True,
            trust_env=True,
            headers={"User-Agent": "Elren/1.0"},
        ) as client:
            try:
                response = await client.get("https://www.cloudflare.com/cdn-cgi/trace")
                response.raise_for_status()
                match = self._country_pattern.search(response.text)
                if match:
                    return EgressRegion(match.group(1))
            except Exception:
                logger.debug("Primary egress-region probe failed", exc_info=True)

            try:
                response = await client.get("https://myip.ipip.net/json")
                response.raise_for_status()
                payload = json.loads(response.content.decode("utf-8"))
                location = ((payload.get("data") or {}).get("location") or [])
                country = str(location[0]).strip() if location else ""
                if country in self._china_names:
                    return EgressRegion("CN")
                if country:
                    # The fallback is needed only to distinguish mainland China.
                    return EgressRegion("NON_CN")
            except Exception:
                logger.debug("Fallback egress-region probe failed", exc_info=True)
        return EgressRegion()
