#!/usr/bin/env python3
"""
Unit tests for SmartAIRouter
Tests multi-model orchestration, prompt routing, and API integration
"""
import pytest
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.ai_router import SmartAIRouter, _PERPLEXITY_COMMUNITY_TIMEOUT


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.responses.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


def _router_with_filter_responses(responses):
    router = SmartAIRouter()
    completions = _FakeCompletions(responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    router.or_client = client
    router.nebius = client
    router._dedicated_role_clients = {}
    return router, completions


class _FakeHTTPResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def json(self):
        return self._data


class _FakeHTTPSession:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status
        self.posts = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _FakeHTTPResponse(self.data, self.status)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_context_closes_http_and_openrouter_clients():
    calls = []

    class Closable:
        async def close(self):
            calls.append("closed")

    router = SmartAIRouter.__new__(SmartAIRouter)
    router.session = Closable()
    router.direct_gemini_client = Closable()
    router.or_client = Closable()

    await router.__aexit__(None, None, None)

    assert calls == ["closed", "closed", "closed"]
    assert router.session is None


@pytest.mark.asyncio
@pytest.mark.integration
class TestSmartAIRouter:
    """Test AI router initialization and model selection"""

    async def test_router_initialization(self):
        """Test that router initializes with API keys (supports 1-key OpenRouter or split keys)"""
        async with SmartAIRouter() as router:
            assert router.perplexity_key is not None, "Research / Perplexity key should resolve"
            assert router.nebius is not None, "Light / OpenRouter client should initialize"
            assert router.or_client is not None or router.gemini_client is not None, "Primary routing client should initialize"

    async def test_perplexity_research(self):
        """Test Perplexity research query"""
        async with SmartAIRouter() as router:
            result = await router.research("What is lead attribution?")

            assert result is not None, "Research should return result"
            assert 'choices' in result, "Result should have choices"
            content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
            assert len(content) > 100, "Research content should be substantial"
            assert 'citations' in result.get('choices', [{}])[0].get('message', {}), "Should include citations"

    async def test_gemini_generation(self):
        """Test Gemini content generation"""
        async with SmartAIRouter() as router:
            prompt = """Write a 200-word introduction about lead attribution software.
            Include: definition, key benefits, and why it matters."""

            result = await router.generate(prompt, {
                'keyword': 'lead attribution software',
                'style': 'standard'
            })

            assert result is not None, "Generation should return content"
            assert len(result) > 150, "Generated content should meet length requirements"
            assert 'lead' in result.lower(), "Content should mention the topic"
            assert 'attribution' in result.lower(), "Content should be relevant"

    async def test_custom_prompt_detection(self):
        """Test smart prompt detection for custom vs keyword prompts"""
        async with SmartAIRouter() as router:
            # Custom research prompt with markers
            custom_prompt = """
            CRITICAL RESEARCH QUALITY STANDARDS:
            - 3500-5000 words
            - Academic tone

            Write comprehensive research on lead attribution.
            """

            result = await router.generate(custom_prompt, {
                'keyword': 'lead attribution',
                'style': 'research'
            })

            assert result is not None, "Custom prompt should generate content"
            assert len(result) > 200, "Should generate substantial content"

    async def test_multipart_generation(self):
        """Test multi-part generation for long content"""
        async with SmartAIRouter() as router:
            prompt = """Write a comprehensive 2000+ word guide about CRM integration.

            Include these sections:
            1. Introduction
            2. How CRM Integration Works
            3. Implementation Steps
            4. Benefits
            5. Common Challenges
            6. Best Practices
            """

            result = await router.generate(prompt, {
                'keyword': 'CRM integration',
                'style': 'guide'
            })

            assert result is not None, "Multipart generation should complete"
            # Should have meaningful content (multipart combines 3 parts)
            assert len(result) > 500, "Multipart should generate substantial content"

    async def test_edit_formatting(self):
        """Test OpenRouter edit/formatting step"""
        async with SmartAIRouter() as router:
            raw_content = (
                "---\n"
                'title: "Lead Attribution Guide"\n'
                'description: "Practical guide to lead attribution."\n'
                "---\n\n"
                "## Introduction\n\n"
                "Lead attribution is important for revenue teams. It helps track leads across channels.\n\n"
                "## How It Works\n\n"
                "Teams use UTM parameters and CRM stage matching to verify conversion sources.\n"
            )

            result = await router.edit(raw_content)

            assert isinstance(result, str) and len(result) > 0, "Edit should return formatted content"
            assert '---' in result and '## ' in result, "Should preserve frontmatter and headings"

    async def test_polish_step(self):
        """Test polish_content step"""
        async with SmartAIRouter() as router:
            if not router.or_client and "POLISH" not in getattr(router, "_dedicated_role_clients", {}):
                pytest.skip("Polish API key not available")

            raw_content = (
                "---\n"
                'title: "Test Article"\n'
                'description: "A test article about lead tracking."\n'
                "---\n\n"
                "## Lead Tracking Basics\n\n"
                "This is a test article about lead tracking across channels. "
                "It has concrete examples that can be polished for clarity.\n"
            )

            result = await router.polish_content(raw_content)

            assert isinstance(result, dict), "Polish should return a mapping"
            assert not result.get("failed"), result.get("failure_reason")
            polished = result["qwen"]
            assert isinstance(polished, str) and len(polished) > 0, "Polish should return content"
            assert '---' in polished, "Should preserve frontmatter"

    async def test_error_handling(self):
        """Test graceful error handling"""
        async with SmartAIRouter() as router:
            # Test with empty prompt
            try:
                result = await router.generate("", {})
                # Should either return empty or handle gracefully
                assert isinstance(result, str), "Should return string even on error"
            except Exception as e:
                # If it raises, should be a meaningful error
                assert len(str(e)) > 0, "Error message should be informative"


@pytest.mark.asyncio
@pytest.mark.unit
class TestInsightFiltering:
    """Unit tests for research insight filtering fallbacks."""

    async def test_qwen_filter_retries_empty_visible_content(self):
        """Empty first response should retry with low reasoning excluded."""
        insights = [
            "Lead sellers say CRM attribution gaps make it hard to prove which ad spend generated pipeline.",
            "Enterprise procurement teams discuss a completely unrelated compliance process.",
        ]
        router, completions = _router_with_filter_responses([
            "",
            '["Lead sellers say CRM attribution gaps make it hard to prove which ad spend generated pipeline."]',
        ])
        router.model_light = "qwen/qwen3.5-397b-a17b"  # legacy LIGHT_MODEL route

        filtered = await router._filter_batch_with_qwen(
            insights,
            "lead attribution",
            "Reddit discussions",
        )

        assert filtered == [insights[0]]
        assert len(completions.calls) == 2
        assert completions.calls[0]["extra_body"] == router.OR_PROVIDER_QWEN_FAST
        retry_extra = completions.calls[1]["extra_body"]
        assert retry_extra["provider"] == router.OR_PROVIDER_QWEN["provider"]
        assert retry_extra["reasoning"] == {"effort": "low", "exclude": True}
        assert completions.calls[1]["extra_body"]["reasoning"]["exclude"] is True

    async def test_qwen_filter_keeps_insights_when_retry_is_empty(self):
        """If both attempts are empty, preserve the existing graceful fallback."""
        insights = [
            "Media buyers need source-backed CAC ranges before adjusting spend allocation.",
            "Service businesses often need CRM notes connected to quote and invoice outcomes.",
        ]
        router, completions = _router_with_filter_responses(["", ""])

        filtered = await router._filter_batch_with_qwen(
            insights,
            "lead attribution",
            "Reddit discussions",
        )

        assert filtered == insights
        assert len(completions.calls) == 2

    async def test_fake_filter_router_ignores_dedicated_light_key(self, monkeypatch):
        """Dedicated FLEO_LIGHT_API_KEY must not bypass the unit-test fake queue."""
        monkeypatch.setenv("FLEO_LIGHT_API_KEY", "sk-proj-test-dedicated-light")
        router, completions = _router_with_filter_responses(['["kept"]'])
        out = await router._filter_batch_with_qwen(["kept"], "postgres", "Reddit")
        assert out == ["kept"]
        assert len(completions.calls) == 1

    async def test_qwen_filter_prompt_uses_brand_profile_audience_without_enterprise_exclusion(self):
        """Enterprise BrandProfiles must not receive a hardcoded 'business operators, not enterprise' exclusion."""
        from core.brand_profile import BrandProfile
        router, completions = _router_with_filter_responses(['["Enterprise cluster migration reduced p99 latency."]'])
        router.brand_profile = BrandProfile(
            company_name="AcmeCloud",
            icp_reader="Enterprise platform and infrastructure engineering teams",
        )
        out = await router._filter_batch_with_qwen(
            ["Enterprise cluster migration reduced p99 latency."],
            "postgres multi-region failover",
            "Reddit discussions",
        )
        assert out == ["Enterprise cluster migration reduced p99 latency."]
        user_prompt = completions.calls[0]["messages"][1]["content"]
        assert "Enterprise platform and infrastructure engineering teams" in user_prompt
        assert "not enterprise" not in user_prompt.lower()
        assert "business operators" not in user_prompt.lower()


@pytest.mark.unit
@pytest.mark.asyncio
class TestGenerateSlug:
    """Unit tests for the smart slug generator (cheap LLM + graceful fallback)."""

    async def test_parses_single_line_slug(self):
        router = SmartAIRouter()

        async def _fake_qwen(messages, **kwargs):
            assert kwargs.get("reasoning_enabled") is False
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="  scale-paid-ads-like-a-trader\n"))]
            )

        router._qwen_create = _fake_qwen
        slug = await router.generate_slug("how to scale paid ads like a stock market trader")
        assert slug == "scale-paid-ads-like-a-trader"

    async def test_returns_none_on_empty_or_error(self):
        router = SmartAIRouter()

        async def _empty(messages, **kwargs):
            return None

        router._qwen_create = _empty
        assert await router.generate_slug("anything") is None

        async def _boom(messages, **kwargs):
            raise RuntimeError("provider down")

        router._qwen_create = _boom
        assert await router.generate_slug("anything") is None


