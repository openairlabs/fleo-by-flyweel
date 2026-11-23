"""Consolidated unit tests for GSC boundary/inspection, SchemaGenerator, proper nouns, slugs, and brand positioning."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.brand_profile import BrandProfile
from core.formatter import AstroFormatter, SLUG_MAX_CHARS, build_seo_slug, restamp_slug
from core.generator import ContentGenerator
from core.gsc_analyzer import GSCAnalyzer
from core.schema_generator import SchemaGenerator

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# GSC Boundary & URL Inspection
# ---------------------------------------------------------------------------

def _analyzer():
    analyzer = GSCAnalyzer.__new__(GSCAnalyzer)
    analyzer._credentials = None
    return analyzer


def test_gsc_response_normalizes_numeric_fields_once_at_boundary():
    response = {
        "rows": [
            {"keys": ["query"], "clicks": "12.9", "impressions": 44.0, "ctr": "0.25", "position": "8.5"},
            {"keys": ["bad"], "clicks": None, "impressions": "bad", "ctr": None, "position": "bad"},
        ]
    }
    normalized = GSCAnalyzer._normalize_response(response)
    assert normalized["rows"][0] == {
        "keys": ["query"], "clicks": 12, "impressions": 44, "ctr": 0.25, "position": 8.5,
    }
    assert normalized["rows"][1] == {
        "keys": ["bad"], "clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0,
    }
    assert response["rows"][0]["clicks"] == "12.9"


@pytest.mark.asyncio
async def test_gsc_execute_has_a_transport_deadline(monkeypatch):
    analyzer = _analyzer()
    analyzer.transport_timeout_seconds = 0.01

    async def never_finishes(_callable):
        await asyncio.sleep(10)

    monkeypatch.setattr(asyncio, "to_thread", never_finishes)
    with pytest.raises(asyncio.TimeoutError):
        await analyzer._execute(lambda: None, "deadline-test")


def test_http_transport_connections_are_closed():
    closed = []

    class Connection:
        def close(self):
            closed.append(True)

    transport = SimpleNamespace(connections={"one": Connection(), "two": Connection()})
    GSCAnalyzer._close_http_connections(transport)
    assert closed == [True, True]
    assert transport.connections == {}


class _FakeExecute:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class _FakeIndex:
    def __init__(self, response):
        self.response = response
        self.body = None

    def inspect(self, body):
        self.body = body
        return _FakeExecute(self.response)


class _FakeUrlInspection:
    def __init__(self, index):
        self._index = index

    def index(self):
        return self._index


class _FakeService:
    def __init__(self, index):
        self.index = index

    def urlInspection(self):
        return _FakeUrlInspection(self.index)


@pytest.mark.asyncio
async def test_inspect_url_maps_google_response():
    response = {
        "inspectionResult": {
            "inspectionResultLink": "https://search.google.com/search-console/inspect",
            "indexStatusResult": {
                "verdict": "PASS",
                "coverageState": "Submitted and indexed",
                "robotsTxtState": "ALLOWED",
                "pageFetchState": "SUCCESSFUL",
                "googleCanonical": "https://www.acme.dev/blog/test",
                "userCanonical": "https://www.acme.dev/blog/test",
                "lastCrawlTime": "2026-05-15T00:00:00Z",
            },
        }
    }
    fake_index = _FakeIndex(response)
    gsc = GSCAnalyzer.__new__(GSCAnalyzer)
    gsc._initialized = True
    gsc._property_verified = True
    gsc._site_url = "https://www.acme.dev/"
    gsc._service = _FakeService(fake_index)

    result = await gsc.inspect_url("https://www.acme.dev/blog/test")

    assert result["available"] is True
    assert result["coverage_state"] == "Submitted and indexed"
    assert result["verdict"] == "PASS"
    assert fake_index.body["inspectionUrl"] == "https://www.acme.dev/blog/test"
    assert fake_index.body["siteUrl"] == "https://www.acme.dev/"


# ---------------------------------------------------------------------------
# JSON-LD Schema Generator
# ---------------------------------------------------------------------------

def _schema_frontmatter():
    return {
        "title": "Attribution Reporting Guide",
        "description": "A practical guide to attribution reporting for marketing teams.",
        "canonical": "https://www.acme.dev/blog/attribution-reporting",
        "publishDate": "2026-05-15",
        "updatedDate": "2026-05-15",
        "author": "Editorial Team",
        "category": "Marketing",
        "seo": {"title": "Attribution Reporting Guide", "description": "A practical guide to attribution reporting for marketing teams."},
        "faq": [
            {"question": f"Question {i}?", "answer": "Attribution reporting connects spend to pipeline and revenue for clearer decisions."}
            for i in range(12)
        ],
    }


class TestSchemaGenerator:
    def test_complete_schema_includes_blogposting_and_faqpage(self):
        schema = SchemaGenerator().generate_complete_schema_markup(
            _schema_frontmatter(),
            "Content body with enough words for a test.",
        )
        types = {item.get("@type") for item in schema.get("@graph", [])}
        assert "BlogPosting" in types
        assert "FAQPage" in types

    def test_faqpage_schema_does_not_require_twelve_questions(self):
        schema = SchemaGenerator().generate_complete_schema_markup(
            {**_schema_frontmatter(), "faq": [
                {"question": "What is attribution?", "answer": "Attribution connects spend to pipeline and revenue."}
            ]},
            "Content body with enough words for a test.",
        )
        types = {item.get("@type") for item in schema.get("@graph", [])}
        assert "FAQPage" in types

    def test_complete_schema_includes_claimreview_and_howto(self):
        schema = SchemaGenerator().generate_complete_schema_markup(
            _schema_frontmatter(),
            "## Step 1: Connect CRM\n\nConnect the CRM first.",
            verified_claims=[{
                "claim": "Average reporting time fell by 40%.",
                "source": "Research",
                "confidence": "INDUSTRY",
                "date_published": "2026-01-01",
            }],
            guide_steps=[
                {"name": "Connect CRM", "text": "Connect the CRM to the reporting workflow."},
                {"name": "Connect ads", "text": "Connect advertising platforms."},
                {"name": "Review revenue", "text": "Compare spend against closed revenue."},
            ],
        )
        types = [item.get("@type") for item in schema.get("@graph", [])]
        assert "ClaimReview" in types
        assert "HowTo" in types


# ---------------------------------------------------------------------------
# Proper Noun Normalization & Slug Hygiene
# ---------------------------------------------------------------------------

class TestProperNounNormalization:
    def setup_method(self):
        self.formatter = AstroFormatter()

    def test_openai_keyword_is_normalized(self):
        assert self.formatter._normalized_keyword_title("openai ads manager") == "OpenAI Ads Manager"

    def test_chatgpt_keyword_is_normalized(self):
        assert self.formatter._normalized_keyword_title("chatgpt for sales") == "ChatGPT For Sales"

    def test_unknown_token_uses_title_case(self):
        assert self.formatter._normalized_keyword_title("widget pipeline") == "Widget Pipeline"

    def test_normalize_inside_existing_title(self):
        out = self.formatter._normalize_proper_nouns("How To Use Openai And Chatgpt In Saas Marketing")
        assert "OpenAI" in out and "ChatGPT" in out and "SaaS" in out
        assert "How" in out and "Marketing" in out


class TestTitleGeneration:
    def setup_method(self):
        self.formatter = AstroFormatter()

    def test_guide_title_no_longer_emits_how_to_noun_phrase(self):
        content = "Some article body without an H1.\n\n## Section\n\nText.\n"
        title = self.formatter._generate_title(content, "openai ads manager", "guide")
        assert "Openai" not in title and "OpenAI" in title
        assert not title.startswith("How to OpenAI")

    def test_existing_h1_is_honored_with_proper_noun_casing(self):
        content = "# How To Use Openai Ads Manager Effectively\n\nBody.\n"
        title = self.formatter._generate_title(content, "openai ads manager", "guide")
        assert "OpenAI" in title


class TestMetaDescription:
    def setup_method(self):
        self.formatter = AstroFormatter()

    def test_meta_does_not_end_with_dangling_fragment_plus_cta(self):
        content = (
            "The best practices for OpenAI Ads Manager campaigns involve "
            "setting up precise conversion tracking and using context hints. "
            "This is the second sentence.\n"
        )
        desc = self.formatter._generate_meta_description(content, "openai ads manager best practices", style="guide")
        assert desc.rstrip().endswith((".", "!", "?"))
        assert "setting up precise Get started free" not in desc
        assert "Openai" not in desc

    def test_meta_under_160_chars(self):
        content = "## A\n\nLong body paragraph. Two sentences.\n"
        desc = self.formatter._generate_meta_description(content, "lead attribution", style="standard")
        assert 50 <= len(desc) <= 160


class TestStopwordRemoval:
    def test_articles_and_prepositions_dropped(self):
        assert build_seo_slug("The Best CRM for a Small Business") == "best-crm-small-business"

    def test_intent_words_are_kept(self):
        assert build_seo_slug("how to use the best crm vs hubspot") == "how-use-best-crm-vs-hubspot"

    def test_all_stopword_input_keeps_tokens(self):
        assert build_seo_slug("the and of to") == "the-and-of-to"


class TestAsciiTransliteration:
    def test_accents_transliterated(self):
        assert build_seo_slug("Café résumé naïve") == "cafe-resume-naive"

    def test_non_latin_stripped_not_percent_encoded(self):
        out = build_seo_slug("OpenAI 广告 manager")
        assert out == "openai-manager" and "%" not in out

    def test_ampersand_does_not_create_double_hyphen(self):
        assert build_seo_slug("sales & marketing") == "sales-marketing"

    def test_apostrophe_collapsed_not_hyphenated(self):
        assert build_seo_slug("don't break this") == "dont-break"


class TestLengthCaps:
    def test_char_cap_breaks_on_word_boundary(self):
        long_kw = "enterprise customer relationship management platform comparison guide deep dive analysis"
        slug = build_seo_slug(long_kw)
        assert len(slug) <= SLUG_MAX_CHARS and not slug.endswith("-") and "-" in slug
        assert all(part.isalnum() for part in slug.split("-"))

    def test_word_cap_enforced(self):
        slug = build_seo_slug("one two three four five six seven eight nine ten", max_words=4)
        assert slug.count("-") == 3

    def test_custom_caps_respected(self):
        assert build_seo_slug("alpha beta gamma delta epsilon", max_words=2, max_chars=50) == "alpha-beta"


class TestHyphenHygiene:
    def test_no_leading_trailing_or_repeated_hyphens(self):
        assert build_seo_slug("  --Hello???   World!!--  ") == "hello-world"

    def test_empty_and_symbol_only_input(self):
        assert build_seo_slug("") == "" and build_seo_slug("!!!") == "" and build_seo_slug("   ") == ""


class TestSlugFormatterIntegration:
    def setup_method(self):
        self.formatter = AstroFormatter()

    def test_generate_slug_is_seo_optimised(self):
        assert self.formatter._generate_slug("How to Use the OpenAI Ads Manager") == "how-use-openai-ads-manager"

    def test_passed_slug_is_single_source_of_truth(self):
        raw_content = "# Intro\n\nBody about ads.\n\n## How It Works\n\nDetails here.\n"
        result = self.formatter.format(
            content=raw_content,
            keyword="how to scale paid ads like a stock market trader",
            style="standard",
            research_data={},
            slug="Scale Paid Ads Like a Trader!",
        )
        expected = build_seo_slug("Scale Paid Ads Like a Trader!")
        assert result["slug"] == expected
        assert f"/blog/{expected}" in result["content"]
        assert f"{expected}-hero.webp" in result["content"]

    def test_no_slug_falls_back_to_deterministic_keyword_slug(self):
        raw_content = "# Intro\n\nBody.\n\n## Section\n\nMore.\n"
        result = self.formatter.format(content=raw_content, keyword="best crm for small business", style="standard", research_data={})
        expected = build_seo_slug("best crm for small business")
        assert result["slug"] == expected and f"/blog/{expected}" in result["content"]


class TestRestampSlug:
    def test_rewrites_canonical_and_hero_only(self):
        mdx = (
            "canonical: https://www.acme.dev/blog/old-slug\n"
            "  src: /src/assets/blog/old-slug-hero.webp\n"
            "Body text mentioning old-slug as prose stays untouched.\n"
        )
        out = restamp_slug(mdx, "old-slug", "new-slug")
        assert "https://www.acme.dev/blog/new-slug" in out
        assert "/src/assets/blog/new-slug-hero.webp" in out
        assert "mentioning old-slug as prose" in out

    def test_noop_when_unchanged_or_empty(self):
        mdx = "blog/same-slug and same-slug-hero.webp"
        assert restamp_slug(mdx, "same-slug", "same-slug") == mdx
        assert restamp_slug(mdx, "", "new") == mdx
        assert restamp_slug(mdx, "old", "") == mdx


# ---------------------------------------------------------------------------
# Brand Positioning & #1 Ranking Enforcement
# ---------------------------------------------------------------------------

_DEMOTED = """Intro.

