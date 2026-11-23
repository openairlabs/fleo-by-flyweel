"""LLM-as-judge for subjective AI-SEO / GEO content dimensions.

The deterministic ``ContentValidator`` covers everything regex can measure
(structure, citation density, schema, banned words, keyword stuffing). It
cannot measure the *subjective* Princeton GEO factors — clarity (+20%),
fluency (+15-30%), and authoritative tone (+25%). Those need a model.

``judge_quality`` scores exactly those dimensions on a 0-100 scale via the
shared ``SmartAIRouter``, using the light model with reasoning disabled
(routed per vendor by ``SmartAIRouter._or_extra``) so the call does not exhaust
``max_tokens`` on hidden reasoning and return ``content=None`` — the
token-exhaustion trap documented in CLAUDE.md.

Contract: never silently substitute a default score. On hard failure a
dimension is recorded with ``score=None`` and an ``error`` string so the
composite can exclude it rather than fake a pass.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .ai_router import SmartAIRouter

logger = logging.getLogger(__name__)

DEFAULT_DIMENSIONS = ["clarity", "fluency", "authoritative_tone"]

# Body chars sent to the judge. Clarity/fluency/tone are well-sampled long
# before this; capping keeps the call bounded without starving the judge.
_MAX_BODY_CHARS = 16000

_DIMENSION_GUIDE = {
    "clarity": "How easily a reader (and an AI answer engine) can extract a direct "
               "answer. High = short sentences, one idea per paragraph, answer-first.",
    "fluency": "Natural, human, readable prose. High = varied sentence rhythm, no "
               "robotic filler, no AI tells, smooth flow.",
    "authoritative_tone": "Demonstrated expertise and confidence backed by specifics. "
                          "High = concrete data, named sources, decisive claims; "
                          "Low = vague hedging and generic marketing fluff.",
}


def _strip_frontmatter(content: str) -> str:
    m = re.match(r"^---\n.*?\n---\n(.*)$", content, re.DOTALL)
    return m.group(1) if m else content


def _build_prompt(body: str, keyword: str, dimensions: List[str]) -> str:
    rubric = "\n".join(f'- "{d}": {_DIMENSION_GUIDE.get(d, d)}' for d in dimensions)
    keys = ", ".join(f'"{d}"' for d in dimensions)
    return f"""You are a strict content-quality judge for AI-search (GEO) optimization.

Score the article below on each dimension from 0 to 100 (100 = best). Judge only
what is written; do not reward length. Be discerning — reserve 90+ for genuinely
excellent writing.

TARGET KEYWORD: {keyword}

DIMENSIONS:
{rubric}

Return ONLY a JSON object, no prose, no code fences, in EXACTLY this shape:
{{"dimensions": {{ {keys} }}}}
where each dimension maps to {{"score": <int 0-100>, "rationale": "<one sentence>"}}.

ARTICLE:
{body}
"""


def _parse_judge_json(raw: str, dimensions: List[str]) -> Optional[Dict[str, Any]]:
    """Parse the judge's JSON, tolerating code fences / surrounding prose."""
    if not raw:
        return None
    text = raw.strip()
    # Strip code fences.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    # Isolate the outermost JSON object.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    dims = data.get("dimensions") if isinstance(data, dict) else None
    if not isinstance(dims, dict):
        return None
    out: Dict[str, Any] = {}
    for d in dimensions:
        payload = dims.get(d)
        if isinstance(payload, dict) and isinstance(payload.get("score"), (int, float)):
            score = int(max(0, min(100, payload["score"])))
            out[d] = {"score": score, "rationale": str(payload.get("rationale", ""))[:300], "error": None}
        else:
            out[d] = {"score": None, "rationale": "", "error": "missing_or_unparseable"}
    return out


async def judge_quality(
    content: str,
    keyword: str,
    router: Any,
    *,
    dimensions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Score subjective GEO dimensions (0-100 each) with an LLM judge.

    Args:
        content: Full MDX (frontmatter is stripped before judging).
        keyword: Target keyword, for relevance grounding.
        router: A ``SmartAIRouter`` instance (uses its ``or_client``,
            ``model_light``, ``_qwen_create``, ``_safe_content``).
        dimensions: Override the dimensions to score.

    Returns:
        ``{"dimensions": {name: {"score": int|None, "rationale": str,
        "error": str|None}}, "model": str|None}``. On total failure every
        dimension carries ``score=None`` + an ``error`` — never a faked pass.
    """
    dims = dimensions or DEFAULT_DIMENSIONS

    def _all_failed(reason: str) -> Dict[str, Any]:
        return {
            "dimensions": {d: {"score": None, "rationale": "", "error": reason} for d in dims},
            "model": None,
        }

    client = getattr(router, "or_client", None)
    if client is None:
        logger.warning("quality_judge: no OpenRouter client available; dimensions left null")
        return _all_failed("no_client")

    body = _strip_frontmatter(content)[:_MAX_BODY_CHARS]
    prompt = _build_prompt(body, keyword or "", dims)
    model = getattr(router, "model_light", "openai/gpt-6-luna")
    # Vendor-correct routing for the raw-client path: a Nebius pin must never
    # reach an OpenAI/Anthropic light model.
    extra_body = SmartAIRouter._or_extra(
        model, "off", getattr(router, "OR_PROVIDER_NEBIUS_FAST", None))

    messages = [
        {"role": "system", "content": "You are a precise JSON-only content quality judge."},
        {"role": "user", "content": prompt},
    ]

    # Prefer the router's shared light-call path (vendor routing + the Jev
    # fallback); fall back to a raw client call for router doubles that only
    # expose or_client.
    qwen_create = getattr(router, "_qwen_create", None)

    # One call + one retry on parse failure (no faked default scores).
    last_reason = "unknown"
    for attempt in range(2):
        try:
            if qwen_create is not None:
                response = await qwen_create(
                    messages,
                    temperature=0.1,
                    max_tokens=800,
                    purpose="quality judge",
                    expect_json=True,
                )
            else:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=800,
                    extra_body=extra_body,
                )
        except Exception as e:  # noqa: BLE001 - surface, do not fake
            last_reason = f"api_error: {str(e).splitlines()[0][:120]}"
            logger.warning(f"quality_judge: API call failed (attempt {attempt + 1}/2): {last_reason}")
            continue

        raw = router._safe_content(response) if hasattr(router, "_safe_content") else None
        parsed = _parse_judge_json(raw or "", dims)
        if parsed is not None:
            used = (getattr(router, "models_used", None) or {}).get("quality judge", {})
            return {"dimensions": parsed, "model": used.get("model", model)}
        last_reason = "parse_failure"
        logger.warning(f"quality_judge: could not parse judge JSON (attempt {attempt + 1}/2)")

    return _all_failed(last_reason)


__all__ = ["judge_quality", "DEFAULT_DIMENSIONS"]