@pytest.mark.asyncio
@pytest.mark.unit
class TestCompoundWebResearch:
    """Unit tests for generic web-search research mode."""

    async def test_web_research_preserves_prompt_and_returns_content_as_insight(self):
        response_data = {
            "choices": [{
                "message": {
                    "content": '{"url_accessible": true, "claim_supported": true, "source_credible": true}'
                }
            }],
            "citations": ["https://example.com/report"],
            "search_results": [{"title": "Report", "url": "https://example.com/report"}],
        }
        router = SmartAIRouter()
        router.perplexity_key = "test-key"
        router.session = _FakeHTTPSession(response_data)
        router.gemini_client = None

        result = await router.research_with_compound(
            ["Verify this citation for factual accuracy."],
            platform="web",
        )

        payload = router.session.posts[0]["json"]
        assert result["insights"] == [response_data["choices"][0]["message"]["content"]]
        assert payload["messages"][0]["content"] == "Verify this citation for factual accuracy."
        assert payload["search_mode"] == "web"
        assert "search_domain_filter" not in payload
        assert result["citations"] == [{
            "url": "https://example.com/report",
            "platform": "Web",
            "type": "perplexity_search"
        }]

    async def test_web_research_does_not_use_web_dot_com_domain_filter(self):
        response_data = {
            "choices": [{"message": {"content": "Plain verification answer"}}],
            "citations": [],
        }
        router = SmartAIRouter()
        router.perplexity_key = "test-key"
        router.session = _FakeHTTPSession(response_data)
        router.gemini_client = None

        await router.research_with_compound(["Verify source"], platform="web")

        payload = router.session.posts[0]["json"]
        assert "web.com" not in payload["messages"][0]["content"]
        assert payload.get("search_domain_filter") is None

    async def test_gemini_grounding_collects_message_annotations_citations(self):
        router = SmartAIRouter()

        async def _fake_gemini(**kwargs):
            return SimpleNamespace(
                content="- Teams report lower latency with logical replication.",
                tool_calls=[],
                annotations=[
                    {
                        "type": "url_citation",
                        "url_citation": {
                            "url": "https://reddit.com/r/PostgreSQL/comments/abc123/replication",
                            "title": "Logical replication latency",
                        },
                    }
                ],
            )

        router._call_gemini_with_fallback = _fake_gemini
        web_res = await router._research_web_with_gemini_grounding(["postgres replication latency"])
        comm_res = await router._research_with_gemini_grounding(["postgres replication latency"], "reddit", "")
        assert web_res["citations"][0]["url"] == "https://reddit.com/r/PostgreSQL/comments/abc123/replication"
        assert comm_res["citations"][0]["url"] == "https://reddit.com/r/PostgreSQL/comments/abc123/replication"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.generation
