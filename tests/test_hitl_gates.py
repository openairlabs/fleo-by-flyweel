"""Consolidated unit tests for Human-in-the-Loop (HITL) pre-polish review gate and GSC opportunity score gate."""
import inspect
from pathlib import Path

import pytest

from core.generator import (
    ContentGenerator,
    GSC_GATE_ESTIMATED_THRESHOLD_DEFAULT,
    GSC_GATE_THRESHOLD_DEFAULT,
)
from core.gsc_analyzer import normalize_traffic_potential_score

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Pre-Polish HITL Review Gate
# ---------------------------------------------------------------------------

def valid_mdx(body: str = "") -> str:
    filler = body or ("This paragraph has enough words to satisfy the body-length check. " * 18)
    return f"""---
title: "Review Gate Test"
publishDate: "2026-06-22"
description: "A focused test for the review gate."
author: "Editorial Team"
---

{filler}
"""


def test_review_gate_is_after_citations_and_before_polish_steps():
    source = Path("core/generator.py").read_text(encoding="utf-8")
    citation_verify = source.index("citation_verification = await self._verify_citations")
    citation_fix = source.index("corrected_content = await self._auto_correct_citations")
    review_gate = source.index("review_gate_result = await self._run_pre_polish_review_gate")
    polish_frontmatter = source.index("frontmatter_polished = await ai.polish_frontmatter")
    polish_body = source.index("polished_content = await ai.polish_content")
    heading_seo = source.index("Optimizing headings for SEO/AEO")
    title_seo = source.index("polished_final = await ai.optimize_title_seo")
    tldr = source.index("Generating TLDR")

    assert citation_verify < review_gate and citation_fix < review_gate
    assert review_gate < polish_frontmatter < polish_body < heading_seo < title_seo < tldr


class FakeReviewAi:
    def __init__(self, edited: str):
        self.edited = edited
        self.calls = []

    async def edit(self, content, progress_callback=None, user_directive=None):
        self.calls.append({"content": content, "user_directive": user_directive})
        return self.edited


@pytest.mark.asyncio
async def test_review_gate_approve_skips_comment_edit():
    generator = ContentGenerator(site_context={})
    ai = FakeReviewAi(valid_mdx("Edited content should not be used. " * 20))
    original = valid_mdx()
    result = await generator._run_pre_polish_review_gate(
        content=original,
        ai=ai,
        keyword="review gate",
        style="standard",
        citation_verification={"verification_status": "verified"},
        review_gate_callback=lambda payload: {"decision": "approve", "comments": ["unused"]},
    )
    assert result["content"] == original and result["applied_comments"] is False and ai.calls == []


@pytest.mark.asyncio
async def test_review_gate_applies_queued_comments_with_targeted_edit():
    generator = ContentGenerator(site_context={})
    edited = valid_mdx("The revised article keeps MDX valid and adds a concrete example. " * 16)
    ai = FakeReviewAi(edited)
    result = await generator._run_pre_polish_review_gate(
        content=valid_mdx(),
        ai=ai,
        keyword="review gate",
        style="standard",
        citation_verification={"verification_status": "verified"},
        review_gate_callback=lambda payload: {
            "decision": "apply_comments",
            "comments": ["Add a concrete example", "Keep the citations unchanged"],
        },
    )
    assert result["content"] == edited and result["applied_comments"] is True
    assert "Add a concrete example" in ai.calls[0]["user_directive"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["add_final_comment", "rerun_comment_edit"])
async def test_review_gate_applies_final_and_rerun_comment_decisions(decision):
    generator = ContentGenerator(site_context={})
    edited = valid_mdx("The revised article applies the final gate comment safely. " * 16)
    ai = FakeReviewAi(edited)
    result = await generator._run_pre_polish_review_gate(
        content=valid_mdx(),
        ai=ai,
        keyword="review gate",
        style="standard",
        citation_verification={"verification_status": "verified"},
        review_gate_callback=lambda payload: {
            "decision": decision,
            "comments": ["Queued comment"],
            "comment": "Final gate comment",
        },
    )
    assert result["content"] == edited and result["applied_comments"] is True
    assert "Queued comment" in ai.calls[0]["user_directive"] and "Final gate comment" in ai.calls[0]["user_directive"]


@pytest.mark.asyncio
async def test_review_gate_invalid_comment_edit_falls_back_to_original():
    generator = ContentGenerator(site_context={})
    original = valid_mdx()
    ai = FakeReviewAi("not valid mdx")
    result = await generator._run_pre_polish_review_gate(
        content=original,
        ai=ai,
        keyword="review gate",
        style="standard",
        citation_verification={"verification_status": "verified"},
        review_gate_callback=lambda payload: {"decision": "apply_comments", "comments": ["Rewrite everything"]},
    )
    assert result["content"] == original and result["applied_comments"] is False and "broke MDX" in result["warning"]


