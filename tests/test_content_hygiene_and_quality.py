"""Consolidated unit tests for AEO content validation, scrubber hygiene, CTA/FAQ sync, heading quality, and title quality."""
import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.ai_router import SmartAIRouter
from core.brand_profile import BrandProfile
from core.content_scrubber import _consolidate_source_tags, scrub
from core.content_validator import ContentValidator
from core.formatter import AstroFormatter
from core.generator import ContentGenerator
from core.mdx_security import assert_safe_mdx

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# AEO Content Validation Gates
# ---------------------------------------------------------------------------

def _atomic_answer(seed: str = "Attribution") -> str:
    return (
        f"{seed} works best when teams connect ad spend, CRM stages, and closed revenue in one reporting view. "
        "This gives marketers enough context to explain which campaigns create qualified pipeline instead of only clicks. "
        "Review the same metrics monthly so budget shifts follow revenue, not platform-level vanity numbers."
    )


class TestAEOValidation:
    def test_faq_count_and_answer_length_are_warnings(self):
        validator = ContentValidator()
        report = validator.validate_faq_answers([
            {"question": "What is attribution?", "answer": "Too short."}
        ])
        assert not report.errors
        assert any("FAQ Coverage" in warning for warning in report.warnings)
        assert any("Concise FAQ Answers" in warning for warning in report.warnings)

    def test_atomic_answers_query_headings_and_evidence_pass(self):
        validator = ContentValidator()
        content = f"""
## What is attribution?

{_atomic_answer('Attribution')} [1] HubSpot reported a 24% improvement in 2025.

| Metric | Manual | Automated |
|---|---:|---:|
| Time | 12 hours | 2 hours |
| Accuracy | 71% | 94% |

## How should teams measure it?

{_atomic_answer('Measurement')} [2] Salesforce reported $120K in influenced pipeline in 2025.

| Step | Owner | Output |
|---|---|---|
| Track | Marketing | UTMs |
| Match | Sales | CRM stages |
"""
        frontmatter = {
            "canonical": "https://www.acme.dev/blog/attribution",
            "seo": {"noindex": False, "nofollow": False},
            "faq": [{"question": f"Question {i}?", "answer": _atomic_answer()} for i in range(12)],
        }
        report = validator.validate_aeo_gates(content, frontmatter, has_schema=True)
        assert report.content_type == "aeo_gates"
        assert not report.errors
        assert any(check["name"] == "Atomic Answer Blocks" and check["passed"] for check in report.checks)
        assert any(check["name"] == "Query-Style Headings" and check["passed"] for check in report.checks)

    def test_duplicate_passages_are_warnings(self):
        validator = ContentValidator()
        repeated = "teams need one source of truth for ad spend crm stages and closed revenue attribution"
        content = f"## What matters?\n\n{_atomic_answer()}\n\n{repeated} {repeated}\n"
        frontmatter = {"canonical": "", "seo": {}, "faq": []}
        report = validator.validate_aeo_gates(content, frontmatter, has_schema=False)
        assert not report.errors and report.warnings
        assert any("Frontmatter Readiness" in warning for warning in report.warnings)

    def test_aeo_score_prioritizes_crawl_freshness_and_usefulness(self):
        validator = ContentValidator()
        content = """
## What is revenue attribution?

Revenue attribution connects ad spend to CRM stages and closed revenue so teams can compare performance with business outcomes. The method works best when reporting includes examples, benchmarks, limitations, and scenario analysis for each channel. In 2025, one case study showed 24% faster budget decisions and $120K in influenced pipeline. [1]

## How should teams use it?

Teams should review attribution monthly, compare campaign examples, and document trade-offs before shifting budget. A useful methodology covers data quality, attribution windows, and CRM matching rules. This creates source-backed analysis instead of repeating generic definitions. [2]
"""
        frontmatter = {
            "title": "Revenue Attribution Guide",
            "description": "A source-backed guide to revenue attribution.",
            "canonical": "https://www.acme.dev/blog/revenue-attribution",
            "publishDate": "2026-05-15",
            "updatedDate": "2026-05-17",
            "seo": {"noindex": False, "nofollow": False},
            "faq": [{"question": "What is revenue attribution?", "answer": _atomic_answer()}],
        }
        report = validator.score_aeo_readiness(content, frontmatter, has_schema=False, citation_coverage=1.0)
        assert any(check["name"] == "Crawl and Snippet Readiness" and check["passed"] for check in report.checks)
        assert any(check["name"] == "Freshness Signals" and check["passed"] for check in report.checks)
        assert any(check["name"] == "Structured Data Aid" and not check["passed"] for check in report.checks)


