"""Jev (core/jev.py) typed-decision integrations, with ``jev.ask`` stubbed.

Every integration must (a) act on Jev's probabilities when it answers and
(b) reproduce the pre-Jev behaviour when Jev is off or returns None.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import jev
from core.ai_router import SmartAIRouter
from core.generator import ContentGenerator
from core.gsc_analyzer import ContentRecommendation, GSCAnalyzer
from core.research import classify_keyword_intent, classify_keyword_intent_jev
from tests.test_internal_link_quality import _formatter

pytestmark = pytest.mark.unit


@pytest.fixture
def jev_on(monkeypatch):
    """Enable Jev with a stub ``ask``; set ``stub.answer = fn(state, questions)``."""
    monkeypatch.setenv("JEV", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    class Stub:
        calls = []
        answer = staticmethod(lambda state, questions: None)

    async def fake_ask(state, questions, session=None):
        Stub.calls.append((state, questions))
        return Stub.answer(state, questions)

    monkeypatch.setattr(jev, "ask", fake_ask)
    return Stub


def _nouls(**probs):
    return {name: {"type": "noul", "noul": p} for name, p in probs.items()}


def test_disabled_by_kill_switch_and_missing_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("JEV", "0")
    assert not jev.enabled()
    monkeypatch.setenv("JEV", "1")
    assert jev.enabled()
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert not jev.enabled()


# --- insight filter -------------------------------------------------------

def _router():
    router = SmartAIRouter.__new__(SmartAIRouter)
    router.models_used = {}
    router.model_light = "openai/gpt-6-luna"
    return router


async def test_insight_filter_keeps_original_strings_by_rule(jev_on):
    verdicts = {
        "good": _nouls(relevant=0.9, specific=0.8, promo=0.1),
        "vague": _nouls(relevant=0.9, specific=0.2, promo=0.1),
        "promo": _nouls(relevant=0.9, specific=0.9, promo=0.9),
        "offtopic": _nouls(relevant=0.1, specific=0.9, promo=0.1),
    }
    jev_on.answer = lambda state, q: verdicts[state["insight"].split()[0]]
    insights = ["good  detail, exact", "vague one", "promo spam", "offtopic hike", "good  detail, exact"]

    kept = await _router()._filter_with_jev(insights, "ad spend")

    assert kept == ["good  detail, exact"]  # original string, exact duplicate removed


async def test_insight_filter_returns_none_when_most_calls_fail(jev_on):
    jev_on.answer = lambda state, q: None
    assert await _router()._filter_with_jev(["a b", "c d", "e f"], "ad spend") is None


async def test_insight_filter_treats_incomplete_answers_as_unjudged(jev_on):
    # Majority incomplete ({}) -> falls back by returning None
    jev_on.answer = lambda state, q: {}
    assert await _router()._filter_with_jev(["a b", "c d", "e f"], "ad spend") is None

    # Minority incomplete ({}) -> kept as unjudged while scored items are filtered normally
    verdicts = {
        "good": _nouls(relevant=0.9, specific=0.8, promo=0.1),
        "partial": {"relevant": {"type": "noul", "noul": 0.9}},  # missing specific & promo
        "bad": _nouls(relevant=0.1, specific=0.1, promo=0.9),
    }
    jev_on.answer = lambda state, q: verdicts[state["insight"].split()[0]]
    kept = await _router()._filter_with_jev(["good one", "partial one", "bad one"], "ad spend")
    assert kept == ["good one", "partial one"]


# --- editorial audit ------------------------------------------------------

_ARTICLE = """---
title: Test
---

Intro sentence with enough words to be audited properly by the editorial pass here and now for sure okay.
It keeps going for a while so the section clears the forty word minimum easily enough today friends.

## Measuring CAC

In today's world, CAC matters. Customer acquisition cost is what you pay per customer across all channels and it
decides whether scaling spend is safe. Seventy percent of agencies say so. More words here to pass the minimum length.

## Using CAC later

