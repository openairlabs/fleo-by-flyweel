"""Unit tests for core/keyword_ideas.py (GSC-driven keyword idea generation)
and SmartAIRouter.ideate_keywords (core/ai_router.py).

All offline: fake GSC + fake AI duck-typed objects are injected directly into
``generate_keyword_ideas`` (via its optional ``gsc=``/``ai=`` params) so no
network/credentials are needed. See tests/test_gsc_estimation.py and
tests/test_generator_gsc_gate.py for the fake-injection conventions this
follows.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.ai_router import SmartAIRouter
from core.gsc_analyzer import MIN_ESTIMATE_NEIGHBORS, normalize_traffic_potential_score
from core.keyword_ideas import generate_keyword_ideas


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeAI:
    """Duck-typed SmartAIRouter stand-in: only ideate_keywords / research / embeddings."""

    def __init__(self, ideas=None, research_result=None, raise_on_ideate=False):
        self._ideas = ideas if ideas is not None else []
        self._research_result = research_result
        self.raise_on_ideate = raise_on_ideate
        self.ideate_calls = []
        self.research_calls = []

    async def ideate_keywords(self, **kwargs):
        self.ideate_calls.append(kwargs)
        if self.raise_on_ideate:
            raise RuntimeError("ideation exploded")
        return list(self._ideas)

    async def research(self, query, search_type="web"):
        self.research_calls.append((query, search_type))
        if self._research_result is not None:
            return self._research_result
        return {"error": "no key"}

    async def filter_by_embedding_relevance(self, query, items, threshold=0.60):
        return []


class _FakeGSC:
    """Duck-typed GSCAnalyzer stand-in.

    ``recommendations`` maps keyword -> dict shaped like get_content_recommendations().
    ``estimates`` maps keyword -> dict shaped like estimate_new_topic_opportunity().
    ``raise_on_recommendations`` can be bool (global) or set/frozenset (per-keyword).
    """

    def __init__(self, inventory=None, opportunities=None, recommendations=None,
                 estimates=None, raise_on_inventory=False, raise_on_recommendations=False):
        self.inventory = inventory or []
        self.opportunities = opportunities or {"all_keywords": []}
        self.recommendations = recommendations or {}
        self.estimates = estimates or {}
        self.raise_on_inventory = raise_on_inventory
        self.raise_on_recommendations = raise_on_recommendations
        self.recommendation_calls = []
        self.estimate_calls = []

    async def get_site_query_inventory(self, days=180, row_limit=1000):
        if self.raise_on_inventory:
            raise RuntimeError("GSC inventory down")
        return self.inventory

    async def get_keyword_opportunities(self, base_keyword, days=90, min_impressions=50):
        return self.opportunities

    async def get_content_recommendations(self, keyword, days=90):
        self.recommendation_calls.append(keyword)
        # Support both boolean (global) and set (per-keyword) raising
        should_raise = False
        if isinstance(self.raise_on_recommendations, bool):
            should_raise = self.raise_on_recommendations
        elif isinstance(self.raise_on_recommendations, (set, frozenset)):
            should_raise = keyword in self.raise_on_recommendations
        if should_raise:
            raise RuntimeError("GSC recommendations down")
        return self.recommendations.get(
            keyword,
            {"primary_keyword": {"traffic_potential": 0, "impressions": 0}, "gsc_status": "no_data"},
        )

    async def estimate_new_topic_opportunity(self, keyword, ai_router, days=180):
        self.estimate_calls.append(keyword)
        return self.estimates.get(keyword, {"neighbor_count": 0, "traffic_potential": 0.0})


def _idea(keyword, kind="original", rationale="because", angle=None):
    return {
        "keyword": keyword,
        "angle": angle if angle is not None else f"why {keyword} is misunderstood",
        "kind": kind,
        "rationale": rationale,
    }


def _rec(traffic_potential, impressions, gsc_status="ok"):
    return {
        "primary_keyword": {"traffic_potential": traffic_potential, "impressions": impressions},
        "gsc_status": gsc_status,
    }


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

async def test_happy_path_blends_grounded_and_original():
    grounded_kw = [f"grounded kw {i}" for i in range(5)]
    original_kw = [f"original kw {i}" for i in range(7)]
    ideas = [_idea(kw, "grounded") for kw in grounded_kw] + [_idea(kw, "original") for kw in original_kw]

    recommendations = {}
    # Grounded ideas: real GSC data, decent scores.
    for i, kw in enumerate(grounded_kw):
        recommendations[kw] = _rec(traffic_potential=500 - i * 50, impressions=200)
    # Original ideas: no direct data, some get an estimate.
    for i, kw in enumerate(original_kw):
        recommendations[kw] = _rec(traffic_potential=0, impressions=0, gsc_status="no_data")

    estimates = {
        original_kw[0]: {"neighbor_count": 5, "traffic_potential": 300},
        original_kw[1]: {"neighbor_count": 4, "traffic_potential": 150},
        original_kw[2]: {"neighbor_count": 0, "traffic_potential": 0},
    }

    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC(recommendations=recommendations, estimates=estimates)

    result = await generate_keyword_ideas("attribution tools", gsc=fake_gsc, ai=fake_ai)

    assert result["gsc_available"] is True
    assert result["target"] == 60
    suggestions = result["suggestions"]
    assert len(suggestions) == 5

    keywords = [s["keyword"] for s in suggestions]
    assert len(set(k.casefold() for k in keywords)) == 5, "suggestions must be unique"

    for s in suggestions:
        assert set(s.keys()) == {"keyword", "angle", "score", "grounded", "estimated", "trafficPotential", "rationale"}
        assert isinstance(s["angle"], str) and s["angle"]

    scores = [s["score"] for s in suggestions]
    non_none = [sc for sc in scores if sc is not None]
    # sorted best-first: None scores (if any) sort last
    none_positions = [i for i, sc in enumerate(scores) if sc is None]
    if none_positions:
        assert min(none_positions) >= len(non_none)
    assert non_none == sorted(non_none, reverse=True)

    grounded_count = sum(1 for kw in keywords if kw in grounded_kw)
    original_count = sum(1 for kw in keywords if kw in original_kw)
    assert grounded_count == 2
    assert original_count == 3


# ---------------------------------------------------------------------------
# 2. Bucket backfill
# ---------------------------------------------------------------------------

async def test_backfill_when_grounded_bucket_short():
    ideas = [_idea("only grounded idea", "grounded")] + [
        _idea(f"original idea {i}", "original") for i in range(6)
    ]
    recommendations = {
        "only grounded idea": _rec(traffic_potential=400, impressions=100),
    }
    for i in range(6):
        recommendations[f"original idea {i}"] = _rec(
            traffic_potential=100 - i * 10, impressions=50)

    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC(recommendations=recommendations)

    result = await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai)
    suggestions = result["suggestions"]

    assert len(suggestions) == 5
    keywords = {s["keyword"] for s in suggestions}
    assert "only grounded idea" in keywords
    # 4 of the 6 originals fill the remaining slots.
    assert sum(1 for kw in keywords if kw.startswith("original idea")) == 4


async def test_backfill_when_original_bucket_short():
    ideas = [_idea(f"grounded idea {i}", "grounded") for i in range(6)] + [
        _idea("only original idea", "original")
    ]
    recommendations = {"only original idea": _rec(traffic_potential=0, impressions=0, gsc_status="no_data")}
    for i in range(6):
        recommendations[f"grounded idea {i}"] = _rec(
            traffic_potential=200 - i * 10, impressions=80)

    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC(recommendations=recommendations)

    result = await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai)
    suggestions = result["suggestions"]

    assert len(suggestions) == 5
    keywords = {s["keyword"] for s in suggestions}
    assert "only original idea" in keywords
    assert sum(1 for kw in keywords if kw.startswith("grounded idea")) == 4


# ---------------------------------------------------------------------------
# 3. Dedupe + previous
# ---------------------------------------------------------------------------

async def test_dedupe_case_variants_and_excludes_previous():
    ideas = [
        _idea("Attribution Tools", "grounded"),
        _idea("attribution tools", "grounded"),  # case-variant duplicate
        _idea("ATTRIBUTION TOOLS", "original"),  # duplicate again
        _idea("crm attribution guide", "original"),
        _idea("already suggested keyword", "original"),  # excluded via previous
        _idea("fresh original idea one", "original"),
        _idea("fresh original idea two", "original"),
    ]
    recommendations = {
        "attribution tools": _rec(traffic_potential=300, impressions=100),
        "crm attribution guide": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
        "fresh original idea one": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
        "fresh original idea two": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
    }
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC(recommendations=recommendations)

    # keyword mode: previous exclusion compares against the keyword field.
    result = await generate_keyword_ideas(
        "attribution", previous=["Already Suggested Keyword"], gsc=fake_gsc, ai=fake_ai,
        mode="keyword")

    suggestions = result["suggestions"]
    keywords = [s["keyword"] for s in suggestions]
    # Only one surviving instance of the case-variant duplicate.
    assert sum(1 for kw in keywords if kw.casefold() == "attribution tools") == 1
    assert "already suggested keyword" not in [kw.casefold() for kw in keywords]
    assert len(suggestions) == len(set(k.casefold() for k in keywords))


# ---------------------------------------------------------------------------
# 4. Scoring path
# ---------------------------------------------------------------------------

async def test_scoring_direct_data_vs_estimated_vs_none():
    ideas = [
        _idea("has direct data", "grounded"),
        _idea("no data but neighbors", "original"),
        _idea("no data no neighbors", "original"),
        _idea("filler one", "original"),
        _idea("filler two", "original"),
    ]
    recommendations = {
        "has direct data": _rec(traffic_potential=250, impressions=120),
        "no data but neighbors": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
        "no data no neighbors": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
        "filler one": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
        "filler two": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
    }
    estimates = {
        "no data but neighbors": {"neighbor_count": MIN_ESTIMATE_NEIGHBORS, "traffic_potential": 42.0},
        "no data no neighbors": {"neighbor_count": MIN_ESTIMATE_NEIGHBORS - 1, "traffic_potential": 999.0},
    }
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC(recommendations=recommendations, estimates=estimates)

    result = await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai)
    by_kw = {s["keyword"]: s for s in result["suggestions"]}

    direct = by_kw["has direct data"]
    assert direct["grounded"] is True
    assert direct["estimated"] is False
    assert direct["score"] == normalize_traffic_potential_score(250)
    assert direct["trafficPotential"] == 250

    estimated = by_kw["no data but neighbors"]
    assert estimated["grounded"] is False
    assert estimated["estimated"] is True
    assert estimated["score"] == normalize_traffic_potential_score(42.0)

    none_scored = by_kw["no data no neighbors"]
    assert none_scored["grounded"] is False
    assert none_scored["estimated"] is False
    assert none_scored["score"] is None
    assert none_scored["trafficPotential"] is None


async def test_production_scoring_fetches_one_inventory_and_uses_one_embedding_batch():
    inventory = [
        _row("crm attribution benchmarks", impressions=500, position=8.0),
        _row("lead source reporting", impressions=300, position=10.0),
        _row("closed loop measurement", impressions=200, position=12.0),
    ]
    ideas = [_idea(f"candidate topic {index}", "original") for index in range(6)]

    class BatchAI(_FakeAI):
        def __init__(self):
            super().__init__(ideas=ideas)
            self.embedding_batches = []

        async def _embed_batch(self, texts):
            self.embedding_batches.append(list(texts))
            return [[1.0, 0.0] for _ in texts]

    class InventoryOnlyGSC(_FakeGSC):
        def __init__(self):
            super().__init__(inventory=inventory)
            self.inventory_calls = 0

        async def get_site_query_inventory(self, days=180, row_limit=1000):
            self.inventory_calls += 1
            return self.inventory

        async def get_keyword_opportunities(self, *args, **kwargs):
            raise AssertionError("ideation must not fetch a second GSC inventory")

        async def get_content_recommendations(self, *args, **kwargs):
            raise AssertionError("per-candidate GSC fan-out is forbidden")

        async def estimate_new_topic_opportunity(self, *args, **kwargs):
            raise AssertionError("per-candidate embedding fan-out is forbidden")

    ai = BatchAI()
    gsc = InventoryOnlyGSC()

    result = await generate_keyword_ideas("attribution", gsc=gsc, ai=ai)

    assert len(result["suggestions"]) == 5
    assert gsc.inventory_calls == 1
    assert len(ai.embedding_batches) == 1
    assert len(ai.embedding_batches[0]) == min(12, len(ideas)) + len(inventory)


# ---------------------------------------------------------------------------
# 5. GSC-unavailable
# ---------------------------------------------------------------------------

async def test_gsc_unavailable_returns_unscored_suggestions():
    ideas = [_idea(f"idea {i}", "grounded" if i % 2 == 0 else "original") for i in range(6)]
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC(raise_on_inventory=True)

    result = await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai)

    assert result["gsc_available"] is False
    suggestions = result["suggestions"]
    assert len(suggestions) == 5
    for s in suggestions:
        assert s["score"] is None
        assert s["grounded"] is False
        assert s["estimated"] is False
        assert s["trafficPotential"] is None
        assert s["rationale"].startswith("GSC unavailable — unscored. ")
    # No GSC scoring calls were ever attempted once inventory failed.
    assert fake_gsc.recommendation_calls == []


async def test_gsc_service_unavailable_detected_without_exceptions():
    """The real GSCAnalyzer signals missing auth by LOGGING and returning empty
    (``is_available`` False) — it never raises. The pipeline must read that flag
    rather than rely on exceptions; otherwise it reports gsc_available=True with
    silently-null scores, skips the honest "GSC unavailable" labeling, and burns
    a scoring call per candidate. (Regression: found in live smoke, 2026-07-04.)
    """
    ideas = [_idea(f"idea {i}", "grounded" if i % 2 == 0 else "original") for i in range(6)]
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC()
    fake_gsc.is_available = False  # mirrors the GSCAnalyzer.is_available property

    result = await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai)

    assert result["gsc_available"] is False
    suggestions = result["suggestions"]
    assert len(suggestions) == 5
    for s in suggestions:
        assert s["score"] is None
        assert s["rationale"].startswith("GSC unavailable — unscored. ")
    # Neither inventory nor scoring calls were attempted against a dead service.
    assert fake_gsc.recommendation_calls == []
    assert fake_gsc.estimate_calls == []


async def test_per_candidate_scoring_raised_keeps_unscored_in_suggestions():
    """When get_content_recommendations raises for one candidate, that
    candidate still appears in the final 5 with score=None (and grounded=False,
    estimated=False, trafficPotential=None), while other candidates score
    normally. Tests that gsc_available stays True when per-candidate failures
    occur (as opposed to global GSC unavailability).
    """
    ideas = [
        _idea("grounded one", "grounded"),
        _idea("grounded two", "grounded"),
        _idea("original one", "original"),
        _idea("original two", "original"),
        _idea("will fail scoring", "original"),  # This one's get_content_recommendations will raise
    ]

    # Setup recommendations for the candidates that should score successfully
    # Grounded ideas have direct GSC data (impressions > 0)
    # Original ideas with no direct GSC data need estimates to score
    recommendations = {
        "grounded one": _rec(traffic_potential=500, impressions=200),
        "grounded two": _rec(traffic_potential=400, impressions=150),
        "original one": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
        "original two": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
        # "will fail scoring" deliberately has no entry
    }

    # Provide estimates for the original ideas so they score
    estimates = {
        "original one": {"neighbor_count": MIN_ESTIMATE_NEIGHBORS, "traffic_potential": 200.0},
        "original two": {"neighbor_count": MIN_ESTIMATE_NEIGHBORS, "traffic_potential": 150.0},
    }

    fake_ai = _FakeAI(ideas=ideas)
    # Pass empty inventory so that gsc_available stays True (inventory call succeeds)
    # Use per-keyword raise set for only "will fail scoring"
    fake_gsc = _FakeGSC(
        inventory=[],
        recommendations=recommendations,
        estimates=estimates,
        raise_on_recommendations={"will fail scoring"},
    )

    result = await generate_keyword_ideas("test keyword", gsc=fake_gsc, ai=fake_ai)

    # GSC should still be available because inventory succeeded
    assert result["gsc_available"] is True

    suggestions = result["suggestions"]
    assert len(suggestions) == 5

    keywords = {s["keyword"] for s in suggestions}
    assert "will fail scoring" in keywords, "Failed candidate must still appear in top 5"

    # Find the failed candidate
    failed_suggestion = next(s for s in suggestions if s["keyword"] == "will fail scoring")
    assert failed_suggestion["score"] is None, "Failed candidate should be unscored"
    assert failed_suggestion["grounded"] is False
    assert failed_suggestion["estimated"] is False
    assert failed_suggestion["trafficPotential"] is None

    # Other candidates should score normally
    scored_suggestions = [s for s in suggestions if s["keyword"] != "will fail scoring"]
    scored_scores = [s["score"] for s in scored_suggestions]
    assert all(sc is not None for sc in scored_scores), "Non-failed candidates should score"


async def test_gsc_construction_failure_falls_back_gracefully(monkeypatch):
    """When gsc= is not injected, generate_keyword_ideas constructs a real
    GSCAnalyzer(); if even *that* raises, the run must still degrade to
    unscored suggestions rather than raising (brief: "GSCAnalyzer can't init").
    """
    import core.keyword_ideas as keyword_ideas_module

    class _ExplodingGSCAnalyzer:
        def __init__(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(keyword_ideas_module, "GSCAnalyzer", _ExplodingGSCAnalyzer)

    ideas = [_idea(f"idea {i}") for i in range(5)]
    fake_ai = _FakeAI(ideas=ideas)

    result = await generate_keyword_ideas("attribution", ai=fake_ai)

    assert result["gsc_available"] is False
    assert len(result["suggestions"]) == 5
    assert all(s["rationale"].startswith("GSC unavailable — unscored. ") for s in result["suggestions"])


async def test_ideation_failure_does_not_raise_and_returns_empty():
    fake_ai = _FakeAI(raise_on_ideate=True)
    fake_gsc = _FakeGSC()

    result = await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai)

    assert result["gsc_available"] is True
    assert result["suggestions"] == []


async def test_short_ideation_pool_returns_fewer_than_five():
    ideas = [_idea("only one idea", "grounded"), _idea("only two idea", "original")]
    recommendations = {
        "only one idea": _rec(traffic_potential=100, impressions=50),
        "only two idea": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
    }
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC(recommendations=recommendations)

    result = await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai)

    assert len(result["suggestions"]) == 2


# ---------------------------------------------------------------------------
# Trends wiring (only fetched when trends_fresh=True)
# ---------------------------------------------------------------------------

async def test_trends_only_fetched_when_requested():
    ideas = [_idea(f"idea {i}") for i in range(5)]
    fake_ai = _FakeAI(ideas=ideas, research_result={
        "choices": [{"message": {"content": "AI-driven attribution is trending."}}]
    })
    fake_gsc = _FakeGSC()

    result = await generate_keyword_ideas(
        "attribution", gsc=fake_gsc, ai=fake_ai, trends_fresh=True)

    assert len(fake_ai.research_calls) == 1
    assert result["suggestions"]

    fake_ai_no_trends = _FakeAI(ideas=ideas)
    await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai_no_trends, trends_fresh=False)
    assert fake_ai_no_trends.research_calls == []


# ---------------------------------------------------------------------------
# Angle + mode wiring
# ---------------------------------------------------------------------------

async def test_mode_passthrough_to_ideate_keywords():
    ideas = [_idea(f"idea {i}") for i in range(5)]
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC()

    result = await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai, mode="keyword")

    assert fake_ai.ideate_calls[0]["mode"] == "keyword"
    assert result["mode"] == "keyword"


async def test_mode_defaults_to_angle():
    ideas = [_idea(f"idea {i}") for i in range(5)]
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC()

    result = await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai)

    assert fake_ai.ideate_calls[0]["mode"] == "angle"
    assert result["mode"] == "angle"


async def test_suggestions_carry_angle():
    ideas = [_idea(f"idea {i}", angle=f"bold take {i}") for i in range(5)]
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC()

    result = await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai)

    for s in result["suggestions"]:
        assert "angle" in s and isinstance(s["angle"], str) and s["angle"]
    angles = {s["angle"] for s in result["suggestions"]}
    assert angles == {f"bold take {i}" for i in range(5)}


async def test_dedupe_by_angle_in_angle_mode_drops_previous():
    ideas = [
        _idea("keyword a", angle="Attribution is a lie"),
        _idea("keyword b", angle="Budgets should follow revenue"),
        _idea("keyword c", angle="Fresh angle one"),
        _idea("keyword d", angle="Fresh angle two"),
        _idea("keyword e", angle="Fresh angle three"),
    ]
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC()

    # previous contains an ANGLE string the user already saw — angle mode drops it.
    result = await generate_keyword_ideas(
        "attribution", previous=["attribution is a lie"], gsc=fake_gsc, ai=fake_ai, mode="angle")

    angles = {s["angle"].casefold() for s in result["suggestions"]}
    assert "attribution is a lie" not in angles
    keywords = {s["keyword"] for s in result["suggestions"]}
    assert "keyword a" not in keywords


async def test_dedupe_by_keyword_in_keyword_mode_ignores_angle_previous():
    ideas = [
        _idea("keyword a", angle="Attribution is a lie"),
        _idea("keyword b", angle="Budgets should follow revenue"),
        _idea("keyword c", angle="Fresh angle one"),
    ]
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC()

    # In keyword mode, a previous ANGLE string must NOT drop the keyword idea.
    result = await generate_keyword_ideas(
        "attribution", previous=["attribution is a lie"], gsc=fake_gsc, ai=fake_ai, mode="keyword")

    keywords = {s["keyword"] for s in result["suggestions"]}
    assert "keyword a" in keywords


async def test_unscored_path_carries_angle():
    ideas = [_idea(f"idea {i}", angle=f"unscored take {i}") for i in range(6)]
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC(raise_on_inventory=True)  # forces the unscored path

    result = await generate_keyword_ideas("attribution", gsc=fake_gsc, ai=fake_ai)

    assert result["gsc_available"] is False
    for s in result["suggestions"]:
        assert s["angle"].startswith("unscored take ")
        assert s["score"] is None


# ---------------------------------------------------------------------------
# 6. ideate_keywords parsing (SmartAIRouter)
# ---------------------------------------------------------------------------

class TestIdeateKeywordsParsing:

    def test_parses_fenced_json_with_ideas_key(self):
        text = (
            "```json\n"
            '{"ideas": [{"angle": "Ad spend leaks before the click", '
            '"keyword": "Ad Spend Attribution", "kind": "grounded", '
            '"rationale": "real demand"}]}\n'
            "```"
        )
        result = SmartAIRouter._parse_ideation_response(text)
        assert result == [
            {"keyword": "ad spend attribution", "angle": "Ad spend leaks before the click",
             "kind": "grounded", "rationale": "real demand"}
        ]

    def test_parses_bare_list(self):
        text = ('[{"angle": "Most CRM migrations lose pipeline", '
                '"keyword": "crm migration guide", "kind": "original", "rationale": "fresh angle"}]')
        result = SmartAIRouter._parse_ideation_response(text)
        assert result == [
            {"keyword": "crm migration guide", "angle": "Most CRM migrations lose pipeline",
             "kind": "original", "rationale": "fresh angle"}
        ]

    def test_entries_missing_keyword_are_skipped(self):
        text = '{"ideas": [{"kind": "original"}, {"keyword": "", "kind": "original"}, ' \
               '{"keyword": "valid keyword", "kind": "original"}]}'
        result = SmartAIRouter._parse_ideation_response(text)
        # Missing angle falls back to the keyword.
        assert result == [
            {"keyword": "valid keyword", "angle": "valid keyword", "kind": "original", "rationale": ""}
        ]

    def test_missing_kind_and_rationale_default(self):
        text = '{"ideas": [{"keyword": "some keyword"}]}'
        result = SmartAIRouter._parse_ideation_response(text)
        assert result == [
            {"keyword": "some keyword", "angle": "some keyword", "kind": "original", "rationale": ""}
        ]

    def test_missing_or_blank_angle_falls_back_to_keyword(self):
        text = ('{"ideas": [{"keyword": "first keyword", "angle": "   "}, '
                '{"keyword": "second keyword", "angle": 123}]}')
        result = SmartAIRouter._parse_ideation_response(text)
        assert result == [
            {"keyword": "first keyword", "angle": "first keyword", "kind": "original", "rationale": ""},
            {"keyword": "second keyword", "angle": "second keyword", "kind": "original", "rationale": ""},
        ]

    def test_dedupes_case_variant_angles(self):
        text = json_dump_ideas([
            {"keyword": "keyword one", "angle": "Attribution Is A Lie"},
            {"keyword": "keyword two", "angle": "attribution is a lie"},  # dup angle, casefold
            {"keyword": "keyword three", "angle": "A different take entirely"},
        ])
        result = SmartAIRouter._parse_ideation_response(text)
        assert [i["keyword"] for i in result] == ["keyword one", "keyword three"]
        assert [i["angle"] for i in result] == ["Attribution Is A Lie", "A different take entirely"]

    def test_invalid_kind_defaults_to_original(self):
        text = '{"ideas": [{"keyword": "some keyword", "kind": "bogus"}]}'
        result = SmartAIRouter._parse_ideation_response(text)
        assert result[0]["kind"] == "original"

    def test_dedupes_case_variants_preserving_order(self):
        text = json_dump_ideas([
            {"keyword": "Attribution Tools", "kind": "grounded"},
            {"keyword": "attribution tools", "kind": "original"},
            {"keyword": "second keyword", "kind": "original"},
        ])
        result = SmartAIRouter._parse_ideation_response(text)
        assert [i["keyword"] for i in result] == ["attribution tools", "second keyword"]
        assert result[0]["kind"] == "grounded"  # first occurrence wins

    def test_malformed_json_returns_empty_list(self):
        assert SmartAIRouter._parse_ideation_response("not json at all {{{") == []

    def test_empty_text_returns_empty_list(self):
        assert SmartAIRouter._parse_ideation_response("") == []
        assert SmartAIRouter._parse_ideation_response(None) == []

    def test_non_list_ideas_value_returns_empty_list(self):
        assert SmartAIRouter._parse_ideation_response('{"ideas": "not a list"}') == []


def json_dump_ideas(ideas):
    import json
    return json.dumps({"ideas": ideas})


# ---------------------------------------------------------------------------
# ideate_keywords tiered fallback (fake or_client / gemini / nebius)
# ---------------------------------------------------------------------------

class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))]
        )


def _stub_router(or_client=None):
    """Bare SmartAIRouter with only the attributes ideate_keywords touches.

    Built via __new__ (bypassing __init__/env/API-client setup, same technique
    tests/test_gsc_estimation.py uses for GSCAnalyzer) so tests never depend on
    real credentials. Fallback tiers (_call_gemini_with_fallback /
    _nebius_with_fallback) are stubbed per-test as plain async functions
    assigned directly onto the instance — the same fake-injection idiom
    tests/test_gsc_estimation.py uses for GSCAnalyzer methods.
    """
    router = SmartAIRouter.__new__(SmartAIRouter)
    router.or_client = or_client
    router.model_precision = "openai/gpt-6-sol"
    router.gemini_flash = "google/gemini-3.8-flash"
    router.nebius_models = ["nousresearch/hermes-4-405b"]
    router.OR_PROVIDER_OPENAI_LOW = {"provider": {"only": ["openai"]}, "reasoning": {"effort": "low"}}
    router.models_used = {}
    return router


async def test_ideate_keywords_uses_primary_tier_on_success():
    completions = _FakeCompletions([
        '{"ideas": [{"keyword": "primary tier keyword", "kind": "grounded"}]}',
    ])
    router = _stub_router(or_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    async def _must_not_be_called(*args, **kwargs):
        raise AssertionError("fallback tier should not run when the primary tier succeeds")

    router._call_gemini_with_fallback = _must_not_be_called
    router._nebius_with_fallback = _must_not_be_called

    result = await router.ideate_keywords(
        gsc_context="", icp_context="icp", seed="attribution")

    assert result == [
        {"keyword": "primary tier keyword", "angle": "primary tier keyword",
         "kind": "grounded", "rationale": ""}
    ]
    assert router.models_used["keyword_ideation"]["model"] == "openai/gpt-6-sol"
    assert router.models_used["keyword_ideation"]["provider"] == "openai"
    assert router.models_used["keyword_ideation"]["downgraded"] is False
    assert completions.calls[0]["response_format"] == {"type": "json_object"}
    assert completions.calls[0]["extra_body"] == router.OR_PROVIDER_OPENAI_LOW


async def test_ideate_system_prompt_carries_ai_seo_diversity_frame():
    """Idea variety is driven by AI-SEO citable-archetype principles, not a hardcoded lens.

    Guards the course-correction: the enumerated vantage/archetype lists are gone;
    the system prompt now frames diversity by what AI answer engines actually cite
    and demands extractable, searcher-recognizable title-seeds.
    """
    completions = _FakeCompletions([
        '{"ideas": [{"keyword": "k", "kind": "original"}]}',
    ])
    router = _stub_router(or_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    async def _unused(*args, **kwargs):
        raise AssertionError("primary tier should have handled this")

    router._call_gemini_with_fallback = _unused
    router._nebius_with_fallback = _unused

    await router.ideate_keywords(
        gsc_context="", icp_context="icp", seed="attribution")

    system_msg = next(m["content"] for m in completions.calls[0]["messages"] if m["role"] == "system")
    # Diversity framed by what AI answer engines cite (comparison / data / contrarian / how-to...).
    assert "answer engines cite" in system_msg
    assert "comparisons" in system_msg
    # Title-seeds must be AI-SEO extractable + anchored to specificity a searcher recognizes.
    assert "searcher would recognize" in system_msg.lower()
    assert "a concrete number" in system_msg
    # The old enumerated-archetype rotation wording is gone.
    assert "different angle archetypes" not in system_msg


async def test_ideate_system_prompt_decoupled_from_product_catalog():
    """Ideation produces reader-searchable topics, not articles about Flyweel's products.

    Root-cause guard, configured by intent — NOT by a word blacklist. The product catalog
    isn't injected, the prompt frames ideas around the reader's search intent (products are
    never the subject), and the grounded path is told to SKIP navigational/brand-name queries
    by reading each query's intent — so "meta ads mcp" is deprioritized without any hardcoded
    term list (and legitimate terms like "claude" are never blanket-suppressed).
    """
    completions = _FakeCompletions([
        '{"ideas": [{"keyword": "k", "kind": "original"}]}',
    ])
    router = _stub_router(or_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    async def _unused(*args, **kwargs):
        raise AssertionError("primary tier should have handled this")

    router._call_gemini_with_fallback = _unused
    router._nebius_with_fallback = _unused

    await router.ideate_keywords(
        gsc_context="", icp_context="icp", seed="attribution")

    system_msg = next(m["content"] for m in completions.calls[0]["messages"] if m["role"] == "system")
    # The product lineup is no longer described as an input or an idea source.
    assert "product lineup" not in system_msg
    # Ideas are framed by the reader's search intent; the company's products are never the subject.
    assert "reader's search intent" in system_msg.lower()
    assert "never the subject of an idea" in system_msg
    assert "reader's real problems" in system_msg
    # Grounded ideas judge query intent and skip navigational/brand queries (no blacklist).
    assert "navigational" in system_msg


async def test_ideate_pov_stance_is_present_but_seeds_no_topic_vocabulary():
    """A light Flyweel POV steers STANCE without seeding the topics it's trying not to over-produce.

    Guards the exact balance the user demanded twice over: (1) the voice guardrail exists so
    ideas stop contradicting what Flyweel believes (don't argue against investing in / funding /
    measuring growth), but (2) it names NO domain vocabulary — because listing "ROAS / last-click /
    financing / vanity dashboards" would make the model flood the output with those very topics.
    Both halves are asserted: presence of the guardrail AND absence of the seeding terms.
    """
    completions = _FakeCompletions([
        '{"ideas": [{"keyword": "k", "kind": "original"}]}',
    ])
    router = _stub_router(or_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    async def _unused(*args, **kwargs):
        raise AssertionError("primary tier should have handled this")

    router._call_gemini_with_fallback = _unused
    router._nebius_with_fallback = _unused

    await router.ideate_keywords(
        gsc_context="", icp_context="icp", seed="attribution")

    system_msg = next(m["content"] for m in completions.calls[0]["messages"] if m["role"] == "system")
    # (1) The stance guardrail exists and is scoped to voice only, not a topic.
    assert "caution on voice" in system_msg
    assert "check on stance only" in system_msg
    assert "must not pull ideas toward any particular topic" in system_msg
    # (2) It seeds none of the villain vocabulary that would bias the output toward those topics.
    assert "last-click" not in system_msg
    assert "vanity dashboard" not in system_msg
    assert "platform-ROAS" not in system_msg


async def test_ideate_keywords_falls_back_through_tiers_on_malformed_json():
    completions = _FakeCompletions(["not valid json"])
    router = _stub_router(or_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    async def fake_gemini_fallback(**kwargs):
        return SimpleNamespace(content="also not valid json")  # malformed -> try next tier

    async def fake_nebius_fallback(messages, **kwargs):
        return '{"ideas": [{"keyword": "nebius fallback keyword", "kind": "original"}]}'

    router._call_gemini_with_fallback = fake_gemini_fallback
    router._nebius_with_fallback = fake_nebius_fallback

    result = await router.ideate_keywords(
        gsc_context="", icp_context="icp", seed="attribution")

    assert result == [
        {"keyword": "nebius fallback keyword", "angle": "nebius fallback keyword",
         "kind": "original", "rationale": ""}
    ]
    assert router.models_used["keyword_ideation"]["downgraded"] is True


async def test_ideate_keywords_returns_empty_when_all_tiers_fail():
    completions = _FakeCompletions(["still not json"])
    router = _stub_router(or_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    async def fake_gemini_fallback(**kwargs):
        return SimpleNamespace(content="also not json")

    async def fake_nebius_fallback(messages, **kwargs):
        return "nope, not json either"

    router._call_gemini_with_fallback = fake_gemini_fallback
    router._nebius_with_fallback = fake_nebius_fallback

    result = await router.ideate_keywords(
        gsc_context="", icp_context="icp", seed="attribution")

    assert result == []


async def test_ideate_keywords_no_or_client_skips_to_fallback():
    router = _stub_router(or_client=None)

    async def fake_gemini_fallback(**kwargs):
        return SimpleNamespace(content='{"ideas": [{"keyword": "gemini only keyword"}]}')

    router._call_gemini_with_fallback = fake_gemini_fallback

    result = await router.ideate_keywords(
        gsc_context="", icp_context="icp", seed="attribution")

    assert result == [
        {"keyword": "gemini only keyword", "angle": "gemini only keyword",
         "kind": "original", "rationale": ""}
    ]


# ---------------------------------------------------------------------------
# Grounded-anchor repetition fixes (position filter, cluster collapse,
# cannibalization guard, previous-anchor variety)
# ---------------------------------------------------------------------------

def _row(query, impressions=100, position=8.0, traffic_potential=0):
    return SimpleNamespace(
        query=query, impressions=impressions, position=position,
        traffic_potential=traffic_potential,
    )


class TestAnchorBlockShaping:
    """_build_gsc_prompt_block must shape anchors so grounded ideas can't
    converge on the same site queries every run (live-audit failure modes)."""

    def test_drops_already_won_positions(self):
        from core.keyword_ideas import _build_gsc_prompt_block
        block = _build_gsc_prompt_block([
            _row("flyweel mcp", impressions=500, position=1.4),   # navigational, already won
            _row("average cost per lead google ads", position=7.2),
        ])
        assert "flyweel mcp" not in block
        assert "average cost per lead google ads" in block

    def test_collapses_near_duplicate_query_families(self):
        from core.keyword_ideas import _build_gsc_prompt_block
        # The live audit found 11 of 30 rows were variants of one query family;
        # one cluster line survives, unrelated queries are untouched.
        block = _build_gsc_prompt_block([
            _row("google ads cost per lead", impressions=4518, position=7.9),
            _row("cost per lead google ads", impressions=1004, position=8.5),
            _row("google ads average cost per lead", impressions=681, position=7.6),
            _row("b2b cost per click", impressions=282, position=16.8),
        ])
        lines = block.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith('- "google ads cost per lead"')
        assert '"b2b cost per click"' in block

    def test_drops_product_registry_queries_regardless_of_position(self, monkeypatch):
        """Queries hunting the site's OWN products in BrandProfile are product demand, not blog opportunity."""
        from core.brand_profile import BrandProfile, ProductItem
        from core.keyword_ideas import _build_gsc_prompt_block

        monkeypatch.setattr(
            "core.brand_profile.load_brand_profile",
            lambda *a, **k: BrandProfile(
                company_name="Acme",
                products=[ProductItem(id="pgpulse", name="Acme PgPulse"), ProductItem(id="branchx", name="BranchX")],
            ),
        )
        block = _build_gsc_prompt_block([
            _row("postgres pgpulse", impressions=4518, position=7.9),
            _row("branchx workflow", impressions=120, position=9.0),
            _row("average query latency postgres", impressions=282, position=12.0),
        ])
        assert "pgpulse" not in block
        assert "branchx" not in block
        assert "average query latency postgres" in block

    def test_injected_empty_brand_terms_disables_product_filter(self):
        """brand_terms is injectable (tests/degraded registry) — empty set means
        no product filtering, keeping the block shaping testable in isolation."""
        from core.keyword_ideas import _build_gsc_prompt_block
        block = _build_gsc_prompt_block(
            [_row("meta ads mcp", impressions=100, position=8.0)], set()
        )
        assert "meta ads mcp" in block

    def test_ranks_by_impressions_when_traffic_potential_is_zero(self):
        from core.keyword_ideas import _build_gsc_prompt_block
        # Inventory rows commonly carry traffic_potential=0; the sort must not
        # degrade to insertion order.
        block = _build_gsc_prompt_block([
            _row("niche reporting workflow", impressions=10, position=9.0),
            _row("popular reporting benchmarks", impressions=900, position=9.0),
        ])
        assert block.splitlines()[0].startswith('- "popular reporting benchmarks"')

    def test_empty_candidates_returns_empty(self):
        from core.keyword_ideas import _build_gsc_prompt_block
        assert _build_gsc_prompt_block([]) == ""