# ---------------------------------------------------------------------------
# Content Scrubber
# ---------------------------------------------------------------------------

class TestKnownLeakPatterns:
    def test_mine_community_forums_is_stripped(self):
        mdx = "## Tips\n\nMine Community Forums: Search Quora manually with terms related to your industry.\nSome other line.\n"
        result = scrub(mdx)
        assert "Mine Community Forums" not in result.cleaned
        assert any(v.pattern_name == "manual-template-bullet" for v in result.violations)

    def test_include_this_critical_is_stripped(self):
        mdx = "## The Manual Way Section (INCLUDE THIS - CRITICAL)\n\nbody\n"
        result = scrub(mdx)
        assert "INCLUDE THIS" not in result.cleaned and result.violations

    def test_highly_detailed_educational_is_stripped(self):
        mdx = "## Section (HIGHLY DETAILED - EDUCATIONAL GUIDE)\n\nbody\n"
        result = scrub(mdx)
        assert "HIGHLY DETAILED" not in result.cleaned and result.violations

    def test_total_manual_time_investment_is_stripped(self):
        mdx = "**TOTAL MANUAL TIME INVESTMENT: 15-23 hours per week**\n\nbody\n"
        result = scrub(mdx)
        assert "TOTAL MANUAL TIME" not in result.cleaned

    def test_pro_hack_bullet_is_stripped(self):
        mdx = (
            "Body paragraph here with two sentences. It is fine.\n"
            "- **Pro hack:** Create a master folder structure.\n"
            "- Normal bullet.\n"
        )
        result = scrub(mdx)
        assert "Pro hack" not in result.cleaned and "Normal bullet" in result.cleaned


class TestScrubberPassThroughAndScaffolding:
    def test_clean_content_is_unchanged(self):
        mdx = "---\ntitle: hello\n---\n\n## Section\n\nThis is a totally fine paragraph. It has two sentences.\n"
        result = scrub(mdx)
        assert result.violations == [] and "totally fine paragraph" in result.cleaned

    def test_empty_input_safe(self):
        result = scrub("")
        assert result.cleaned == "" and result.violations == []

    def test_section_n_of_n_marker_is_stripped(self):
        mdx = "## Intro\n\nBody paragraph.\n\nSECTION 7 of 7:\n## FAQ: Your Top Questions Answered\n\nAnswer.\n"
        result = scrub(mdx)
        assert "SECTION 7 of 7" not in result.cleaned
        assert "## FAQ: Your Top Questions Answered" in result.cleaned
        assert any(v.pattern_name == "section-marker" for v in result.violations)

    def test_part_n_of_n_marker_is_stripped(self):
        result = scrub("Body.\n\nPART 2 of 3:\n\nMore body.\n")
        assert "PART 2 of 3" not in result.cleaned
        assert any(v.pattern_name == "section-marker" for v in result.violations)

    def test_generation_scope_directive_is_stripped(self):
        mdx = "GENERATION SCOPE FOR THIS PART (Part 1 of 3):\n\nReal content.\n"
        result = scrub(mdx)
        assert "GENERATION SCOPE" not in result.cleaned and "Real content." in result.cleaned


class TestSourceTagConsolidationAndSpelling:
    def test_source_tags_removed_from_table_cells(self):
        mdx = (
            "---\ntitle: t\n---\n\n"
            "| Finding | Metric | Confidence |\n"
            "|---|---|---|\n"
            "| Local CPL | $40-$120 **[Digitalapplied, 2026]** | OBSERVATIONAL |\n"
        )
        result = scrub(mdx)
        assert "[Digitalapplied, 2026]" not in result.cleaned
        assert "$40-$120" in result.cleaned and "OBSERVATIONAL" in result.cleaned

    def test_prose_duplicates_deduped_per_paragraph(self):
        mdx = (
            "First claim [Acme, 2025] and a second claim [Acme, 2025] here.\n\n"
            "A new paragraph cites [Acme, 2025] again afresh.\n"
        )
        result = scrub(mdx)
        assert result.cleaned.count("[Acme, 2025]") == 2
        assert "First claim [Acme, 2025] and a second claim here." in result.cleaned

    def test_numbered_citation_refs_untouched(self):
        mdx = 'A claim <a href="#cite-8" class="citation-ref">[8]</a> stands.\n'
        result = scrub(mdx)
        assert '<a href="#cite-8" class="citation-ref">[8]</a>' in result.cleaned

    def test_frontmatter_not_touched_by_source_pass(self):
        mdx = "---\ntitle: 'A study, 2025 review'\n---\n\nBody claim [Foo, 2025] and again [Foo, 2025].\n"
        result = scrub(mdx)
        assert "title: 'A study, 2025 review'" in result.cleaned
        assert result.cleaned.count("[Foo, 2025]") == 1

    def test_strict_fail_property_reflects_violations(self):
        assert scrub("**TOTAL MANUAL TIME INVESTMENT: 1 hour**\n", strict=True).strict_fail is True
        assert scrub("## OK\n\nNormal paragraph. Two sentences here.\n", strict=True).strict_fail is False

    def test_uk_spelling_centred_and_inflections(self):
        mdx = "The programme is centred on data centres and centring customer behaviour.\n"
        result = scrub(mdx)
        assert "program is centered on data centers and centering customer behavior." in result.cleaned
        assert "centerd" not in result.cleaned