class TestPromptConstruction:
    """Test prompt building and template logic"""

    async def test_paa_section_formatting(self):
        """Test PAA question formatting in prompts"""
        async with SmartAIRouter() as router:
            paa_questions = [
                "What is lead attribution?",
                "How does attribution tracking work?",
                "Why is lead attribution important?"
            ]

            # This is tested indirectly through generate()
            result = await router.generate("lead attribution", {
                'keyword': 'lead attribution',
                'style': 'standard',
                'research_data': {
                    'serp': {
                        'paa_questions': paa_questions
                    }
                }
            })

            assert result is not None, "Should handle PAA questions"


@pytest.mark.unit
class TestTLDRGeneration:
    """Test TLDR generation validation and parsing logic"""

    def test_tldr_validation_with_valid_data(self):
        """Test valid TLDR structures pass successfully"""
        router = SmartAIRouter()
        valid_tldr = {
            "answer": "A perfect direct answer that has exactly forty words and explains the concept of ad reconciliation for sales-led operations. This is a very clean answer with 120-500 characters and no forbidden symbols.",
            "takeaways": [
                "This is a correct standalone declarative takeaway sentence.",
                "Another valid declarative takeaway sentence for performance marketing.",
                "Third declarative takeaway sentence with specific criteria."
            ]
        }
        is_valid, issues = router._validate_tldr_data(valid_tldr)
        assert is_valid, f"Should be valid, got issues: {issues}"

    def test_tldr_validation_with_invalid_data(self):
        """Test invalid TLDR structures fail with precise errors"""
        router = SmartAIRouter()
        
        # Too short answer
        invalid_tldr_1 = {
            "answer": "Too short.",
            "takeaways": [
                "Valid sentence one.",
                "Valid sentence two.",
                "Valid sentence three."
            ]
        }
        is_valid, issues = router._validate_tldr_data(invalid_tldr_1)
        assert not is_valid
        assert any("strict limit: 120-500 chars" in issue for issue in issues)

        # Has forbidden punctuation (semicolons/em-dashes)
        invalid_tldr_2 = {
            "answer": "This answer is too long and also contains a forbidden character; a semicolon. It also has enough characters to pass the character count limits, but fails because of the punctuation.",
            "takeaways": [
                "Valid sentence one.",
                "Valid sentence two.",
                "Invalid sentence—with em dash."
            ]
        }
        is_valid, issues = router._validate_tldr_data(invalid_tldr_2)
        assert not is_valid
        assert any("contains semicolons" in issue for issue in issues)
        assert any("contains em/en dashes" in issue for issue in issues)

    def test_direct_google_client_is_primary_for_gemini(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
        monkeypatch.setenv("GOOGLE_API_KEY", "direct-test-key")
        monkeypatch.setenv("NEBIUS_API_KEY", "nebius-test-key")
        router = SmartAIRouter()
        assert router.direct_google_key == "direct-test-key"
        assert router.direct_gemini_client is not None
        assert router.nebius_direct_key is None
        assert router.nebius_direct is None
        assert router.gemini_client is router.direct_gemini_client

    @pytest.mark.asyncio
    async def test_direct_google_strips_openrouter_only_tools(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "direct-test-key")
        router = SmartAIRouter()
        completions = _FlakyCompletions(errors=[], responses=["direct gemini"])
        router.direct_gemini_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions))

        message = await router._call_gemini_with_fallback(
            [{"role": "user", "content": "hello"}],
            model=router.gemini_flash,
            tools=[
                {"type": "openrouter:web_search"},
                {"type": "function", "function": {"name": "lookup"}},
            ],
            purpose="direct-google-tools-test",
        )

        assert message.content == "direct gemini"
        assert completions.calls[0]["model"] == router.gemini_flash.removeprefix("google/")
        assert completions.calls[0]["tools"] == [
            {"type": "function", "function": {"name": "lookup"}}
        ]

    @pytest.mark.asyncio
    async def test_missing_google_key_falls_back_to_openrouter_openai(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        completions = _FlakyCompletions(errors=[], responses=["openrouter gemini"])
        router = SmartAIRouter()
        router.or_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        message = await router._call_gemini_with_fallback(
            [{"role": "user", "content": "hello"}],
            model=router.gemini_flash,
            purpose="openrouter-only test",
        )

        assert message.content == "openrouter gemini"
        assert completions.calls[0]["model"] == router.model_fallback_openai
        assert completions.calls[0]["extra_body"] == router.OR_PROVIDER_OPENAI_LOW

    @pytest.mark.asyncio
    async def test_explicit_fleo_seo_model_routes_via_openrouter_and_title_editor_disables_reasoning(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("FLEO_SEO_MODEL", "google/gemini-4-hydrogen")
        completions = _FlakyCompletions(errors=[], responses=["custom seo title"])
        router = SmartAIRouter()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        router.or_client = client
        router.role_clients["SEO"] = client

        msg = await router._call_title_editor([{"role": "user", "content": "optimize title"}])

        assert msg.content == "custom seo title"
        assert completions.calls[0]["model"] == "google/gemini-4-hydrogen"
        assert completions.calls[0]["max_tokens"] == 1200
        assert completions.calls[0]["extra_body"] == {
            "provider": {"allow_fallbacks": True},
            "reasoning": {"enabled": False},
        }
        assert router.models_used["title SEO optimization"]["downgraded"] is False



@pytest.mark.asyncio
@pytest.mark.unit
class TestKeywordContextInterpretation:
    """Test keyword context interpretation, cleaning, validation, fallback and corrections."""

    def test_clean_keyword_context_interpretation_removes_labels(self):
        router = SmartAIRouter()
        raw_text = (
            "Sentence 1: This topic is about treating paid ads as a capital allocation decision.\n"
            "Sentence 2 (What the angle should take): It should show operators how to scale like an investor."
        )
        cleaned = router._clean_keyword_context_interpretation(raw_text)
        assert cleaned == "This topic is about treating paid ads as a capital allocation decision. It should show operators how to scale like an investor."

    def test_clean_keyword_context_interpretation_unbalanced_nested(self):
        router = SmartAIRouter()
        # Test cleaning brackets and lists
        raw_text = (
            "- [Sentence 1] This is the first sentence.\n"
            "* (Sentence 2) This is the second sentence."
        )
        cleaned = router._clean_keyword_context_interpretation(raw_text)
        assert cleaned == "This is the first sentence. This is the second sentence."

    def test_valid_keyword_context_interpretation_rules(self):
        router = SmartAIRouter()
        
        # Valid output: 2 sentences, length > 80, no banned patterns
        valid = (
            "This topic is about understanding modern lead attribution models in growth marketing. "
            "The article should walk performance marketers through setup, metrics, and closed-loop validation."
        )
        assert router._valid_keyword_context_interpretation(valid) is True

        # Invalid: Too short
        assert router._valid_keyword_context_interpretation("Too short sentence. Another too short sentence.") is False

        # Invalid: Fewer than 2 sentences
        assert router._valid_keyword_context_interpretation(
            "This is a single very long sentence that has no second sentence to guide the core angle of the article."
        ) is False

        # Invalid: Leaks prompt template words
        leaky = (
            "Sentence 1: This is what the topic is actually about. "
            "Sentence 2: The angle the article should take."
        )
        assert router._valid_keyword_context_interpretation(leaky) is False

        # Invalid: Obvious incomplete fragments at end
        fragment = (
            "This is the first sentence that is completely valid. "
            "The second sentence is cut off and ends with and"
        )
        assert router._valid_keyword_context_interpretation(fragment) is False

        # Invalid: JSON structure
        json_output = '{"topic": "This is a topic sentence.", "angle": "This is an angle sentence."}'
        assert router._valid_keyword_context_interpretation(json_output) is False

        # Invalid: jargon run-on (real 2026-09-28 output, 45-word second sentence)
        run_on = (
            "Performance marketers are hitting working capital ceilings where campaigns starve. "
            "Delivering a comprehensive blueprint requires detailing the underwriting mechanics of revenue-based "
            "lines versus extended-float credit, alongside a concrete operating model for virtual card "
            "architectures spanning automated balance top-offs, sub-account liability isolation, and merchant "
            "category controls that prevent scaling bottlenecks."
        )
        assert router._valid_keyword_context_interpretation(run_on) is False

    def test_fallback_context_heuristics(self):
        router = SmartAIRouter()

        # Analogy branch: names both sides of the comparison, no templated opener.
        analogy_fallback = router._fallback_keyword_context("How to scale paid ads like a capital markets investor")
        assert "capital markets investor" in analogy_fallback
        assert not analogy_fallback.startswith("This topic")

        # Question branch: keeps the action, no templated opener.
        how_to_fallback = router._fallback_keyword_context("how to migrate a database safely")
        assert "migrate a database safely" in how_to_fallback
        assert not how_to_fallback.startswith("This topic")

        # Feedback synthesis: threads the user's correction through verbatim.
        feedback_fallback = router._fallback_keyword_context(
            "keyword", user_feedback="treating ad spend like an investment given it provides dependable returns"
        )
        assert "treating ad spend like an investment" in feedback_fallback
        assert "dependable returns" in feedback_fallback

    def test_fallback_context_varies_and_avoids_template_opener(self):
        """Fallback must stop emitting one identical 'This topic...' sentence.

        Each keyword is embedded into a natural, shape-aware phrasing, so
        different keywords surface different wording and none lean on the banned
        formula opener. (The fallback is a safety net, not the variety engine —
        that lives in the de-templated LLM path.)
        """
        router = SmartAIRouter()
        keywords = [
            "best crm tools", "ad attribution models", "reduce customer acquisition cost",
            "marketing budget planning", "pipeline forecasting", "roas benchmarks",
        ]
        outputs = [router._fallback_keyword_context(k) for k in keywords]
        for out in outputs:
            assert not out.startswith("This topic")
            assert not out.startswith("This article")
        # Not all identical — de-templating actually produces variety.
        assert len(set(outputs)) > 1
        # Same keyword is stable across calls (deterministic, testable).
        assert router._fallback_keyword_context("best crm tools") == outputs[0]

    def test_fallback_context_style_aware_and_concrete(self):
        """Catch-all fallback commits to concrete substance shaped by style, not
        a generic 'trade-offs' shell."""
        router = SmartAIRouter()
        comparison = router._fallback_keyword_context("crm platforms for solar companies", style="comparison")
        assert "criteria" in comparison
        research = router._fallback_keyword_context("lead conversion benchmarks", style="research")
        assert "data" in research
        default = router._fallback_keyword_context("ad budget pacing", style="guide")
        assert "worked example" in default
        # The old boilerplate shell is gone.
        for out in (comparison, research, default):
            assert "weighing a real decision" not in out
            assert "key trade-offs" not in out

    def test_fallback_context_regenerate_never_repeats_previous(self):
        """If every LLM tier fails on a regenerate, the fallback must still not
        hand back the byte-identical take the user just rejected."""
        router = SmartAIRouter()
        first = router._fallback_keyword_context("ad budget pacing", style="guide")
        second = router._fallback_keyword_context(
            "ad budget pacing", style="guide", previous_interpretation=first
        )
        assert second != first
        assert len(second) > 80  # still a real two-sentence interpretation
        # Without a previous take, output stays deterministic per keyword+style.
        assert router._fallback_keyword_context("ad budget pacing", style="guide") == first

    def test_system_prompt_contains_light_prior_no_brands(self):
        router = SmartAIRouter()
        sys_prompt = router._keyword_context_system_prompt()
        assert "Weak background context" in sys_prompt
        assert "The reader:" in sys_prompt
        assert "each sentence under 25 words" in sys_prompt

        banned = ["Flyweel", "AcmeInternalSecret"]
        for brand in banned:
            assert brand not in sys_prompt

    def test_interpret_system_prompt_steers_away_from_templates(self):
        """The system prompt must actively discourage the formula openers."""
        router = SmartAIRouter()
        sys_prompt = router._keyword_context_system_prompt()
        assert "sharp editor" in sys_prompt
        assert "vary your sentence openings" in sys_prompt
        # Explicitly bans the stock openers that made every interpretation read the same.
        assert "This topic is about" in sys_prompt  # named as a thing to avoid
        assert "The article should" in sys_prompt

    def test_interpret_system_prompt_carries_concrete_substance_frame(self):
        """Reverse-engineered from the desired final article: the second sentence
        must commit to concrete substance (mechanism/inputs/worked example/decision
        criteria), style-shaped — while the produced sentences stay plain editorial
        language with no SEO-meta vocabulary."""
        router = SmartAIRouter()
        sys_prompt = router._keyword_context_system_prompt()
        assert "Work backwards from the finished article" in sys_prompt
        assert "worked example" in sys_prompt
        assert "decision criteria" in sys_prompt
        # Style-shaping is described, not templated per-style scaffolds.
        assert "shaped by the content style" in sys_prompt
        # The guardrail: meta-vocabulary must never leak into the output sentences.
        assert "never mention search engines, SEO, AI, keywords" in sys_prompt

    async def test_interpret_fresh_regenerate_uses_previous_without_feedback(self, monkeypatch):
        """Ctrl+N with no steer text: previous take rides along so the prompt is
        never byte-identical — the root cause of 'regenerate returned the exact
        same interp'. No 'User correction' framing (there was no correction)."""
        router = SmartAIRouter()
        seen_user_messages = []

        async def mock_call(messages, **kwargs):
            seen_user_messages.extend([m["content"] for m in messages if m["role"] == "user"])
            return SimpleNamespace(
                content=(
                    "Service operators here want to know how fast every ad dollar returns as banked revenue. "
                    "The piece should build the payback model from their own close rates and job values, week by week."
                )
            )

        monkeypatch.setattr(router, "_call_gemini_with_fallback", mock_call)
        await router.interpret_keyword_context(
            keyword="90-day ad payback model",
            style="guide",
            previous_interpretation="Someone searching this is weighing a real decision.",
        )

        assert len(seen_user_messages) == 1
        user_msg = seen_user_messages[0]
        assert "Someone searching this is weighing a real decision." in user_msg
        assert "genuinely different" in user_msg
        assert "User correction" not in user_msg

    async def test_interpret_fresh_prompt_drops_sentence_slot_scaffold(self, monkeypatch):
        """Fresh (unsteered) user prompt must no longer use the rigid Sentence 1/2 slots."""
        router = SmartAIRouter()
        seen_user_messages = []

        async def mock_call(messages, **kwargs):
            seen_user_messages.extend([m["content"] for m in messages if m["role"] == "user"])
            return SimpleNamespace(
                content=(
                    "Operators searching this want to know whether high ad spend pays back on a "
                    "timeline their cash flow can absorb. The strongest angle walks through a concrete "
                    "payback model rather than restating the risk."
                )
            )

        monkeypatch.setattr(router, "_call_gemini_with_fallback", mock_call)
        await router.interpret_keyword_context("scale ad spend", "standard")

        assert len(seen_user_messages) == 1
        user_msg = seen_user_messages[0]
        assert "Sentence 1 must" not in user_msg
        assert "Sentence 2 must" not in user_msg
        assert "avoid formula openers" in user_msg

    async def test_interpret_keyword_context_success_first_pass(self, monkeypatch):
        router = SmartAIRouter()
        
        async def mock_call(messages, **kwargs):
            return SimpleNamespace(
                content=(
                    "Sentence 1: This topic is about the ROI of high-budget advertising platforms. "
                    "Sentence 2: The article should explore how to mitigate spend risks using data-driven payback models."
                )
            )
            
        monkeypatch.setattr(router, "_call_gemini_with_fallback", mock_call)
        
        result = await router.interpret_keyword_context("ad spend scaling", "standard")
        assert result == "This topic is about the ROI of high-budget advertising platforms. The article should explore how to mitigate spend risks using data-driven payback models."

    async def test_interpret_keyword_context_success_on_repair_pass(self, monkeypatch):
        router = SmartAIRouter()
        calls = []
        
        async def mock_call(messages, purpose, **kwargs):
            calls.append(purpose)
            if "repair" in purpose:
                return SimpleNamespace(
                    content=(
                        "This topic covers B2B pipeline conversion metrics. "
                        "The article should show how to scale conversion velocity safely."
                    )
                )
            else:
                return SimpleNamespace(content="Sentence 1 (What the topic is about): bad output fragment")
                
        monkeypatch.setattr(router, "_call_gemini_with_fallback", mock_call)
        
        result = await router.interpret_keyword_context("b2b pipeline", "standard")
        assert len(calls) == 2
        assert "keyword context interpretation" in calls[0]
        assert "keyword context interpretation repair" in calls[1]
        assert result == "This topic covers B2B pipeline conversion metrics. The article should show how to scale conversion velocity safely."

    async def test_interpret_keyword_context_fails_to_fallback(self, monkeypatch):
        router = SmartAIRouter()
        
        async def mock_call(messages, **kwargs):
            return SimpleNamespace(content="Sentence 1 (What the topic is about): bad output fragment")
            
        monkeypatch.setattr(router, "_call_gemini_with_fallback", mock_call)
        
        result = await router.interpret_keyword_context("How to scale paid ads like a capital markets investor")
        # Should fallback to the analogy fallback — names the comparison, no template opener.
        assert result is not None
        assert "capital markets investor" in result
        assert not result.startswith("This topic")

    async def test_interpret_keyword_context_applies_user_feedback(self, monkeypatch):
        router = SmartAIRouter()
        seen_user_messages = []
        
        async def mock_call(messages, **kwargs):
            seen_user_messages.extend([m["content"] for m in messages if m["role"] == "user"])
            return SimpleNamespace(
                content=(
                    "This topic explains treating ad spend as a financial asset with predictable yield. "
                    "The article should show how to reallocate budget strictly against cash-flow payback."
                )
            )
            
        monkeypatch.setattr(router, "_call_gemini_with_fallback", mock_call)
        
        result = await router.interpret_keyword_context(
            keyword="scale ads",
            style="standard",
            user_feedback="treat ad spend like an investment",
            previous_interpretation="This topic is aimed at advanced growth"
        )
        
        assert len(seen_user_messages) == 1
        assert "treat ad spend like an investment" in seen_user_messages[0]
        assert "This topic is aimed at advanced growth" in seen_user_messages[0]
        assert result == "This topic explains treating ad spend as a financial asset with predictable yield. The article should show how to reallocate budget strictly against cash-flow payback."


class _TimeoutAwareResponse:
    def __init__(self, data, status=200, raise_timeout=False):
        self._data = data
        self.status = status
        self._raise_timeout = raise_timeout

    async def __aenter__(self):
        if self._raise_timeout:
            raise asyncio.TimeoutError()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def json(self):
        return self._data


class _TimeoutAwareSession:
    """Fake aiohttp session capturing every kwarg passed to post()."""

    def __init__(self, data=None, status=200, raise_timeout=False):
        self.data = data if data is not None else {}
        self.status = status
        self.raise_timeout = raise_timeout
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _TimeoutAwareResponse(self.data, self.status, self.raise_timeout)


def _bare_router(session):
    """SmartAIRouter without __init__ (no env keys needed) — just the attrs
    the Perplexity community-research paths touch."""
    router = SmartAIRouter.__new__(SmartAIRouter)
    router.session = session
    router.perplexity_key = "unit-test-key"
    return router


@pytest.mark.unit
@pytest.mark.asyncio
class TestPerplexityCommunityTimeout:
    """The community/web Perplexity calls must carry the shared client timeout
    and degrade to None (not raise) when it fires."""

    _COMMUNITY_DATA = {
        "choices": [{"message": {"content": "- Users say attribution gaps hide winning ads"}}],
        "citations": ["https://reddit.com/r/PPC/comments/example"],
    }

    def test_timeout_constant_values(self):
        assert _PERPLEXITY_COMMUNITY_TIMEOUT.total == 150
        assert _PERPLEXITY_COMMUNITY_TIMEOUT.connect == 30

    async def test_community_research_passes_shared_timeout(self):
        session = _TimeoutAwareSession(self._COMMUNITY_DATA)
        router = _bare_router(session)

        result = await router._research_with_perplexity(
            ["What frustrates media buyers about attribution?"], "reddit", "")

        assert result is not None
        assert len(session.calls) == 1
        assert session.calls[0]["timeout"] is _PERPLEXITY_COMMUNITY_TIMEOUT

    async def test_web_research_passes_shared_timeout(self):
        session = _TimeoutAwareSession(self._COMMUNITY_DATA)
        router = _bare_router(session)

        result = await router._research_web_with_perplexity(["Verify this claim"])

        assert result is not None
        assert len(session.calls) == 1
        assert session.calls[0]["timeout"] is _PERPLEXITY_COMMUNITY_TIMEOUT

    async def test_community_research_returns_none_on_timeout(self):
        session = _TimeoutAwareSession(raise_timeout=True)
        router = _bare_router(session)

        result = await router._research_with_perplexity(["question"], "quora", "")

        assert result is None
        assert session.calls[0]["timeout"] is _PERPLEXITY_COMMUNITY_TIMEOUT

    async def test_community_mining_queries_apply_brand_search_aliases(self, monkeypatch):
        from core.brand_profile import BrandProfile
        import core.ai_router as ar_mod

        monkeypatch.setattr(
            ar_mod,
            "load_brand_profile",
            lambda: BrandProfile(company_name="Acmee", search_aliases={"acmee": "acme"}),
        )
        session = _TimeoutAwareSession(self._COMMUNITY_DATA)
        router = _bare_router(session)
        router.gemini_client = None

        result = await router.research_with_compound(
            ["What do users think about Acmee branching?"],
            platform="reddit",
        )

        prompt = session.calls[0]["json"]["messages"][0]["content"]
        assert "acme branching" in prompt
        assert "Acmee branching" not in prompt
        assert result["questions"] == ["What do users think about acme branching?"]

    def test_filter_platform_questions_uses_brand_profile_competitors(self):
        from core.brand_profile import BrandProfile

        router = SmartAIRouter.__new__(SmartAIRouter)
        router.brand_profile = BrandProfile(
            company_name="ExampleCo",
            direct_competitors=["CompA", "CompB"],
        )
        questions = [
            "How does OtherTool integrate with webhooks?",
            "Why do teams switch away from CompA?",
            "How do teams run sprint triage?",
        ]
        # Filters configured competitors while keeping unrelated third-party tools
        filtered = router._filter_platform_questions(questions, keyword="sprint triage")
        assert filtered == [
            "How does OtherTool integrate with webhooks?",
            "How do teams run sprint triage?",
        ]
        # Keeps questions naming a competitor explicitly targeted by keyword
        targeted = router._filter_platform_questions(questions, keyword="CompA alternatives")
        assert "Why do teams switch away from CompA?" in targeted

    async def test_web_research_returns_none_on_timeout(self):
        session = _TimeoutAwareSession(raise_timeout=True)
        router = _bare_router(session)

        result = await router._research_web_with_perplexity(["question"])

        assert result is None
        assert session.calls[0]["timeout"] is _PERPLEXITY_COMMUNITY_TIMEOUT


class _ProviderUnavailableError(Exception):
    """Mimics openai.NotFoundError for the dead-provider-pin case."""

    def __init__(self, message, status_code=404):
        super().__init__(message)
        self.status_code = status_code


class _FlakyCompletions:
    """Raises the queued exceptions first, then returns the queued contents."""

    def __init__(self, errors, responses):
        self.errors = list(errors)
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        content = self.responses.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


@pytest.mark.unit
@pytest.mark.asyncio
class TestQwenCreateProviderFallback:
    """_qwen_create uses multi-provider routing without a dead strict pin."""

    def _router(self, completions):
        router = SmartAIRouter()
        router.nebius_direct = None
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        router.or_client = client
        router.nebius = client
        return router

    async def test_uses_fallback_capable_route_on_first_request(self):
        completions = _FlakyCompletions(
            errors=[],
            responses=["POLISH-OK"],
        )
        router = self._router(completions)
        router.model_light = "qwen/qwen3.5-397b-a17b"  # legacy LIGHT_MODEL route

        resp = await router._qwen_create(
            [{"role": "user", "content": "hi"}], purpose="test fallback"
        )

        assert router._safe_content(resp) == "POLISH-OK"
        assert len(completions.calls) == 1
        extra = completions.calls[0]["extra_body"]
        assert extra["provider"] == {
            "allow_fallbacks": True,
            "require_parameters": True,
        }
        assert "only" not in extra["provider"]
        assert extra["reasoning"] == {"enabled": False}

    async def test_provider_errors_are_not_retried_with_another_client_request(self):
        completions = _FlakyCompletions(
            errors=[_ProviderUnavailableError("provider_not_available", status_code=403)],
            responses=["ok"],
        )
        router = self._router(completions)

        with pytest.raises(_ProviderUnavailableError):
            await router._qwen_create([{"role": "user", "content": "hi"}])
        assert len(completions.calls) == 1

    async def test_non_availability_errors_reraise(self):
        completions = _FlakyCompletions(
            errors=[_ProviderUnavailableError("rate limited", status_code=429)],
            responses=["never reached"],
        )
        router = self._router(completions)

        with pytest.raises(_ProviderUnavailableError):
            await router._qwen_create([{"role": "user", "content": "hi"}])
        assert len(completions.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
class TestPolishResilience:
    """A dead model must degrade to the original draft, flagged, not crash."""

    def _content(self):
        body = " ".join(["word"] * 120)
        return (
            "---\ntitle: Test\n---\n"
            f"Intro paragraph. {body}\n\n"
            f"## Section One\n{body}\n\n"
            f"## Section Two\n{body}\n"
        )

    async def test_frontmatter_polish_uses_dedicated_polish_path(self):
        router = SmartAIRouter()
        router.nebius = object()
        calls = []

        async def _ok(messages, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="---\ntitle: Polished\n---"
                ))]
            )

        router._polish_create = _ok
        content = "---\ntitle: Original\n---\n\n" + " ".join(["body"] * 120)

        out = await router.polish_frontmatter(content)

        assert out["qwen"].startswith("---\ntitle: Polished\n---")
        assert calls == [{
            "temperature": 0.2,
            "max_tokens": 8000,
            "model": router.model_polish,
            "purpose": "frontmatter polish",
        }]

    async def test_polish_section_survives_raising_calls(self):
        router = SmartAIRouter()

        calls = []

        async def _boom(messages, **kwargs):
            calls.append(kwargs)
            raise _ProviderUnavailableError("No allowed providers", status_code=404)

        router._polish_create = _boom
        section = "## Heading\n" + " ".join(["word"] * 100)

        result = await router._polish_section(section, 0, 1, router.model_polish)

        assert result == section
        assert len(calls) == 3  # all attempts consumed, nothing escaped
        assert router._polish_fallback_count == 1
        assert "No allowed providers" in router._polish_failure_reason

    async def test_polish_content_flags_total_fallback(self):
        router = SmartAIRouter()
        router.nebius = object()  # pass the client guard

        async def _boom(messages, **kwargs):
            raise _ProviderUnavailableError("No allowed providers", status_code=404)

        router._polish_create = _boom
        content = self._content()

        out = await router.polish_content(content)

        assert out.get("failed") is True
        assert "No allowed providers" in out["failure_reason"]
        # Body text is preserved (whitespace may be rejoined between batches).
        assert "Section Two" in out["qwen"]

    async def test_polish_content_flags_failed_on_exception(self):
        router = SmartAIRouter()
        router.nebius = object()

        async def _explode(*args, **kwargs):
            raise RuntimeError("multipart blew up")

        router._polish_multipart = _explode
        content = self._content()

        out = await router.polish_content(content)

        assert out == {
            "llama": content,
            "qwen": content,
            "failed": True,
            "failure_reason": "multipart blew up",
        }

    async def test_polish_content_no_flag_on_success(self):
        router = SmartAIRouter()
        router.nebius = object()
        polished_section = "## Heading\n" + " ".join(["nice"] * 100)

        async def _ok(messages, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=polished_section))]
            )

        router._polish_create = _ok
        content = self._content()

        out = await router.polish_content(content)

        assert "failed" not in out
        assert out["qwen"] != content

    async def test_rejected_polish_preserves_evidence_and_reports_reason(self):
        router = SmartAIRouter()
        router.nebius = object()
        section = (
            "## Results\n"
            "The study found 42% growth [1] and a $10K saving. "
            + " ".join(["evidence"] * 90)
        )
        stripped = "## Results\n" + " ".join(["generic"] * 100)

        async def _strips_evidence(messages, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=stripped))]
            )

        router._polish_create = _strips_evidence
        result = await router._polish_section(
            section, 0, 1, router.model_polish
        )

        assert result == section
        assert router._polish_fallback_count == 1
        assert "lost evidence" in router._polish_failure_reason

    async def test_successful_polish_preserves_mdx_and_evidence_signals(self):
        router = SmartAIRouter()
        router.nebius = object()
        section = (
            "## Results\n"
            "Revenue improved 42% [1], saving $10K with a [source](https://example.com). "
            + " ".join(["evidence"] * 90)
        )
        polished = section.replace("improved", "grew")

        async def _preserves_evidence(messages, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=polished))]
            )

        router._polish_create = _preserves_evidence
        result = await router._polish_section(
            section, 0, 1, router.model_polish
        )

        assert result == polished
        assert result.startswith("## Results")
        assert router._evidence_signal_count(result) == router._evidence_signal_count(section)

    async def test_word_count_guard_reports_rejected_output(self):
        router = SmartAIRouter()
        router.nebius = object()
        section = "## Heading\n" + " ".join(["word"] * 100)
        too_long = "## Heading\n" + " ".join(["expanded"] * 180)

        async def _too_long(messages, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=too_long))]
            )

        router._polish_create = _too_long
        result = await router._polish_section(
            section, 0, 1, router.model_polish
        )

        assert result == section
        assert "word count changed too much" in router._polish_failure_reason

    async def test_polish_create_shrinks_max_tokens_when_openrouter_402_reports_affordable_ceiling(self):
        err_402 = _ProviderUnavailableError(
            "Error code: 402 - {'error': {'message': 'This request requires more credits, or fewer max_tokens. You requested up to 8000 tokens, but can only afford 1600.'}}",
            status_code=402,
        )
        completions = _FlakyCompletions(errors=[err_402], responses=["Polished within remaining credit"])
        router = SmartAIRouter()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        router.or_client = client
        router.role_clients["POLISH"] = client
        router._dedicated_role_clients = {}

        resp = await router._polish_create([{"role": "user", "content": "hi"}], max_tokens=8000, purpose="section polish")

        assert router._safe_content(resp) == "Polished within remaining credit"
        assert len(completions.calls) == 2
        assert completions.calls[0]["max_tokens"] == 8000
        assert completions.calls[1]["max_tokens"] == 1536
        assert completions.calls[1]["model"] == router.model_polish
        assert router.models_used["section polish"]["downgraded"] is False

    async def test_polish_create_falls_back_to_generation_model_on_402_byok_mismatch(self):
        err_402 = _ProviderUnavailableError(
            "Error code: 402 - {'error': {'message': 'This request requires more credits, or fewer max_tokens. You requested up to 2500 tokens, but can only afford 0.'}}",
            status_code=402,
        )
        completions = _FlakyCompletions(errors=[err_402], responses=["Polished via OpenAI BYOK", "Second batch via sticky fallback"])
        router = SmartAIRouter()
        router.model_polish = "anthropic/claude-sonnet-5.5"
        router.model_generation = "openai/gpt-6-sol"
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        router.or_client = client
        router.role_clients["POLISH"] = client
        router._dedicated_role_clients = {}

        resp1 = await router._polish_create([{"role": "user", "content": "batch 1"}], max_tokens=2500, purpose="section polish")
        resp2 = await router._polish_create([{"role": "user", "content": "batch 2"}], max_tokens=2500, purpose="section polish")

        assert router._safe_content(resp1) == "Polished via OpenAI BYOK"
        assert router._safe_content(resp2) == "Second batch via sticky fallback"
        assert [c["model"] for c in completions.calls] == [
            "anthropic/claude-sonnet-5.5",
            "openai/gpt-6-sol",
            "openai/gpt-6-sol",
        ]
        assert router.models_used["section polish"]["model"] == "openai/gpt-6-sol"
        assert router.models_used["section polish"]["downgraded"] is True



if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "-s"])