class TestCannibalizationGuard:
    """Ideas re-targeting queries the site already ranks for are dropped —
    inventory-anchored and exact, never a word blacklist."""

    def test_drops_verbatim_and_trimmed_inventory_keywords(self):
        from core.keyword_ideas import _drop_cannibalizing_ideas
        inventory = [
            _row("meta ads mcp"),
            _row("all in one paid ads reporting system"),
        ]
        ideas = [
            _idea("Meta-Ads-MCP", kind="grounded"),                 # verbatim after normalization
            _idea("paid ads reporting system", kind="grounded"),    # trimmed substring of an inventory query
            _idea("google ads lead quality scorecards", kind="original"),
            # A LONGER adjacent query is a genuine grounded expansion — kept.
            _idea("paid ads reporting system for hvac companies", kind="grounded"),
        ]
        kept = [i["keyword"] for i in _drop_cannibalizing_ideas(ideas, inventory)]
        assert "Meta-Ads-MCP" not in kept
        assert "paid ads reporting system" not in kept
        assert "google ads lead quality scorecards" in kept
        assert "paid ads reporting system for hvac companies" in kept

    def test_empty_inventory_is_a_noop(self):
        from core.keyword_ideas import _drop_cannibalizing_ideas
        ideas = [_idea("anything at all")]
        assert _drop_cannibalizing_ideas(ideas, []) == ideas


