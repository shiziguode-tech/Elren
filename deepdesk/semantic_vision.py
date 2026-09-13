"""Bounded image requests shared by the privacy-aware OCR fallback chain."""
from __future__ import annotations

import asyncio
import base64

import httpx

from deepdesk.vision_runtime import DEEPSEEK_VISION_MODEL, VisionEndpoint


class VisionRequestError(RuntimeError):
    def __init__(self, message: str, attempts: list[int | str]):
        super().__init__(message)
        self.attempts = attempts


async def request_semantic_vision(
    endpoint: VisionEndpoint, raw: bytes, mime: str, prompt: str,
) -> tuple[str, list[int | str]]:
    encoded = base64.b64encode(raw).decode("ascii")
    headers = {"Content-Type": "application/json"}
    if endpoint.protocol == "gemini":
        if endpoint.api_key:
            headers["x-goog-api-key"] = endpoint.api_key
        model = endpoint.model.removeprefix("models/")
        url = f"{endpoint.base_url}/v1beta/models/{model}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [
                {"text": prompt}, {"inline_data": {"mime_type": mime, "data": encoded}},
            ]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 2000},
        }
    else:
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        url = f"{endpoint.base_url}/chat/completions"
        payload = {"model": endpoint.model, "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
        ]}], "temperature": 0, "max_tokens": 2000}
        if endpoint.model == DEEPSEEK_VISION_MODEL:
            # OCR does not need unbounded reasoning before its actual text.
            payload["thinking"] = {"type": "disabled"}
    is_official_deepseek = endpoint.source.startswith("deepseek-official-")
    local = endpoint.base_url.startswith("http://127.0.0.1:")
    attempts: list[int | str] = []
    limit = 1 if is_official_deepseek else 3
    async with httpx.AsyncClient(
        timeout=45 if is_official_deepseek else 120,
        trust_env=not (local or is_official_deepseek), follow_redirects=False,
    ) as client:
        for attempt in range(limit):
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.RequestError as exc:
                attempts.append(type(exc).__name__)
                if attempt + 1 < limit:
                    await asyncio.sleep(1.5 * (2 ** attempt))
                    continue
                raise VisionRequestError("Transport unavailable", attempts) from None
            attempts.append(response.status_code)
            if response.status_code in {429, 500, 502, 503, 504, 524} and attempt + 1 < limit:
                try:
                    delay = float(response.headers.get("retry-after", ""))
                except ValueError:
                    delay = 1.5 * (2 ** attempt)
                await asyncio.sleep(max(0.5, min(delay, 30.0)))
                continue
            if not 200 <= response.status_code < 300:
                # Never expose response bodies, echoed credentials or headers.
                raise VisionRequestError(f"HTTP {response.status_code}", attempts)
            try:
                data = response.json()
                if endpoint.protocol == "gemini":
                    candidate = (data.get("candidates") or [{}])[0]
                    if candidate.get("finishReason") not in {None, "STOP"}:
                        raise ValueError("Incomplete or blocked response")
                    description = "\n".join(
                        part["text"] for part in (candidate.get("content") or {}).get("parts", [])
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                        and not part.get("thought")
                    ).strip()
                else:
                    choice = data["choices"][0]
                    message = choice["message"]
                    if choice.get("finish_reason") not in {None, "stop"} or message.get("refusal"):
                        raise ValueError("Incomplete or refused response")
                    description = message.get("content")
                    if not isinstance(description, str):
                        raise ValueError("Invalid text response")
                    description = description.strip()
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                raise VisionRequestError("Invalid or incomplete vision response", attempts) from None
            if not description or description.upper() == "NO_READABLE_TEXT":
                raise VisionRequestError("No usable recognition result", attempts)
            return description, attempts
    raise VisionRequestError("No response", attempts)
