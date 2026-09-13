"""Bounded live smoke test for every configured built-in reasoning level.

This is intentionally not collected by pytest. It reads an existing private
provider store supplied on the command line, never prints credentials, limits
each answer, and writes a machine-readable report under outputs/.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from deepdesk.deepseek import DeepSeekClient
from deepdesk.provider_secrets import ProviderSecrets, ProviderSecretsStore

PROMPT = (
    "A box contains red and blue balls. After removing 2 red balls, the ratio "
    "of red to blue is 3:4. Initially there were 14 red balls. How many blue "
    "balls are there? Reply with the integer and one short reason."
)
EXPECTED = "16"


def _reasoning_metric(usage: dict[str, Any]) -> dict[str, int]:
    details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    return {
        "reasoning_tokens": int(details.get("reasoning_tokens") or 0),
        "reasoning_chars": int(usage.get("reasoning_chars") or 0),
    }


async def run_case(
    client: DeepSeekClient,
    selector: str,
    level: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        started = time.perf_counter()
        model_token = client.bind_task_model(selector)
        effort_token = client.bind_task_reasoning_effort(level)
        output_token = client._task_max_output_tokens.set(512)
        try:
            reply = await asyncio.wait_for(
                client._chat([{"role": "user", "content": PROMPT}], [], recovery=False),
                timeout=120,
            )
            text = str(reply.message.get("content") or "").strip()
            usage = dict(reply.usage or {})
            return {
                "selector": selector,
                "level": level,
                "ok": bool(text),
                # This is deliberately only a formatting signal.  A provider
                # may wrap the same correct answer in Markdown, so it must not
                # be confused with endpoint or reasoning-control support.
                "answer_format_match": text.startswith(EXPECTED),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "answer_chars": len(text),
                "answer_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "provider_model": reply.provider_model,
                "finish_reason": reply.finish_reason,
                "usage": usage,
                **_reasoning_metric(usage),
            }
        except Exception as exc:  # live compatibility evidence
            return {
                "selector": selector,
                "level": level,
                "ok": False,
                "answer_format_match": False,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
        finally:
            client._task_max_output_tokens.reset(output_token)
            client.reset_task_reasoning_effort(effort_token)
            client.reset_task_model(model_token)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider_store", type=Path)
    parser.add_argument("--output", type=Path, default=Path("outputs/reasoning-matrix-live.json"))
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--selector-prefix", default="")
    args = parser.parse_args()

    secrets = ProviderSecretsStore(args.provider_store, ProviderSecrets()).value
    client = DeepSeekClient(
        "https://api.deepseek.com/v1",
        "deepseek-v4-flash",
        secrets.deepseek_primary,
        secrets.deepseek_backup,
        timeout=100,
    )
    relay_key = secrets.aicodemirror
    client.set_provider_models(
        list(secrets.model_providers),
        {"openai": relay_key, "anthropic": relay_key, "google": relay_key},
        secrets.aicodemirror_fable,
    )
    selectors = [
        item["selector"] for item in client.model_options()
        if not args.selector_prefix or item["selector"].startswith(args.selector_prefix)
    ]
    semaphore = asyncio.Semaphore(max(1, min(args.concurrency, 6)))
    capability_by_selector = {
        item["selector"]: item.get("reasoning") or {}
        for item in client.model_options()
        if item["selector"] in selectors
    }
    tasks = []
    for selector in selectors:
        capability = capability_by_selector[selector]
        for level in capability.get("levels") or []:
            tasks.append(asyncio.create_task(run_case(client, selector, level, semaphore)))
    results: list[dict[str, Any]] = []
    for completed in asyncio.as_completed(tasks):
        result = await completed
        results.append(result)
        print(
            f"[{len(results):03d}/{len(tasks):03d}] "
            f"{result['selector']} {result['level']}: "
            f"{'ok' if result['ok'] else 'failed'} {result['elapsed_seconds']}s",
            flush=True,
        )

    level_order = ("minimal", "low", "medium", "high", "xhigh", "max")
    ordered = sorted(results, key=lambda item: (selectors.index(item["selector"]), level_order.index(item["level"])))
    report = {
        "prompt": PROMPT,
        "expected": EXPECTED,
        "model_count": len(selectors),
        "capabilities": capability_by_selector,
        "case_count": len(ordered),
        "passed": sum(bool(item["ok"]) for item in ordered),
        "answer_format_matches": sum(bool(item["answer_format_match"]) for item in ordered),
        "results": ordered,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("model_count", "case_count", "passed", "answer_format_matches")
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] == report["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