# ---------------------------------------------------------------------------
# Brand-Agnostic Formatter CTA & FAQ Frontmatter Sync
# ---------------------------------------------------------------------------

ACME_PROFILE = BrandProfile(
    company_name="Acme Cloud",
    site_url="https://acme.dev",
    signup_or_cta_url="https://acme.dev/signup",
    cta_label="Start Free",
    category="Developer Tools",
    icp_priority_topics=["postgres branching", "preview environments"],
)

BUTTON = '<Button variant="figma-v1" href="https://acme.dev/signup" icon="pixelarticons:trending-up">Start Free</Button>'
IMPORT = "import Button from '@/components/ui/Button.astro';"
RAW_CTA_DOC = (
    "Intro paragraph about spinning up isolated preview databases.\n\n"
    "## How It Works\n\nSome body text here.\n\n"
    "## Conclusion\n\nWrap up.\n"
)


def _format_cta(mode="full", keyword="how to branch postgres databases", profile=ACME_PROFILE):
    return AstroFormatter(flyweel_mode=mode, brand_profile=profile).format(
        content=RAW_CTA_DOC, keyword=keyword, style="standard", research_data={}
    )["content"]


def test_configured_brand_gets_cta_button_before_conclusion_and_passes_security():
    mdx = _format_cta()
    assert mdx.count(BUTTON) == 1 and IMPORT in mdx
    assert mdx.index(BUTTON) < mdx.index("## Conclusion")
    assert_safe_mdx(mdx)


def test_none_mode_and_unconfigured_brand_get_no_cta():
    assert "<Button" not in _format_cta(mode="none")
    assert "<Button" not in _format_cta(mode="full", profile=BrandProfile())


def test_signup_cta_is_idempotent():
    formatter = AstroFormatter(brand_profile=ACME_PROFILE)
    once = formatter._add_signup_cta(RAW_CTA_DOC)
    assert formatter._add_signup_cta(once) == once and once.count("<Button") == 1


def test_primary_tag_is_brand_and_keyword_driven():
    formatter = AstroFormatter(brand_profile=ACME_PROFILE)
    tags = formatter._generate_tags("postgres branching for preview environments", "", "guide")
    assert "Developer Tools" in tags and "Postgres Branching" in tags and "Guide" in tags


def test_ensure_signup_cta_restores_button_and_import_dropped_by_llm_passes():
    mdx = _format_cta()
    stripped = mdx.replace(BUTTON, "").replace(IMPORT + "\n", "")
    fixed = AstroFormatter(flyweel_mode="full", brand_profile=ACME_PROFILE).ensure_signup_cta(stripped)
    assert fixed.count(BUTTON) == 1 and fixed.count(IMPORT) == 1
    assert fixed.index(BUTTON) < fixed.index("## Conclusion")
    assert_safe_mdx(fixed)


def test_scrubber_keeps_leading_indent():
    out, _ = _consolidate_source_tags("- a\n  - nested  item\n<div>\n  " + BUTTON + "\n</div>")
    assert "  - nested item" in out and "  " + BUTTON in out