class TestProductIdeaFilter:
    """Ideas subjecting the site's own products are dropped deterministically —
    the model can't recognize registry names post-catalog-decoupling, and
    fresh-trends context reintroduces product-adjacent vocabulary (MCP is a
    standing industry trend). Live failure: 'Meta Ads MCP can automate checks,
    not revenue decisions' won a slot in a trends-fresh run."""

    def test_drops_ideas_mentioning_products_in_angle_or_keyword(self, monkeypatch):
        from core.brand_profile import BrandProfile, ProductItem
        from core.keyword_ideas import _drop_product_ideas, _load_product_brand_terms

        monkeypatch.setattr(
            "core.brand_profile.load_brand_profile",
            lambda *a, **k: BrandProfile(
                company_name="Acme",
                products=[ProductItem(id="pgpulse", name="Acme PgPulse"), ProductItem(id="branchx", name="BranchX")],
            ),
        )
        terms = _load_product_brand_terms()
        ideas = [
            _idea("postgres automation limits", kind="grounded",
                  angle="PgPulse can automate checks, not schema design"),
            _idea("branchx for saas teams", kind="original"),
            _idea("postgres connection pooling benchmarks", kind="original",
                  angle="Benchmark PgBouncer transaction pooling"),
        ]
        kept = [i["keyword"] for i in _drop_product_ideas(ideas, terms)]
        assert "postgres automation limits" not in kept
        assert "branchx for saas teams" not in kept
        assert "postgres connection pooling benchmarks" in kept

    def test_registry_derived_terms_cover_names_and_distinctive_tokens(self, monkeypatch):
        """Brand terms come from BrandProfile — full names plus distinctive tokens, generic words excluded."""
        from core.brand_profile import BrandProfile, ProductItem
        from core.keyword_ideas import _load_product_brand_terms

        monkeypatch.setattr(
            "core.brand_profile.load_brand_profile",
            lambda *a, **k: BrandProfile(
                company_name="Acme",
                products=[ProductItem(id="pgpulse", name="Acme PgPulse"), ProductItem(id="ai-agent", name="Acme AI Agent")],
            ),
        )
        terms = _load_product_brand_terms()
        assert "acme pgpulse" in terms
        assert "pgpulse" in terms
        assert "acme" in terms
        assert "ai" not in terms
        assert "agent" not in terms

    def test_empty_brand_terms_is_a_noop(self):
        from core.keyword_ideas import _drop_product_ideas
        ideas = [_idea("postgres branching guide")]
        assert _drop_product_ideas(ideas, set()) == ideas


