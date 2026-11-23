"""Unit tests for the output-quality harness and pipeline-hardening guards.

Covers:
- The three new AI-SEO / Princeton GEO citability dimensions + body banned words
- ContentValidator.build_scorecard composite + judge merge
- SmartAIRouter._safe_content None-guard and _evidence_signal_count
- content_scrubber over-deletion guard + in-place neutralization
- quality_judge JSON parsing
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.content_validator import ContentValidator
from core.content_scrubber import scrub
from core.ai_router import SmartAIRouter
from core.quality_judge import _parse_judge_json, DEFAULT_DIMENSIONS


GUIDE_MDX = """---
title: How to Reduce CAC
author: Jane Doe
publishDate: 2026-05-01
updatedDate: 2026-05-20
canonical: https://flyweel.co/blog/reduce-cac
seo:
  noindex: false
  nofollow: false
description: Reduce customer acquisition cost with a clear method.
---

## What Is CAC?

Customer acquisition cost measures spend per new customer. According to HubSpot,
companies that nurture leads see a 50% lift in sales-ready prospects.

## How to Reduce It

1. Audit every paid channel for wasted spend.
2. Score leads using behavioral data.
3. Reinvest into the channels with the lowest cost per lead.

A 37% improvement is realistic when you fix attribution. Costs near $40 per lead
are common in B2B.
"""

NATURAL_BODY = ("---\ntitle: t\n---\n\n## Section\n\n"
                + "Lead tracking matters. " + "Teams measure progress with clear stages and data. " * 60)


@pytest.mark.unit
class TestNewAISeoDimensions:
    def test_keyword_stuffing_flags_overuse(self):
        stuffed = "---\ntitle: t\n---\n\n" + ("best crm software " * 30)
        v = ContentValidator()
        r = v.validate_keyword_stuffing(stuffed, "best crm software")
        assert r.overall_score < 50

    def test_keyword_stuffing_passes_natural(self):
        v = ContentValidator()
        r = v.validate_keyword_stuffing(NATURAL_BODY, "lead tracking")
        assert r.overall_score >= 80

    def test_process_structure_requires_steps_for_guide(self):
        v = ContentValidator()
        no_steps = "---\ntitle: t\n---\n\n## How To\n\nJust prose, no numbered steps here.\n"
        r = v.validate_process_structure(no_steps, "guide")
        assert r.overall_score == 0

    def test_process_structure_rewards_steps_for_guide(self):
        v = ContentValidator()
        r = v.validate_process_structure(GUIDE_MDX, "guide")
        assert r.overall_score == 100

    def test_process_structure_lenient_for_non_guide(self):
        v = ContentValidator()
        no_steps = "---\ntitle: t\n---\n\n## Overview\n\nProse only.\n"
        r = v.validate_process_structure(no_steps, "comparison")
        assert r.overall_score == 100  # ordered steps optional for non-process styles

    def test_author_attribution_rewards_byline_and_experts(self):
        v = ContentValidator()
        r = v.validate_author_attribution(GUIDE_MDX, {"author": "Jane Doe"})
        assert r.overall_score >= 60  # byline (5) + >=1 expert attribution

    def test_author_attribution_penalizes_missing(self):
        v = ContentValidator()
        body = "---\ntitle: t\n---\n\nNo byline and no expert quotes anywhere.\n"
        r = v.validate_author_attribution(body, {})
        assert r.overall_score < 60

    def test_banned_words_body_docks_buzzwords(self):
        v = ContentValidator()
        body = "---\ntitle: t\n---\n\nWe leverage a robust, cutting-edge, holistic synergy.\n"
        r = v.validate_banned_words_body(body)
        assert r.overall_score < 100

    def test_banned_words_body_clean(self):
        v = ContentValidator()
        body = "---\ntitle: t\n---\n\nWe help teams cut costs with simple, clear steps.\n"
        r = v.validate_banned_words_body(body)
        assert r.overall_score == 100


@pytest.mark.unit
class TestScorecard:
    def test_build_scorecard_composite_and_dims(self):
        v = ContentValidator()
        card = v.build_scorecard(GUIDE_MDX, {"author": "Jane Doe"},
                                 keyword="reduce cac", style="guide", has_schema=False)
        assert isinstance(card["composite"], int)
        assert 0 <= card["composite"] <= 100
        for d in ["structure", "aeo_readiness", "keyword_stuffing",
                  "process_structure", "author_attribution", "banned_words_body"]:
            assert d in card["dimensions"]

    def test_judge_dims_merge_and_null_excluded(self):
        v = ContentValidator()
        judge = {"dimensions": {
            "clarity": {"score": 80, "rationale": "clear"},
            "fluency": {"score": None, "error": "parse_failure"},
        }}
        card = v.build_scorecard(GUIDE_MDX, {"author": "Jane Doe"},
                                 keyword="reduce cac", style="guide", judge=judge)
        assert card["dimensions"]["clarity"]["score"] == 80
        assert card["dimensions"]["fluency"]["score"] is None
        assert isinstance(card["composite"], int)  # null dim excluded, no crash


# ---- helpers for _safe_content tests (fake OpenAI-style response shapes) ----
class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, msg):
        self.message = msg


class _Resp:
    def __init__(self, choices):
        self.choices = choices


@pytest.mark.unit
class TestSafeContent:
    def test_none_content_returns_none(self):
        resp = _Resp([_Choice(_Msg(None))])
        assert SmartAIRouter._safe_content(resp) is None

    def test_valid_content_stripped(self):
        resp = _Resp([_Choice(_Msg("  hello  "))])
        assert SmartAIRouter._safe_content(resp) == "hello"

    def test_empty_string_returns_none(self):
        resp = _Resp([_Choice(_Msg("   "))])
        assert SmartAIRouter._safe_content(resp) is None

    def test_none_response(self):
        assert SmartAIRouter._safe_content(None) is None

    def test_accepts_message_directly(self):
        assert SmartAIRouter._safe_content(_Msg("hi")) == "hi"

    def test_empty_choices(self):
        assert SmartAIRouter._safe_content(_Resp([])) is None


@pytest.mark.unit
class TestEvidenceSignal:
    def test_counts_citations_and_stats(self):
        text = "Claim [1] with 50% lift and $40 cost and 3x growth and [link](https://x.co)."
        assert SmartAIRouter._evidence_signal_count(text) >= 5

    def test_empty_is_zero(self):
        assert SmartAIRouter._evidence_signal_count("") == 0


@pytest.mark.unit
class TestScrubberGuard:
    def test_over_deletion_flagged(self):
        # Almost the entire body is a single scaffolding line.
        mdx = "GENERATION SCOPE FOR THIS PART (Part 1 of 3): do the thing now please\n\nok\n"
        result = scrub(mdx)
        assert result.removed_ratio > 0.10
        assert result.over_deletion is True

    def test_normal_scrub_not_over_deletion(self):
        mdx = ("---\ntitle: t\n---\n\n## Section\n\n"
               + "This is a perfectly fine paragraph with real content. " * 20
               + "\n\nSECTION 3 of 7:\n")
        result = scrub(mdx)
        assert result.over_deletion is False

    def test_neutralize_keeps_advice(self):
        mdx = ("Body paragraph with enough words to matter here today friends. " * 5
               + "\n- **Pro hack:** Create a master folder structure for assets.\n"
               + "- Normal bullet.\n")
        result = scrub(mdx)
        assert "Pro hack" not in result.cleaned
        assert "Create a master folder structure for assets." in result.cleaned
        assert "Normal bullet" in result.cleaned


@pytest.mark.unit
class TestJudgeParse:
    def test_parse_with_code_fences(self):
        raw = ('```json\n{"dimensions":{"clarity":{"score":88,"rationale":"clear"},'
               '"fluency":{"score":80,"rationale":"ok"},'
               '"authoritative_tone":{"score":75,"rationale":"good"}}}\n```')
        parsed = _parse_judge_json(raw, DEFAULT_DIMENSIONS)
        assert parsed["clarity"]["score"] == 88
        assert parsed["fluency"]["score"] == 80

    def test_parse_missing_dim_marks_null(self):
        raw = '{"dimensions":{"clarity":{"score":90,"rationale":"x"}}}'
        parsed = _parse_judge_json(raw, DEFAULT_DIMENSIONS)
        assert parsed["clarity"]["score"] == 90
        assert parsed["fluency"]["score"] is None
        assert parsed["fluency"]["error"]

    def test_parse_garbage_returns_none(self):
        assert _parse_judge_json("not json at all", DEFAULT_DIMENSIONS) is None

    def test_parse_clamps_out_of_range(self):
        raw = '{"dimensions":{"clarity":{"score":150},"fluency":{"score":-5},"authoritative_tone":{"score":50}}}'
        parsed = _parse_judge_json(raw, DEFAULT_DIMENSIONS)
        assert parsed["clarity"]["score"] == 100
        assert parsed["fluency"]["score"] == 0