def test_fallback_title_and_meta_prefer_draft_frontmatter():
    draft = (
        '---\ntitle: "How to Branch Postgres Databases Safely"\n'
        'description: "Learn how to branch Postgres databases safely in CI, isolate pull-request '
        'migrations, and tear down ephemeral preview instances automatically."\n---\n\nBody text.\n'
    )
    f, kw = AstroFormatter(brand_profile=ACME_PROFILE), "branch postgres databases"
    assert f._generate_title(draft, kw, "guide") == "How to Branch Postgres Databases Safely"
    assert f._generate_meta_description(draft, kw, "guide").startswith("Learn how to branch Postgres")


AUDIT_MDX = (
    '---\ntitle: "T"\nfaq:\n  - question: \'Wrong?\'\n    answer: \'Unrelated body paragraph.\'\n'
    "tldr:\n  answer: 'x'\n---\n\n" + IMPORT + "\n\nIntro.\n\n## Body\n\nText.\n\n"
    "## Frequently Asked Questions\n\n### How fast is branching?\n\n**Under 1 second.** Copy-on-write shares blocks.\n\n"
    "### Does it duplicate storage?\n\n**No.** Only mutated pages consume space.\n\n"
    "## Next Steps\n\nClose.\n\n"
    '<div class="not-prose mt-4">\n  ' + BUTTON + "\n</div>\n"
)


def test_ensure_signup_cta_moves_stranded_button_before_faq():
    mdx = AstroFormatter(brand_profile=ACME_PROFILE).ensure_signup_cta(AUDIT_MDX)
    assert mdx.count("<Button") == 1 and mdx.count(IMPORT) == 1
    assert mdx.index("<Button") < mdx.index("## Next Steps")
    assert_safe_mdx(mdx)


def test_sync_faq_frontmatter_mirrors_and_deduplicates_body_faq():
    mdx = AstroFormatter.sync_faq_frontmatter(AUDIT_MDX)
    fm = yaml.safe_load(re.match(r"---\n(.*?)\n---", mdx, re.S).group(1))
    assert fm["faq"] == [
        {"question": "How fast is branching?", "answer": "Under 1 second. Copy-on-write shares blocks."},
        {"question": "Does it duplicate storage?", "answer": "No. Only mutated pages consume space."},
    ]


# ---------------------------------------------------------------------------
# Heading & Title Quality
# ---------------------------------------------------------------------------

SENTENCE_KEYWORD_A = "Response Times Drop Fastest When Support Queues Stay Hidden"
SENTENCE_KEYWORD_B = "Revenue Leaks Fastest When Hand-Offs Take Too Long"


def _mdx_with_heading(heading: str) -> str:
    return f"""---
title: "Queue Metrics Test"
description: "Testing heading quality."
publishDate: "2026-07-05"
author: "Editorial Team"
---

{heading}

Hidden queues make response time reporting look healthier than the customer experience really is. The section explains why teams need queue-level visibility before changing staffing.
"""


def test_heading_quality_fails_sentence_like_raw_keyword_wrapper():
    validator = ContentValidator()
    report = validator.validate_heading_quality(
        _mdx_with_heading("## What is Response Times Drop Fastest Support Queues Stay Hidden?"),
        SENTENCE_KEYWORD_A,
    )
    assert not report.checks[0]["passed"]
    assert "mechanical raw-keyword wrapper" in report.warnings[0]


def test_heading_quality_fails_generic_sentence_like_wrapper():
    validator = ContentValidator()
    report = validator.validate_heading_quality(
        _mdx_with_heading("## What is Revenue Leaks Fastest Hand-Offs Take Too Long?"),
        SENTENCE_KEYWORD_B,
    )
    assert not report.checks[0]["passed"]
    assert "mechanical raw-keyword wrapper" in report.warnings[0]


def test_heading_quality_allows_smart_sentence_keyword_variant():
    validator = ContentValidator()
    report = validator.validate_heading_quality(_mdx_with_heading("## Where Hidden Queues Slow Responses"), SENTENCE_KEYWORD_A)
    assert report.checks[0]["passed"] and report.overall_score == 100


def test_heading_quality_allows_semantic_heading_without_exact_body_tokens():
    validator = ContentValidator()
    content = _mdx_with_heading("## What Future Trends Should You Prepare For?").replace(
        "Hidden queues make response time reporting look healthier than the customer experience really is. "
        "The section explains why teams need queue-level visibility before changing staffing.",
        "AI-mediated search and stricter privacy rules will reshape paid acquisition during 2027.",
    )
    report = validator.validate_heading_quality(content)
    assert report.checks[0]["passed"] and report.overall_score == 100