async def test_pipeline_drops_product_subject_idea_before_scoring(monkeypatch):
    """End-to-end: an idea whose ANGLE names a BrandProfile product never reaches the final five."""
    from core.brand_profile import BrandProfile, ProductItem

    monkeypatch.setattr(
        "core.brand_profile.load_brand_profile",
        lambda *a, **k: BrandProfile(company_name="Acme", products=[ProductItem(id="pgpulse", name="PgPulse")]),
    )
    ideas = [
        _idea("database automation guardrails", kind="grounded",
              angle="PgPulse can automate checks, not schema decisions"),
        _idea("fresh grounded expansion", kind="grounded"),
        _idea("original one", kind="original"),
        _idea("original two", kind="original"),
        _idea("original three", kind="original"),
    ]
    recommendations = {
        "fresh grounded expansion": _rec(traffic_potential=200, impressions=80),
        "original one": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
        "original two": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
        "original three": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
    }
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC(inventory=[], recommendations=recommendations)

    result = await generate_keyword_ideas("database tools", gsc=fake_gsc, ai=fake_ai)

    keywords = [s["keyword"] for s in result["suggestions"]]
    assert "database automation guardrails" not in keywords
    assert "fresh grounded expansion" in keywords
    assert "database automation guardrails" not in fake_gsc.recommendation_calls


