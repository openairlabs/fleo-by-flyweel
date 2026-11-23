"""Jev (TypeSafe System One) typed decisions via OpenRouter's Decisions API.

Supports dedicated `FLEO_DECISIONS_API_KEY` (falling back to `OPENROUTER_API_KEY`)
and `FLEO_JEV_ENABLED=0` / `JEV=0` kill-switches. Returns None on any failure so
callers always degrade cleanly to their non-Jev path.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import os
from typing import Any, Dict, List, Optional

import aiohttp

from .agent_credentials import resolve_role_credential

logger = logging.getLogger(__name__)

JEV_MODEL = "typesafe/jev-1.13"
_URL = "https://openrouter.ai/api/alpha/decisions"
_TIMEOUT = aiohttp.ClientTimeout(total=10)

_cost: Dict[str, float] = {"usd": 0.0, "calls": 0, "input_tokens": 0}


def _decisions_key() -> Optional[str]:
    cred = resolve_role_credential("DECISIONS")
    return cred.api_key if (cred.configured and cred.provider == "openrouter") else None


def enabled() -> bool:
    if os.getenv("JEV", "1") == "0" or os.getenv("FLEO_JEV_ENABLED", "1") == "0":
        return False
    return bool(_decisions_key())


def reset_cost() -> None:
    _cost.update(usd=0.0, calls=0, input_tokens=0)


def cost() -> Dict[str, float]:
    return dict(_cost)


_MAX_RETRY_AFTER_SECONDS = 5.0


def _parse_retry_after_seconds(raw_header: Optional[str]) -> Optional[float]:
    if not raw_header or not raw_header.strip():
        return 1.0
    val = raw_header.strip()
    try:
        secs = float(val)
        return max(0.0, secs) if math.isfinite(secs) else 1.0
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return 1.0


def _retryable(status: int, body: Any) -> bool:
    if status == 429 or status >= 500:
        return True
    return status == 402 and isinstance(body, dict) and "openrouter_in_flight_budget" in str(body)


async def ask(
    state: Any,
    questions: Dict[str, Dict[str, Any]],
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[Dict[str, Dict[str, Any]]]:
    api_key = _decisions_key()
    if not enabled() or not api_key or not questions:
        return None
    payload = {"model": JEV_MODEL, "state": state, "questions": questions}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    own = session is None
    session = session or aiohttp.ClientSession(timeout=_TIMEOUT)
    try:
        for attempt in (1, 2):
            async with session.post(_URL, json=payload, headers=headers, timeout=_TIMEOUT) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001
                    body = None
                if resp.status == 200 and isinstance(body, dict) and isinstance(body.get("answers"), dict):
                    usage = body.get("usage") or {}
                    _cost["calls"] += 1
                    _cost["usd"] += float(usage.get("cost") or 0)
                    _cost["input_tokens"] += int(usage.get("input_tokens") or 0)
                    return body["answers"]
                if attempt == 1 and _retryable(resp.status, body):
                    delay_seconds = _parse_retry_after_seconds(resp.headers.get("Retry-After"))
                    if delay_seconds is None or delay_seconds > _MAX_RETRY_AFTER_SECONDS:
                        logger.warning("Jev Retry-After %.1fs exceeds %.1fs budget; skipping retry", delay_seconds or 0.0, _MAX_RETRY_AFTER_SECONDS)
                        return None
                    await asyncio.sleep(delay_seconds)
                    continue
                logger.warning("Jev decision failed: HTTP %s %s", resp.status, str(body)[:160])
                return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Jev decision failed: %s: %s", type(exc).__name__, str(exc)[:160])
        return None
    finally:
        if own:
            await session.close()
    return None


def noul(answers: Optional[Dict[str, Any]], name: str) -> Optional[float]:
    if not isinstance(answers, dict):
        return None
    entry = answers.get(name)
    if not isinstance(entry, dict):
        return None
    value = entry.get("noul")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


async def ask_many(
    states: List[Any],
    questions: Dict[str, Dict[str, Any]],
    concurrency: int = 20,
) -> List[Optional[Dict[str, Dict[str, Any]]]]:
    if not enabled() or not states:
        return [None] * len(states)
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async def one(state: Any):
            async with sem:
                return await ask(state, questions, session=session)

        return list(await asyncio.gather(*(one(s) for s in states)))


def ask_many_sync(
    states: List[Any],
    questions: Dict[str, Dict[str, Any]],
    concurrency: int = 20,
) -> List[Optional[Dict[str, Dict[str, Any]]]]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ask_many(states, questions, concurrency))
    return [None] * len(states)