def test_scorecard_includes_heading_quality_dimension():
    validator = ContentValidator()
    content = _mdx_with_heading("## Where Hidden Queues Slow Responses")
    frontmatter = {"title": "Queue Metrics Test", "description": "Testing heading quality.", "publishDate": "2026-07-05", "author": "Editorial Team"}
    scorecard = validator.build_scorecard(content, frontmatter, keyword=SENTENCE_KEYWORD_A, style="standard")
    assert scorecard["dimensions"]["heading_quality"]["score"] == 100


def test_generator_heading_intent_prompt_allows_variants_not_forcing_exact_match():
    generator = ContentGenerator(site_context={"features": [], "internal_links": {}})
    generator._keyword_context = "The reader wants to know where delays appear when ownership hand-offs are too slow."
    serp = {"paa_questions": ["How do slow handoffs create delays?"]}
    prompt = generator._format_heading_keyword_intent_prompt(SENTENCE_KEYWORD_B, serp)
    standard_prompt = generator._get_standard_prompt(
        SENTENCE_KEYWORD_B,
        {
            "serp": {"paa_questions": [], "content_gaps": [], "recommended_length": 2000},
            "reddit": {"insights": []},
            "quora": {"expert_insights": []},
            "site_context": {"features": [], "key_differentiators": []},
            "topical_insights": {},
        },
    )
    assert "not a phrase that must be forced" in prompt
    assert "exact keyword, partial match, synonym" in prompt
    assert "Use clever keyword variants" in standard_prompt


@pytest.mark.asyncio
async def test_heading_optimizer_repairs_sentence_like_keyword_wrapper(monkeypatch):
    router = SmartAIRouter()
    router.or_client = SimpleNamespace()
    calls = []

    async def fake_heading_editor(messages, **kwargs):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return SimpleNamespace(content="## What is Response Times Drop Fastest Support Queues Stay Hidden?\n## Why Queues Hide the Real Delay")
        return SimpleNamespace(content="## Where Hidden Queues Slow Responses\n## Why Queues Hide the Real Delay")

    monkeypatch.setattr(router, "_call_heading_editor", fake_heading_editor)
    content = _mdx_with_heading("## What Queue Metrics Miss") + "\n## Why Queues Hide the Real Delay\n\nQueue averages hide outliers.\n"
    result = await router._optimize_headings_seo(content, SENTENCE_KEYWORD_A)
    assert len(calls) == 2 and "## Where Hidden Queues Slow Responses" in result


@pytest.mark.asyncio
async def test_heading_optimizer_keeps_original_when_repair_still_bad(monkeypatch):
    router = SmartAIRouter()
    router.or_client = SimpleNamespace()

    async def fake_heading_editor(messages, **kwargs):
        return SimpleNamespace(content="## What is Response Times Drop Fastest Support Queues Stay Hidden?")

    monkeypatch.setattr(router, "_call_heading_editor", fake_heading_editor)
    content = _mdx_with_heading("## What Queue Metrics Miss")
    result = await router._optimize_headings_seo(content, SENTENCE_KEYWORD_A)
    assert "## What Queue Metrics Miss" in result


def _content_with_frontmatter(title: str, seo_title: Optional[str] = None) -> str:
    seo_block = f"seo:\n  title: '{seo_title}'\n" if seo_title is not None else ""
    return f"""---
title: '{title}'
description: 'Hidden support queues make response time reporting look healthier than the customer experience really is.'
publishDate: '2026-07-05'
author: 'Editorial Team'
{seo_block}---

## Where Hidden Queues Slow Responses

Hidden queues make response time reporting look healthier than the customer experience really is. Teams need queue-level visibility before changing staffing.
"""


def test_title_quality_fails_raw_keyword_wrapper_and_fragment():
    validator = ContentValidator()
    wrapper = validator.validate_title_quality(
        _content_with_frontmatter("What is Response Times Drop Fastest Support Queues Stay Hidden?"),
        {"title": "What is Response Times Drop Fastest Support Queues Stay Hidden?"},
        SENTENCE_KEYWORD_A,
        "standard",
    )
    fragment = validator.validate_title_quality(
        _content_with_frontmatter("The Support Dashboard Is Dead; The Agentic API Is The"),
        {"title": "The Support Dashboard Is Dead; The Agentic API Is The"},
        "The support dashboard is dead; the agentic API is the operating layer",
        "standard",
    )
    assert not wrapper.checks[0]["passed"] and "mechanical raw-keyword title wrapper" in wrapper.warnings[0]
    assert not fragment.checks[0]["passed"]