async def test_pipeline_drops_inventory_verbatim_grounded_idea():
    """End-to-end: a grounded idea whose keyword IS a site inventory query never
    reaches the final five — the exact live failure ('meta ads mcp' scoring 92
    and winning a grounded slot every run)."""
    inventory = [_row("meta ads mcp", impressions=4518, position=7.9)]
    ideas = [
        _idea("meta ads mcp", kind="grounded"),          # cannibalizes — must be dropped
        _idea("fresh grounded expansion", kind="grounded"),
        _idea("original one", kind="original"),
        _idea("original two", kind="original"),
        _idea("original three", kind="original"),
    ]
    recommendations = {
        "meta ads mcp": _rec(traffic_potential=900, impressions=4518),
        "fresh grounded expansion": _rec(traffic_potential=200, impressions=80),
        "original one": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
        "original two": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
        "original three": _rec(traffic_potential=0, impressions=0, gsc_status="no_data"),
    }
    fake_ai = _FakeAI(ideas=ideas)
    fake_gsc = _FakeGSC(inventory=inventory, recommendations=recommendations)

    result = await generate_keyword_ideas("attribution tools", gsc=fake_gsc, ai=fake_ai)

    keywords = [s["keyword"] for s in result["suggestions"]]
    assert "meta ads mcp" not in keywords
    assert "fresh grounded expansion" in keywords
    # The dropped idea was never even scored.
    assert "meta ads mcp" not in fake_gsc.recommendation_calls