@pytest.mark.asyncio
async def test_review_gate_cancel_stops_generation_path():
    generator = ContentGenerator(site_context={})
    ai = FakeReviewAi(valid_mdx("Edited content should not be used. " * 20))
    result = await generator._run_pre_polish_review_gate(
        content=valid_mdx(),
        ai=ai,
        keyword="review gate",
        style="standard",
        citation_verification={"verification_status": "verified"},
        review_gate_callback=lambda payload: {"decision": "cancel"},
    )
    assert result["cancelled"] is True and ai.calls == []


# ---------------------------------------------------------------------------
# GSC Opportunity Score Gate
# ---------------------------------------------------------------------------

def test_normalize_traffic_potential_score_scale():
    assert normalize_traffic_potential_score(0) == 0
    assert normalize_traffic_potential_score(-5) == 0
    assert normalize_traffic_potential_score(1000) == 100
    assert normalize_traffic_potential_score(50_000) == 100
    assert normalize_traffic_potential_score(5) >= GSC_GATE_THRESHOLD_DEFAULT
    assert normalize_traffic_potential_score(4) < GSC_GATE_THRESHOLD_DEFAULT
    scores = [normalize_traffic_potential_score(tp) for tp in (0, 1, 5, 20, 100, 500, 1000)]
    assert scores == sorted(scores)


def test_estimated_threshold_default_is_stricter():
    assert GSC_GATE_ESTIMATED_THRESHOLD_DEFAULT == 10
    assert GSC_GATE_ESTIMATED_THRESHOLD_DEFAULT < GSC_GATE_THRESHOLD_DEFAULT
    assert normalize_traffic_potential_score(1) >= GSC_GATE_ESTIMATED_THRESHOLD_DEFAULT
    assert normalize_traffic_potential_score(0.9) < GSC_GATE_ESTIMATED_THRESHOLD_DEFAULT


def test_generate_signature_defaults_for_estimation():
    params = inspect.signature(ContentGenerator.generate).parameters
    assert params["gsc_gate_estimated_threshold"].default == 10
    assert params["gsc_gate_threshold"].default == GSC_GATE_THRESHOLD_DEFAULT
    assert params["depth"].default == "standard"


def test_gsc_score_gate_and_no_data_branches_in_generator():
    source = Path("core/generator.py").read_text(encoding="utf-8")
    recommendations = source.index("gsc_recommendations = await gsc.get_content_recommendations")
    score_task = source.index('self.reporter.start_task("gsc", "GSC Score"')
    score_gate = source.index("gate_decision = await self._run_gsc_score_gate")
    keyword_map = source.index('self.reporter.start_task("gsc", "Keyword Map"')
    assert recommendations < score_task < score_gate < keyword_map

    no_data_branch = source.index("if not has_gsc_data and not gsc_status.startswith('error')")
    estimate_call = source.index("est = await gsc.estimate_new_topic_opportunity(keyword, est_ai)")
    score_calc = source.index("gsc_score = normalize_traffic_potential_score(traffic_potential)")
    assert no_data_branch < estimate_call < score_calc


@pytest.mark.asyncio
async def test_gsc_gate_decisions_and_error_resilience():
    generator = ContentGenerator(site_context={})
    seen = {}

    async def abort_cb(payload):
        seen.update(payload)
        return {"decision": "abort"}

    decision = await generator._run_gsc_score_gate(
        keyword="low volume keyword",
        score=12,
        threshold=25,
        traffic_potential=1.5,
        primary_query="low volume keyword ideas",
        impressions=30,
        gsc_gate_callback=abort_cb,
        estimated=True,
        neighbor_count=7,
    )
    assert decision == "abort" and seen["estimated"] is True and seen["neighbor_count"] == 7

    proceed = await generator._run_gsc_score_gate(
        keyword="kw", score=10, threshold=25, traffic_potential=0.8, primary_query="kw", impressions=10,
        gsc_gate_callback=lambda payload: {"decision": "proceed"},
    )
    assert proceed == "proceed"

    def broken(_payload):
        raise RuntimeError("gate exploded")

    for cb in (broken, lambda _p: "not a dict", lambda _p: {"decision": "maybe"}):
        assert await generator._run_gsc_score_gate(
            keyword="kw", score=10, threshold=25, traffic_potential=0.8, primary_query="kw", impressions=10, gsc_gate_callback=cb,
        ) == "proceed"


def test_keyword_map_backfills_inferred_secondaries():
    source = Path("core/generator.py").read_text(encoding="utf-8")
    keyword_map = source.index('self.reporter.start_task("gsc", "Keyword Map"')
    curate = source.index("map_ai.curate_secondary_keywords(keyword, neighbors)")
    select = source.index("select_inferred_secondaries(")
    inferred_detail = source.index("related queries (inferred)")
    assert keyword_map < curate < select < inferred_detail
    assert "MUST use EXACT in title" not in source