| # | Solution | Best For | Starting Price | Rating |
|---|----------|----------|----------------|---------|
| 1 | <a href="https://neon.tech/">Neon Serverless Postgres</a> | Autoscaling | $19/month | 8.8/10 |
| 2 | [Acme Cloud](https://acme.dev) | Instant PR branches | $0/month | 6.2/10 |

More text.
"""


def _brand_gen(valid=True):
    gen = ContentGenerator.__new__(ContentGenerator)
    gen.brand_profile = BrandProfile(
        company_name="Acme Cloud",
        site_url="https://acme.dev",
        listicle_strategy="brand-first",
    )
    gen._validate_mdx_structure = lambda c: {"valid": valid}
    return gen


def test_positioning_block_has_vision_and_ranking_only_for_ranked_styles():
    gen = _brand_gen()
    ranked = gen._format_brand_positioning("top-compare", "full")
    guide = gen._format_brand_positioning("guide", "full")
    assert "VISION" in ranked and "RANKING RULE" in ranked
    assert "CATEGORY ACCURACY" in ranked
    assert "ACME CLOUD POSITIONING" in ranked
    assert "VISION" in guide and "RANKING RULE" not in guide
    assert gen._format_brand_positioning("top-compare", "none") == ""


@pytest.mark.parametrize("content, expected", [
    (_DEMOTED, False),
    (_DEMOTED.replace("Neon Serverless Postgres", "Acme Cloud").replace("[Acme Cloud]", "[Supabase]"), True),
    (_DEMOTED.replace("Neon Serverless Postgres", "Acme Cloud").replace("[Acme Cloud]", "[Supabase]")
     .replace("8.8/10", "7.0/10").replace("6.2/10", "9.0/10"), False),
    (_DEMOTED.replace("Neon Serverless Postgres", "Acme Cloud").replace("[Acme Cloud]", "[Supabase]")
     + "\n### 1. [Supabase](https://supabase.com)\n#### Overall Score: 8.5/10\n### 2. [Acme Cloud](https://acme.dev)\n#### Overall Score: 9.2/10\n", False),
    (_DEMOTED.replace("Neon Serverless Postgres", "Acme Cloud").replace("[Acme Cloud]", "[Supabase]")
     + "\n### 1. [Acme Cloud](https://acme.dev)\n#### Overall Score: 8.5/10\n### 2. [Supabase](https://supabase.com)\n#### Overall Score: 9.0/10\n", False),
    (_DEMOTED.replace("Neon Serverless Postgres", "Acme Cloud").replace("[Acme Cloud]", "[Supabase]")
     + "\n### 1. [Acme Cloud](https://acme.dev)\n#### Overall Score: 9.2/10\n### 2. [Supabase](https://supabase.com)\n#### Overall Score: 8.5/10\n", True),
    ("No table here.", True),
])
def test_brand_ranked_first(content, expected):
    assert _brand_gen()._brand_ranked_first(content) is expected


class _ReRankAI:
    def __init__(self, result):
        self.result, self.directive = result, None

    async def edit(self, content, progress_callback=None, user_directive=None):
        self.directive = user_directive
        return self.result


async def test_enforce_rank_applies_fix_only_when_it_lands():
    fixed = _DEMOTED.replace("Neon Serverless Postgres", "Acme Cloud").replace("[Acme Cloud]", "[Neon]")
    ai = _ReRankAI(fixed.replace("8.8/10", "9.1/10"))
    result = await _brand_gen()._enforce_brand_rank(_DEMOTED, ai)
    assert result["fixed"] and "Acme Cloud is #1" in ai.directive

    still_wrong = await _brand_gen()._enforce_brand_rank(_DEMOTED, _ReRankAI(_DEMOTED))
    assert not still_wrong["fixed"] and still_wrong["content"] == _DEMOTED


async def test_enforce_rank_skips_edit_when_already_first():
    ok = _DEMOTED.replace("Neon Serverless Postgres", "Acme Cloud").replace("[Acme Cloud]", "[Supabase]")
    ai = _ReRankAI("unused")
    assert (await _brand_gen()._enforce_brand_rank(ok, ai))["content"] == ok and ai.directive is None


def test_format_gsc_intelligence_accepts_string_faq_questions():
    gen = _brand_gen()
    block = gen._format_gsc_intelligence({
        "primary_keyword": {"query": "postgres branching", "position": 11.4, "impressions": 420, "traffic_potential": 65.0, "ctr": 0.02},
        "secondary_keywords": [],
        "faq_questions": ["how does postgres branching work", "is postgres branching copy on write"],
    })
    assert "- how does postgres branching work" in block
    assert "- is postgres branching copy on write" in block
