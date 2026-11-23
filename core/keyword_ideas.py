"""GSC-driven keyword-idea generation — core layer for the "suggest keywords"
feature on the Ink TUI landing screen.

Blends two flavors of idea, both produced by ``SmartAIRouter.ideate_keywords``:
- "grounded": expansions/reframings of real GSC query clusters the site
  already gets impressions for.
- "original": novel angles invented from the ICP's real problems in the brand's
  domain, and (optionally) recent trends.

Ideation is deliberately NOT fed the brand's own product catalog — blog ideas
are reader-searchable problems; brand tie-in happens downstream in generation.

Every candidate is then scored with the exact same GSC opportunity math the
main generation pipeline gates on (``core/generator.py`` ~L617-693: primary
keyword traffic potential -> estimated-neighbor fallback -> normalized 0-100
score), so a suggestion's score means the same thing here as it does in the
GSC Preflight step later.

GSC unavailability must never raise out of this module — it degrades to
unscored suggestions, matching the rest of the codebase's "GSC unavailable:
continues without GSC features" philosophy (see CLAUDE.md).

Public entry point: ``generate_keyword_ideas``. Everything else is a private
pipeline helper. ``core/`` has no ``__init__.py`` — imports within this module
are relative.
"""
import asyncio
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .ai_router import SmartAIRouter
from .gsc_analyzer import (
    GSCAnalyzer,
    MIN_ESTIMATE_NEIGHBORS,
    calculate_traffic_potential,
    estimate_traffic_potential_from_neighbors,
    normalize_traffic_potential_score,
)
from .research import load_icp_context_for_research

logger = logging.getLogger(__name__)

# Final suggestion count is fixed: 2 grounded + 3 original, blended by score.
GROUNDED_SLOTS = 2
ORIGINAL_SLOTS = 3
FINAL_COUNT = GROUNDED_SLOTS + ORIGINAL_SLOTS  # 5

# How many raw ideas to request from the LLM (brief's "~10-12 candidates").
IDEATION_COUNT = 12

# Concurrency cap on GSC scoring calls per run — bounds cost/latency even if
# the ideation pool comes back larger than requested.
SCORE_POOL_CAP = 10

# Compact GSC prompt block: top-N site queries by traffic potential.
TOP_GSC_QUERIES_FOR_PROMPT = 30

# Queries the site already ranks near the top for (avg position below this) are
# captured demand — navigational hits or pages that already won. They are never
# anchors for new posts (a new post would cannibalize the page that ranks), so
# they're excluded from the ideation anchor block entirely.
ALREADY_WON_POSITION = 4.0

# Near-duplicate collapse for the anchor block: queries whose token sets overlap
# at or above this Jaccard ratio are one cluster and get one line. Without this,
# one query family can flood the block (a live audit found 11 of 30 rows were
# trivial variants of the same three-word query) and the model anchors grounded
# ideas on that same cluster every single run.
ANCHOR_CLUSTER_JACCARD = 0.6

DEFAULT_TARGET = 60

UNAVAILABLE_RATIONALE_PREFIX = "GSC unavailable — unscored. "

def _default_trends_topic() -> str:
    from .brand_profile import load_brand_profile
    p = load_brand_profile()
    if p.is_configured():
        topics = ", ".join(p.icp_priority_topics[:6]) or p.category
        return f"{p.category} — {p.one_liner or topics} (audience: {p.icp_reader})"
    return "software, operations, and growth workflows for modern teams"


_DEFAULT_TRENDS_TOPIC = _default_trends_topic()
_TRENDS_WORD_LIMIT = 300
_PRODUCTS_CONFIG_PATH = Path(__file__).parent.parent / "config" / "products.json"