async def test_ideate_grounded_rules_are_position_aware_and_forbid_verbatim_anchors():
    """The grounded prompt rules must encode the actual SEO opportunity logic:
    near-top positions are captured demand, weak-position impressions are the
    opportunity, and the idea's keyword is never the anchor query itself."""
    completions = _FakeCompletions([
        '{"ideas": [{"keyword": "k", "kind": "original"}]}',
    ])
    router = _stub_router(or_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    async def _unused(*args, **kwargs):
        raise AssertionError("primary tier should have handled this")

    router._call_gemini_with_fallback = _unused
    router._nebius_with_fallback = _unused

    await router.ideate_keywords(
        gsc_context="", icp_context="icp", seed="attribution")

    system_msg = next(m["content"] for m in completions.calls[0]["messages"] if m["role"] == "system")
    assert "position" in system_msg
    assert "real impressions at a weak position" in system_msg
    assert "never the anchor query itself or a trimmed version" in system_msg
    assert "no two grounded ideas may anchor on the same query" in system_msg


async def test_ideate_prompt_demands_coherent_single_claim_angles():
    """Angles must parse in one pass as ONE sensible claim — no pseudo-profound
    conditionals fusing two concepts ('X unless Y', 'the hidden cost of X is Y'),
    which read as nonsense in the idea list. Smart = instantly understood AND
    teaches something, never decode-me cleverness."""
    completions = _FakeCompletions([
        '{"ideas": [{"keyword": "k", "kind": "original"}]}',
    ])
    router = _stub_router(or_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    async def _unused(*args, **kwargs):
        raise AssertionError("primary tier should have handled this")

    router._call_gemini_with_fallback = _unused
    router._nebius_with_fallback = _unused

    await router.ideate_keywords(
        gsc_context="", icp_context="icp", seed="attribution")

    system_msg = next(m["content"] for m in completions.calls[0]["messages"] if m["role"] == "system")
    assert "ONE clear, plainly-worded claim" in system_msg
    assert "Coherence test" in system_msg
    assert "fuses two loosely related concepts" in system_msg
    # The formats list no longer suggests the 'hidden cost of X' aphorism shape.
    assert "hidden second-order costs" not in system_msg


async def test_ideate_prompt_seeds_no_brand_examples_and_varies_sentence_shape():
    """Seed vocabulary is self-fulfilling: the old angle definition enumerated
    '(Google Ads, Meta Ads, GA4, HubSpot, Salesforce, …)' as examples and every
    trends-fresh run produced Salesforce/Meta Ads ideas — Salesforce isn't even
    in the ICP config. Tool specificity must come from the ICP context and GSC
    queries, not prompt examples. And prescriptive rules ('X needs Y') are ONE
    sentence shape, capped at one per set."""
    completions = _FakeCompletions([
        '{"ideas": [{"keyword": "k", "kind": "original"}]}',
    ])
    router = _stub_router(or_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))

    async def _unused(*args, **kwargs):
        raise AssertionError("primary tier should have handled this")

    router._call_gemini_with_fallback = _unused
    router._nebius_with_fallback = _unused

    await router.ideate_keywords(
        gsc_context="", icp_context="icp", seed="attribution")

    system_msg = next(m["content"] for m in completions.calls[0]["messages"] if m["role"] == "system")
    # No enumerated brand examples anywhere in the prompt.
    assert "Salesforce" not in system_msg
    assert "HubSpot" not in system_msg
    assert "GA4" not in system_msg
    # Specificity is sourced from the run's own context instead.
    assert "ICP context" in system_msg
    # Prescriptive-rule shape is capped at one angle per set.
    assert "at most one angle in the set may use it" in system_msg
    assert '"X needs Y"' in system_msg


def test_default_trends_topic_reflects_brand_or_neutral_default():
    """Blank-steer fresh-trends uses BrandProfile when configured and neutral default otherwise."""
    from core.keyword_ideas import _default_trends_topic
    topic = _default_trends_topic()
    assert isinstance(topic, str) and len(topic) > 15


def test_previous_block_caps_prompt_slice_and_demands_new_anchors():
    """A deep accepted-ideas history rides in `previous` for exact dedupe, but
    the prompt only shows the most recent slice — and tells the model to pick
    different GSC anchors than previous ideas covered."""
    previous = [f"previous idea number {i}" for i in range(120)]
    prompt = SmartAIRouter._build_ideation_prompt(
        gsc_context="", icp_context="icp", trends_context="",
        seed=None, style=None, steer=None, previous=previous, count=12,
    )
    assert "previous idea number 39" in prompt
    assert "previous idea number 40" not in prompt
    assert "pick different anchors" in prompt