CAC again, already explained above, so this section must not be asked about it a second time at all ever here.
Plenty of additional words to make this section long enough for the audit threshold to include it in the run.
"""


class _EditAI:
    def __init__(self):
        self.directive = None

    async def edit(self, content, progress_callback=None, user_directive=None):
        self.directive = user_directive
        return content + "\nedited"


def _generator(valid=True):
    gen = ContentGenerator.__new__(ContentGenerator)
    gen._validate_mdx_structure = lambda c: {"valid": valid}
    return gen


async def test_audit_flags_become_directive_and_acronym_checked_once(jev_on):
    def answer(state, questions):
        flagged = "Measuring CAC" in state["section"]
        out = _nouls(slop_opener=0.95 if flagged else 0.1, unsourced_stat=0.9 if flagged else 0.1, off_topic=0.1)
        out.update({q: {"type": "noul", "noul": 0.1} for q in questions if q.startswith("explains_")})
        return out
    jev_on.answer = answer
    ai = _EditAI()

    result = await _generator()._run_editorial_audit(_ARTICLE, ai, "customer acquisition cost")

    assert result["applied"] and result["content"].endswith("edited")
    assert list(result["flags"]) == ["Measuring CAC"]
    assert 'In "Measuring CAC"' in ai.directive and "never invent a source" in ai.directive
    assert "spell out or briefly explain CAC" in ai.directive
    cac_asks = [s for s, q in jev_on.calls if "explains_CAC" in q]
    assert len(cac_asks) == 1 and "Measuring CAC" in cac_asks[0]["section"]


async def test_audit_keeps_content_when_edit_breaks_mdx(jev_on):
    jev_on.answer = lambda state, q: _nouls(slop_opener=0.99, unsourced_stat=0.1, off_topic=0.1)
    result = await _generator(valid=False)._run_editorial_audit(_ARTICLE, _EditAI(), "cac")
    assert result["content"] == _ARTICLE and not result["applied"] and result["flags"]


async def test_audit_is_noop_when_jev_off():
    ai = _EditAI()
    result = await _generator()._run_editorial_audit(_ARTICLE, ai, "cac")
    assert result["content"] == _ARTICLE and ai.directive is None and not result["scores"]


# --- internal links -------------------------------------------------------

_LINK_TEXT = """
## Measurement

Postgres branching benchmarks help teams compare CI runtime and storage overhead. Schema migration strategies matter too for postgres branching.
"""


def test_links_unchanged_when_jev_returns_nothing(jev_on, monkeypatch):
    monkeypatch.setenv("JEV", "0")
    baseline = _formatter()._add_contextual_links(_LINK_TEXT, _formatter().link_opportunities)
    monkeypatch.setenv("JEV", "1")
    jev_on.answer = lambda state, q: None
    formatter = _formatter()
    assert formatter._add_contextual_links(_LINK_TEXT, formatter.link_opportunities) == baseline


def test_links_follow_jev_relevance(jev_on):
    jev_on.answer = lambda state, q: _nouls(
        same_thing=0.2 if "branching" in state["destination"].lower() else 0.9, useful=0.9)
    formatter = _formatter()
    linked = formatter._add_contextual_links(_LINK_TEXT, formatter.link_opportunities)
    assert "postgres-branching-benchmarks" not in linked
    assert "schema-migration-strategies" in linked


# --- keyword intent -------------------------------------------------------

def test_intent_patterns_match_whole_words_only():
    # 'app' used to substring-match "happens" and force navigational
    assert "app" not in classify_keyword_intent("what happens to unused ad budget")["matched_patterns"]


def _choice(intent, confidence):
    return {"intent": {"type": "choice", "choice": intent, "confidence": confidence}}


async def test_jev_intent_decides_when_no_pattern_matched(jev_on):
    jev_on.answer = lambda state, q: _choice("commercial", 0.7)
    assert (await classify_keyword_intent_jev("ad spend cards for agencies"))["intent_type"] == "commercial"


async def test_jev_intent_needs_high_confidence_to_override_pattern(jev_on):
    jev_on.answer = lambda state, q: _choice("informational", 0.87)
    assert (await classify_keyword_intent_jev("flyweel pricing"))["intent_type"] == "transactional"
    jev_on.answer = lambda state, q: _choice("informational", 0.97)
    assert (await classify_keyword_intent_jev("flyweel pricing"))["intent_type"] == "informational"


# --- cannibalization ------------------------------------------------------

def _analyzer(rows):
    analyzer = GSCAnalyzer.__new__(GSCAnalyzer)
    analyzer._site_url = "sc-domain:flyweel.co"

    async def ensure():
        return True

    async def execute(fn, name):
        return {"rows": rows}

    analyzer._ensure_property = ensure
    analyzer._execute = execute
    return analyzer


async def test_cannibalization_drops_substring_only_pages(jev_on):
    rows = [
        {"keys": ["https://flyweel.co/blog/crm-for-agencies", "best crm for agencies"], "impressions": 500, "clicks": 20, "position": 3},
        {"keys": ["https://flyweel.co/blog/crm-ad-attribution", "crm ad attribution"], "impressions": 300, "clicks": 5, "position": 14},
    ]
    jev_on.answer = lambda state, q: _nouls(same_search=0.1 if "agencies" in state["existing_page"] else 0.9)

    result = await _analyzer(rows).check_cannibalization("crm ad attribution")

    assert [p["url"] for p in result.existing_urls] == ["https://flyweel.co/blog/crm-ad-attribution"]
    assert result.recommendation != ContentRecommendation.ABORT  # the pos-3 page no longer counts


def test_lowercase_single_word_never_links_to_integration_page(jev_on, monkeypatch):
    monkeypatch.setenv("JEV", "0")
    formatter = _formatter()
    close = {"url": "https://flyweel.co/integrations/close", "title": "Close", "anchor_text": "close",
             "destination_tokens": ["close", "crm"], "term_tokens": ["close"], "priority": 10}
    text = "## Pipeline\n\nLeads take time to close. Teams on Close sync pipeline stages."
    linked = formatter._add_contextual_links(text, {"close": close}, require_keyword_overlap=False)
    assert "[close]" not in linked