_GENERIC_NAME_TOKENS = {
    "ai", "agent", "agents", "cloud", "pro", "enterprise", "manager",
    "marketing", "analytics", "data", "report", "reporting", "suite",
    "the", "for", "and", "of", "app", "tool", "tools", "platform",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def generate_keyword_ideas(
    seed: Optional[str],
    style: str = "standard",
    *,
    steer: Optional[str] = None,
    previous: Optional[List[str]] = None,
    target: int = DEFAULT_TARGET,
    trends_fresh: bool = False,
    mode: str = "angle",
    gsc: Optional[GSCAnalyzer] = None,
    ai: Optional[SmartAIRouter] = None,
) -> Dict[str, Any]:
    """Generate exactly 5 unique, GSC-scored keyword ideas.

    Opens ``SmartAIRouter()``/``GSCAnalyzer()`` via ``async with`` when not
    injected (``gsc=``/``ai=`` are test/caller-injection points — see
    tests/test_keyword_ideas.py). GSC unavailability degrades gracefully to
    unscored suggestions rather than raising; AI unavailability is the only
    hard failure mode (there is no idea without the ideation model).

    ``mode`` (``"angle"`` | ``"keyword"``) steers the ideation agent's emphasis
    and which field is primary for dedupe vs ``previous``; the scoring math
    downstream is unchanged (always keyed on ``keyword``).

    Returns ``{"suggestions": [...], "target": int, "gsc_available": bool,
    "mode": str}``.
    """
    if ai is not None:
        return await _generate_with_ai(
            seed, style, steer=steer, previous=previous, target=target,
            trends_fresh=trends_fresh, mode=mode, gsc=gsc, ai=ai,
        )

    async with SmartAIRouter() as ai_instance:
        return await _generate_with_ai(
            seed, style, steer=steer, previous=previous, target=target,
            trends_fresh=trends_fresh, mode=mode, gsc=gsc, ai=ai_instance,
        )


async def _generate_with_ai(
    seed: Optional[str],
    style: str,
    *,
    steer: Optional[str],
    previous: Optional[List[str]],
    target: int,
    trends_fresh: bool,
    mode: str,
    gsc: Optional[GSCAnalyzer],
    ai: SmartAIRouter,
) -> Dict[str, Any]:
    if gsc is not None:
        return await _run_pipeline(
            seed, style, steer=steer, previous=previous, target=target,
            trends_fresh=trends_fresh, mode=mode, gsc=gsc, ai=ai,
        )

    try:
        async with GSCAnalyzer() as gsc_instance:
            return await _run_pipeline(
                seed, style, steer=steer, previous=previous, target=target,
                trends_fresh=trends_fresh, mode=mode, gsc=gsc_instance, ai=ai,
            )
    except Exception as e:
        # GSCAnalyzer() itself never raises in practice (its __init__ swallows
        # everything), but the async-with construction is guarded anyway per
        # the repo's "GSC unavailable: continues without GSC features" rule.
        logger.warning(f"GSCAnalyzer unavailable for keyword ideation: {e}")
        return await _run_pipeline(
            seed, style, steer=steer, previous=previous, target=target,
            trends_fresh=trends_fresh, mode=mode, gsc=None, ai=ai,
        )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def _run_pipeline(
    seed: Optional[str],
    style: str,
    *,
    steer: Optional[str],
    previous: Optional[List[str]],
    target: int,
    trends_fresh: bool,
    mode: str,
    gsc: Optional[GSCAnalyzer],
    ai: SmartAIRouter,
) -> Dict[str, Any]:
    # The real GSCAnalyzer signals missing auth/service by logging and returning
    # empty (`is_available` False) — it never raises. Read the flag up front so a
    # dead service degrades to honestly-labeled unscored suggestions instead of
    # reporting gsc_available=True with silently-null scores (and so we skip the
    # futile inventory + per-candidate scoring calls entirely). getattr default
    # keeps duck-typed injected analyzers without the property fully scoreable.
    gsc_available = gsc is not None and bool(getattr(gsc, "is_available", True))
    candidate_pool: List[Any] = []

    if gsc_available:
        try:
            inventory = await gsc.get_site_query_inventory()
            candidate_pool.extend(inventory or [])
        except Exception as e:
            # The first real GSC call failing is treated as a full outage —
            # skip the (equally doomed) scoring calls below rather than firing
            # 10 concurrent calls that will just raise too.
            logger.warning(f"GSC site query inventory unavailable for keyword ideation: {e}")
            gsc_available = False

    brand_terms = _load_product_brand_terms()
    gsc_context = (
        _build_gsc_prompt_block(candidate_pool, brand_terms) if gsc_available else ""
    )
    icp_context = load_icp_context_for_research()

    trends_context = ""
    if trends_fresh:
        trends_context = await _fetch_trends(ai, steer or seed)

    try:
        raw_pool = await ai.ideate_keywords(
            gsc_context=gsc_context,
            icp_context=icp_context,
            trends_context=trends_context,
            seed=seed,
            style=style,
            steer=steer,
            previous=previous,
            count=IDEATION_COUNT,
            mode=mode,
        )
    except Exception as e:
        logger.error(f"Keyword ideation call failed entirely: {e}")
        raw_pool = []

    pool = _dedupe_ideas(raw_pool or [], previous, mode)
    pool = _drop_cannibalizing_ideas(pool, candidate_pool)
    pool = _drop_product_ideas(pool, brand_terms)
    if not pool:
        return {"suggestions": [], "target": target, "gsc_available": gsc_available, "mode": mode}

    if not gsc_available:
        unscored = [_unscored_suggestion(idea, UNAVAILABLE_RATIONALE_PREFIX) for idea in pool]
        suggestions = _select_final_five(unscored)
        return {"suggestions": suggestions, "target": target, "gsc_available": False, "mode": mode}

    to_score = pool[:SCORE_POOL_CAP]
    embed_batch = getattr(ai, "_embed_batch", None)
    if callable(embed_batch):
        results = await _score_candidates_from_inventory(ai, to_score, candidate_pool)
    else:
        # Compatibility for injected/older routers. Production SmartAIRouter
        # always exposes _embed_batch and therefore takes the single-batch path.
        results = await asyncio.gather(
            *(_score_candidate(gsc, ai, idea) for idea in to_score),
            return_exceptions=True,
        )
    scored: List[Dict[str, Any]] = []
    for idea, result in zip(to_score, results):
        if isinstance(result, Exception):
            logger.warning(f"GSC scoring failed for '{idea.get('keyword')}': {result}")
            scored.append(_unscored_suggestion(idea, prefix=""))
        else:
            scored.append(result)

    suggestions = _select_final_five(scored)
    return {"suggestions": suggestions, "target": target, "gsc_available": True, "mode": mode}


def _cosine_similarity(left: List[float], right: List[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


async def _score_candidates_from_inventory(
    ai: SmartAIRouter,
    ideas: List[Dict[str, str]],
    inventory: List[Any],
) -> List[Dict[str, Any]]:
    """Score the candidate pool from one cached inventory and embedding call."""
    if not ideas:
        return []
    if not inventory:
        return [_unscored_suggestion(idea, prefix="") for idea in ideas]

    candidate_texts = [idea["keyword"] for idea in ideas]
    inventory_texts = [str(getattr(row, "query", "")) for row in inventory]
    try:
        embeddings = await ai._embed_batch(candidate_texts + inventory_texts)
    except Exception as exc:
        logger.warning(f"Keyword-idea batch embedding failed: {exc}")
        return [_unscored_suggestion(idea, prefix="") for idea in ideas]

    expected = len(candidate_texts) + len(inventory_texts)
    if len(embeddings) != expected or any(not vector for vector in embeddings):
        logger.warning("Keyword-idea batch embedding returned incomplete vectors")
        return [_unscored_suggestion(idea, prefix="") for idea in ideas]

    candidate_vectors = embeddings[:len(candidate_texts)]
    inventory_vectors = embeddings[len(candidate_texts):]
    exact_inventory = {
        normalize_query(str(getattr(row, "query", ""))): row for row in inventory
    }

    scored: List[Dict[str, Any]] = []
    for idea, candidate_vector in zip(ideas, candidate_vectors):
        keyword = idea["keyword"]
        direct = exact_inventory.get(normalize_query(keyword))
        if direct is not None and float(getattr(direct, "impressions", 0) or 0) > 0:
            traffic_potential = float(getattr(direct, "traffic_potential", 0) or 0)
            if traffic_potential <= 0:
                traffic_potential = calculate_traffic_potential(
                    int(float(getattr(direct, "impressions", 0) or 0)),
                    float(getattr(direct, "position", 0) or 0),
                )
            scored.append(_scored_suggestion(
                idea, traffic_potential=traffic_potential, grounded=True, estimated=False))
            continue

        neighbors = [
            (_cosine_similarity(candidate_vector, inventory_vector), row)
            for inventory_vector, row in zip(inventory_vectors, inventory)
        ]
        neighbors = [(similarity, row) for similarity, row in neighbors if similarity >= 0.60]
        if len(neighbors) < MIN_ESTIMATE_NEIGHBORS:
            scored.append(_unscored_suggestion(idea, prefix=""))
            continue

        estimate = estimate_traffic_potential_from_neighbors(neighbors)
        traffic_potential = float(estimate.get("traffic_potential") or 0)
        scored.append(_scored_suggestion(
            idea, traffic_potential=traffic_potential, grounded=False, estimated=True))

    return scored


def _scored_suggestion(
    idea: Dict[str, str],
    *,
    traffic_potential: float,
    grounded: bool,
    estimated: bool,
) -> Dict[str, Any]:
    return {
        "keyword": idea["keyword"],
        "angle": str(idea.get("angle") or idea["keyword"]),
        "score": normalize_traffic_potential_score(traffic_potential),
        "grounded": grounded,
        "estimated": estimated,
        "trafficPotential": traffic_potential,
        "rationale": str(idea.get("rationale") or ""),
        "_kind": idea.get("kind") or "original",
    }


async def _score_candidate(
    gsc: GSCAnalyzer,
    ai: SmartAIRouter,
    idea: Dict[str, str],
) -> Dict[str, Any]:
    """Score one candidate — replicates core/generator.py:617-693 exactly.

    ``grounded`` means direct GSC data (impressions > 0); ``estimated`` means
    no direct data but enough semantically-related-query signal
    (>= MIN_ESTIMATE_NEIGHBORS) to infer an opportunity. ``score`` is None
    only when neither signal exists (or this candidate's scoring raised,
    handled by the caller's return_exceptions=True gather).
    """
    keyword = idea["keyword"]
    rec = await gsc.get_content_recommendations(keyword)
    primary = rec.get("primary_keyword", {}) if isinstance(rec, dict) else {}
    traffic_potential = float(primary.get("traffic_potential") or 0)
    has_gsc_data = float(primary.get("impressions") or 0) > 0
    gsc_status = str(
        (rec.get("gsc_status") if isinstance(rec, dict) else None)
        or ("ok" if has_gsc_data else "no_data")
    )
    estimated = False

    if not has_gsc_data and not gsc_status.startswith("error"):
        est = await gsc.estimate_new_topic_opportunity(keyword, ai)
        if int((est or {}).get("neighbor_count") or 0) >= MIN_ESTIMATE_NEIGHBORS:
            estimated = True
            traffic_potential = float(est.get("traffic_potential") or 0)

    has_score = has_gsc_data or estimated
    score = normalize_traffic_potential_score(traffic_potential) if has_score else None

    return {
        "keyword": keyword,
        "angle": str(idea.get("angle") or keyword),
        "score": score,
        "grounded": has_gsc_data,
        "estimated": estimated,
        "trafficPotential": traffic_potential if has_score else None,
        "rationale": str(idea.get("rationale") or ""),
        "_kind": idea.get("kind") or "original",
    }


def _unscored_suggestion(idea: Dict[str, str], prefix: str = "") -> Dict[str, Any]:
    """Build a suggestion dict with no GSC signal (unavailable or scoring-raised)."""
    rationale = str(idea.get("rationale") or "").strip()
    return {
        "keyword": idea["keyword"],
        "angle": str(idea.get("angle") or idea["keyword"]),
        "score": None,
        "grounded": False,
        "estimated": False,
        "trafficPotential": None,
        "rationale": f"{prefix}{rationale}".strip(),
        "_kind": idea.get("kind") or "original",
    }


# ---------------------------------------------------------------------------
# Dedupe + selection
# ---------------------------------------------------------------------------

def _load_product_brand_terms() -> Set[str]:
    """Normalized brand & product phrases from active BrandProfile (or legacy products.json if present)."""
    from .brand_profile import load_brand_profile
    terms: Set[str] = set()
    profile = load_brand_profile()
    names = ([profile.company_name] if profile.company_name else []) + [p.name for p in profile.products]
    if _PRODUCTS_CONFIG_PATH.exists():
        try:
            data = json.loads(_PRODUCTS_CONFIG_PATH.read_text(encoding="utf-8"))
            names.extend(str((prod or {}).get("name") or "") for prod in (data.get("products") or {}).values())
        except (OSError, json.JSONDecodeError):
            pass
    for raw_name in names:
        name = normalize_query(raw_name)
        if not name:
            continue
        terms.add(name)
        for token in name.split():
            if token not in _GENERIC_NAME_TOKENS:
                terms.add(token)
    return terms


def _mentions_brand(text: str, brand_terms: Set[str]) -> bool:
    """Whole-phrase, punctuation/case-insensitive brand mention check."""
    if not brand_terms:
        return False
    padded = f" {normalize_query(text)} "
    return any(f" {term} " in padded for term in brand_terms)


def _drop_product_ideas(
    ideas: List[Dict[str, str]], brand_terms: Set[str]
) -> List[Dict[str, str]]:
    """Drop ideas whose keyword OR angle mentions one of the site's own products.

    Ideation must never subject the site's products (blog ideas are reader
    problems; brand tie-in happens downstream in generation) — but the model
    cannot know the registry post-catalog-decoupling, and fresh-trends context
    actively reintroduces product-adjacent vocabulary (MCP is a standing
    industry trend). This is the deterministic backstop the prompt can't be.
    """
    kept: List[Dict[str, str]] = []
    for idea in ideas:
        if _mentions_brand(idea.get("keyword") or "", brand_terms) or _mentions_brand(
            idea.get("angle") or "", brand_terms
        ):
            logger.info(
                f"Dropping product-subject idea '{idea.get('angle') or idea.get('keyword')}' — "
                f"mentions a registry product"
            )
            continue
        kept.append(idea)
    return kept


def _drop_cannibalizing_ideas(
    ideas: List[Dict[str, str]], inventory: List[Any]
) -> List[Dict[str, str]]:
    """Drop ideas that re-target queries the site already ranks for.

    An idea whose keyword IS an inventory query (or a trimmed piece of one)
    isn't an opportunity — the site already has a page collecting impressions
    for that demand, and a new post would cannibalize it (the main pipeline
    aborts generation on exactly this condition). The check is anchored to the
    site's own GSC inventory, exact and punctuation-insensitive — not a word
    blacklist. A live audit caught both repeat winners this way: keyword
    'meta ads mcp' (verbatim the #1 inventory query) and 'paid ads reporting
    system' (substring of 'all in one paid ads reporting system'). Genuine
    grounded expansions (a LONGER adjacent query that merely contains an
    inventory phrase) pass through.
    """
    inventory_norm = {normalize_query(getattr(kw, "query", "")) for kw in inventory}
    inventory_norm.discard("")
    if not inventory_norm:
        return ideas
    kept: List[Dict[str, str]] = []
    for idea in ideas:
        k = normalize_query(idea.get("keyword") or "")
        if k and len(k.split()) >= 2 and any(k == q or k in q for q in inventory_norm):
            logger.info(
                f"Dropping cannibalizing idea keyword '{idea.get('keyword')}' — "
                f"already in site GSC inventory"
            )
            continue
        kept.append(idea)
    return kept


def _dedupe_ideas(
    ideas: List[Dict[str, str]], previous: Optional[List[str]], mode: str = "angle"
) -> List[Dict[str, str]]:
    """Dedupe casefold on keyword; drop anything present in ``previous``.

    ``previous`` holds whatever strings the user actually saw — angles in
    ``angle`` mode, keywords in ``keyword`` mode — so the exclusion compares
    against the mode-primary field, not always the keyword.
    """
    excluded = {p.strip().casefold() for p in (previous or []) if p and p.strip()}
    seen = set()
    result: List[Dict[str, str]] = []
    for idea in ideas:
        keyword = (idea.get("keyword") or "").strip()
        if not keyword:
            continue
        angle = (idea.get("angle") or "").strip() or keyword
        key = keyword.casefold()
        primary_key = (angle if mode == "angle" else keyword).casefold()
        if key in seen or primary_key in excluded:
            continue
        seen.add(key)
        result.append({**idea, "keyword": keyword, "angle": angle})
    return result


def _sort_key(item: Dict[str, Any]):
    """Score desc, None-scores last (stable — preserves original order on ties)."""
    score = item.get("score")
    return (score is None, -(score if score is not None else 0))


def _bucket_of(item: Dict[str, Any]) -> str:
    return "grounded" if item.get("_kind") == "grounded" else "original"


def _select_final_five(scored: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Blend up to 2 grounded + 3 original by score, cross-bucket backfill.

    Sorting each bucket by score desc already prefers higher-scoring
    candidates first, which is equivalent to "preferring candidates with
    score >= target" without needing target as a hard cutoff. If a bucket is
    short, the other bucket's best remaining candidates fill in; if the whole
    pool is still short of 5 (tiny ideation pool), whatever remains — including
    unscored ideas — backfills the rest. Never invents ideas: returns fewer
    than 5 only when the deduped pool itself had fewer than 5 unique ideas.
    """
    grounded = sorted((c for c in scored if _bucket_of(c) == "grounded"), key=_sort_key)
    original = sorted((c for c in scored if _bucket_of(c) == "original"), key=_sort_key)

    picked: List[Dict[str, Any]] = []
    picked_keys: set = set()

    def take(bucket: List[Dict[str, Any]], n: int) -> None:
        for item in bucket:
            if n <= 0:
                break
            key = item["keyword"].casefold()
            if key in picked_keys:
                continue
            picked_keys.add(key)
            picked.append(item)
            n -= 1
            if n <= 0:
                break

    take(grounded, GROUNDED_SLOTS)
    take(original, ORIGINAL_SLOTS)

    if len(picked) < FINAL_COUNT:
        remaining = sorted(
            (c for c in (grounded + original) if c["keyword"].casefold() not in picked_keys),
            key=_sort_key,
        )
        take(remaining, FINAL_COUNT - len(picked))

    picked.sort(key=_sort_key)
    return [{k: v for k, v in item.items() if k != "_kind"} for item in picked[:FINAL_COUNT]]


# ---------------------------------------------------------------------------
# Prompt context builders
# ---------------------------------------------------------------------------

def _query_tokens(query: str) -> Set[str]:
    """Lowercase alphanumeric token set — the unit for anchor clustering."""
    return set(re.findall(r"[a-z0-9]+", (query or "").lower()))


def normalize_query(query: str) -> str:
    """Punctuation/case-insensitive form for inventory comparison
    ('Meta-Ads-MCP' == 'meta ads mcp')."""
    return " ".join(re.findall(r"[a-z0-9]+", (query or "").lower()))


def _build_gsc_prompt_block(
    candidates: List[Any], brand_terms: Optional[Set[str]] = None
) -> str:
    """Compact prompt block of the top ~30 anchor-worthy site queries.

    Shapes the anchor data so the model can't converge on the same grounded
    ideas every run (a live audit proved both failure modes):
    - Drops queries the site already ranks top-~3 for (``ALREADY_WON_POSITION``)
      — captured/navigational demand; anchoring there cannibalizes the page
      that already ranks.
    - Drops queries mentioning a registry product (``brand_terms``) regardless
      of position — those are people hunting the site's own products ("meta
      ads mcp" at position 7.9 is product demand, not a blog opportunity),
      and the model cannot recognize them as navigational on its own.
    - Collapses near-duplicate query variants into one line
      (``ANCHOR_CLUSTER_JACCARD``) so one query family can't flood the block.
    - Ranks by traffic potential with impressions as tiebreak (inventory rows
      commonly carry traffic_potential=0, which made a potential-only sort a
      no-op).
    """
    if not candidates:
        return ""
    if brand_terms is None:
        brand_terms = _load_product_brand_terms()
    ranked = sorted(
        candidates,
        key=lambda kw: (
            getattr(kw, "traffic_potential", 0) or 0,
            getattr(kw, "impressions", 0) or 0,
        ),
        reverse=True,
    )
    lines: List[str] = []
    kept_tokens: List[Set[str]] = []
    for kw in ranked:
        position = float(getattr(kw, "position", 0) or 0)
        if 0 < position < ALREADY_WON_POSITION:
            continue
        if _mentions_brand(kw.query, brand_terms):
            continue
        tokens = _query_tokens(kw.query)
        if not tokens:
            continue
        if any(
            len(tokens & kept) / max(len(tokens | kept), 1) >= ANCHOR_CLUSTER_JACCARD
            for kept in kept_tokens
        ):
            continue
        kept_tokens.append(tokens)
        lines.append(
            f'- "{kw.query}" (impressions={kw.impressions}, position={kw.position:.1f}, '
            f"traffic_potential={kw.traffic_potential:.0f})"
        )
        if len(lines) >= TOP_GSC_QUERIES_FOR_PROMPT:
            break
    return "\n".join(lines)


async def _fetch_trends(ai: SmartAIRouter, topic: Optional[str]) -> str:
    """Recent trends via Perplexity web search (empty on missing key / failure)."""
    resolved_topic = (topic or "").strip() or _DEFAULT_TRENDS_TOPIC
    query = f"current trends and emerging topics in {resolved_topic} (last 6 months)"
    try:
        result = await ai.research(query, search_type="web")
    except Exception as e:
        logger.warning(f"Trends research failed for keyword ideation: {e}")
        return ""

    if not isinstance(result, dict) or result.get("error"):
        return ""
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if not content or not isinstance(content, str):
        return ""

    words = content.split()
    return " ".join(words[:_TRENDS_WORD_LIMIT])