def test_title_quality_allows_smart_variants_and_tracks_scorecard():
    validator = ContentValidator()
    content = _content_with_frontmatter("Where Hidden Queues Slow Support Responses", "Where Hidden Queues Slow Support Responses")
    frontmatter = {
        "title": "Where Hidden Queues Slow Support Responses",
        "description": "Hidden support queues make response time reporting look healthier than the customer experience really is.",
        "seo": {"title": "Where Hidden Queues Slow Support Responses"},
    }
    report = validator.validate_title_quality(content, frontmatter, SENTENCE_KEYWORD_A, "standard")
    scorecard = validator.build_scorecard(content, frontmatter, keyword=SENTENCE_KEYWORD_A, style="standard")
    assert report.checks[0]["passed"] and scorecard["dimensions"]["title_quality"]["score"] == 100


def test_formatter_preserves_sentence_like_keyword_and_canonical_casing():
    formatter = AstroFormatter()
    title = formatter._generate_title("", "api errors rise when crm handoffs stay hidden", "standard")
    meta = formatter._generate_meta_description(
        "Teams miss API errors when CRM handoffs happen outside the reporting workflow. The fix starts with ownership-level visibility.",
        "api errors rise when crm handoffs stay hidden",
        style="standard",
    )
    assert "What is" not in title and "API" in title and "CRM" in title
    assert "Discover" not in meta and "API" in meta and "CRM" in meta


@pytest.mark.asyncio
async def test_title_optimizer_repairs_bad_candidate_and_syncs_seo_title(monkeypatch):
    router = SmartAIRouter()
    router.or_client = SimpleNamespace()
    calls = []

    async def fake_title_editor(messages, **kwargs):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return SimpleNamespace(content="What is Response Times Drop Fastest Support Queues Stay Hidden?")
        return SimpleNamespace(content="Where Hidden Queues Slow Support Responses")

    monkeypatch.setattr(router, "_call_title_editor", fake_title_editor)
    content = _content_with_frontmatter("Response Times Drop Fastest When Support Queues Stay Hidden", "Old SEO Title")
    result = await router.optimize_title_seo(content, SENTENCE_KEYWORD_A, style="standard")
    assert len(calls) == 2 and 'title: "Where Hidden Queues Slow Support Responses"' in result


@pytest.mark.asyncio
async def test_title_optimizer_keeps_original_when_repair_still_bad(monkeypatch):
    router = SmartAIRouter()
    router.or_client = SimpleNamespace()

    async def fake_title_editor(messages, **kwargs):
        return SimpleNamespace(content="The Support Dashboard Is Dead; The Agentic API Is The")

    monkeypatch.setattr(router, "_call_title_editor", fake_title_editor)
    original = _content_with_frontmatter("Support Queue Visibility Before Staffing Changes", "Support Queue Visibility Before Staffing Changes")
    assert await router.optimize_title_seo(original, SENTENCE_KEYWORD_A, style="standard") == original


def test_title_prompt_is_article_type_aware_and_teaches_colon_patterns(monkeypatch):
    router = SmartAIRouter()
    router.or_client = SimpleNamespace()
    captured = {}

    async def fake_title_editor(messages, **kwargs):
        captured["prompt"] = messages[-1]["content"]
        return SimpleNamespace(content="12 CRM Handoff Tools for Cleaner Attribution")

    monkeypatch.setattr(router, "_call_title_editor", fake_title_editor)
    content = _content_with_frontmatter("CRM Handoff Tools", "CRM Handoff Tools")
    asyncio.run(router.optimize_title_seo(content, "crm handoff tools", style="comparison", gsc_primary_keyword="support queue response time"))
    assert "ARTICLE TYPE / STYLE: comparison" in captured["prompt"]
    assert "STRUCTURE PATTERNS" in captured["prompt"]
    assert "a strong signal, not a command" in captured["prompt"]


def test_shorten_title_phrase_preserves_colon_structure():
    formatter = AstroFormatter()
    assert formatter._shorten_title_phrase("Best CRM Handoff Tools for Revenue Teams (2026 Reviewed and Tested)", 60) == "Best CRM Handoff Tools for Revenue Teams"
    shortened = formatter._shorten_title_phrase("Support Queue Benchmarks: Where Response Time Breaks Down For Growing Teams", 60)
    assert shortened.startswith("Support Queue Benchmarks: Where Response Time") and len(shortened) <= 60
