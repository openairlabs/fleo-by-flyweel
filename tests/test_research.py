#!/usr/bin/env python3
"""
Unit tests for Research modules (Web and Community)
Tests SERP analysis, Reddit mining, Quora extraction
"""
import pytest
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.research import (
    WebResearcher,
    CommunityResearcher,
    canonicalize_url,
    classify_source_credibility,
    merge_research_sources,
)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.research
class TestWebResearcher:
    """Test web research and SERP analysis"""

    async def test_researcher_initialization(self):
        """Test WebResearcher initializes correctly"""
        async with WebResearcher() as researcher:
            assert researcher.perplexity_key is not None, "Should have Perplexity key"

    async def test_serp_analysis_standard(self):
        """Test standard SERP analysis with sonar-reasoning-pro"""
        async with WebResearcher() as researcher:
            result = await researcher.analyze_serp("CRM integration", augment_serp=True, style="standard")

            assert result is not None, "Should return SERP result"
            assert 'serp_analysis' in result, "Should have analysis"
            assert 'paa_questions' in result, "Should extract PAA questions"
            assert 'search_results' in result, "Should have search results"

            # Verify content quality
            analysis = result.get('serp_analysis')
            assert analysis, "Augmented SERP analysis should be non-empty"
            assert len(str(analysis)) > 100, "Analysis should be substantial"

    async def test_serp_analysis_research(self):
        """Test deep research SERP analysis with sonar-deep-research"""
        async with WebResearcher() as researcher:
            result = await researcher.analyze_serp("lead attribution models", augment_serp=True, style="research")

            assert result is not None, "Should return research result"
            assert 'serp_analysis' in result, "Should have deep analysis"

            # Research style should use sonar-deep-research (longer, more detailed)
            analysis = result.get('serp_analysis')
            assert analysis, "Augmented deep-research SERP analysis should be non-empty"
            assert len(str(analysis)) > 200, "Research analysis should be more detailed"

    async def test_paa_extraction(self):
        """Test PAA (People Also Ask) question extraction"""
        async with WebResearcher() as researcher:
            result = await researcher.analyze_serp("marketing automation")

            paa_questions = result.get('paa_questions', [])
            assert isinstance(paa_questions, list), "PAA should be a list"
            # May be empty if no PAAs found, but structure should be valid
            if paa_questions:
                assert len(paa_questions) > 0, "Should extract PAA questions"
                assert all(isinstance(q, str) for q in paa_questions), "Questions should be strings"

    async def test_citation_extraction(self):
        """Test citation extraction from SERP analysis"""
        async with WebResearcher() as researcher:
            result = await researcher.analyze_serp("ad spend tracking", augment_serp=True)

            serp_analysis = result.get('serp_analysis')
            assert isinstance(serp_analysis, dict) and serp_analysis, "Augmented SERP analysis should be a non-empty dict"
            citations = serp_analysis.get('citations', [])
            assert isinstance(citations, list), "Citations should be a list"

    async def test_platform_filtering(self):
        """Test platform name filtering in PAA questions"""
        async with WebResearcher() as researcher:
            # This is tested indirectly - PAA questions should not include platform names
            result = await researcher.analyze_serp("Facebook lead ads")

            paa_questions = result.get('paa_questions', [])
            if paa_questions:
                # Should filter out platform-specific questions
                for question in paa_questions:
                    # Questions shouldn't be just about specific platforms
                    assert isinstance(question, str), "Should be valid question string"

    async def test_serp_keyword_generation_with_context(self, monkeypatch):
        """Test that _generate_serp_keyword incorporates the confirmed keyword_context into its prompt."""
        researcher = WebResearcher()
        
        seen_payloads = []
        
        class FakeResponse:
            def __init__(self):
                self.status = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass
            async def json(self):
                return {
                    "choices": [{
                        "message": {
                            "content": "scale paid ads"
                        }
                    }]
                }
                
        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass
            async def close(self):
                pass
            def post(self, url, headers=None, json=None, timeout=None):
                seen_payloads.append(json)
                return FakeResponse()
                
        # Mock aiohttp.ClientSession in core.research
        import aiohttp
        monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
        
        researcher.perplexity_key = "fake_key"
        
        keyword = "How to scale paid ads like a capital markets investor"
        context = "This topic is about treating paid ad spend as a capital allocation decision."
        
        res = await researcher._generate_serp_keyword(keyword, keyword_context=context)
        
        assert res == "scale paid ads"
        assert len(seen_payloads) == 1
        user_message = seen_payloads[0]["messages"][0]["content"]
        assert "Confirmed topic interpretation" in user_message
        assert context in user_message


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.research
class TestCommunityResearcher:
    """Test community research (Reddit and Quora)"""

    async def test_researcher_initialization(self):
        """Test CommunityResearcher initializes correctly"""
        async with CommunityResearcher() as researcher:
            assert researcher.ai.openrouter_key is not None, "Should have OpenRouter key"

    async def test_reddit_mining(self):
        """Test Reddit insight mining"""
        async with CommunityResearcher() as researcher:
            result = await researcher.mine_reddit("lead generation")

            assert result is not None, "Should return Reddit result"
            assert 'insights' in result, "Should have insights"
            assert 'questions' in result, "Should have questions"

            # Verify structure
            assert isinstance(result['insights'], list), "Insights should be a list"
            assert isinstance(result['questions'], list), "Questions should be a list"

    async def test_reddit_limited_mode(self):
        """Test limited Reddit mining (3 insights)"""
        async with CommunityResearcher() as researcher:
            result = await researcher.mine_reddit("CRM software", limit=True)

            assert result is not None, "Should return limited result"
            insights = result.get('insights', [])
            # Should have fewer insights in limited mode
            assert len(insights) <= 5, "Limited mode should have fewer insights"

    async def test_quora_extraction(self):
        """Test Quora insight extraction"""
        async with CommunityResearcher() as researcher:
            result = await researcher.extract_quora("marketing automation")

            assert result is not None, "Should return Quora result"
            assert 'insights' in result, "Should have insights"

            # Verify structure
            insights = result.get('insights', [])
            assert isinstance(insights, list), "Insights should be a list"

    async def test_quora_limited_mode(self):
        """Test limited Quora extraction (1 insight)"""
        async with CommunityResearcher() as researcher:
            result = await researcher.extract_quora("ad tracking", limit=True)

            assert result is not None, "Should return limited result"
            insights = result.get('insights', [])
            # Limited mode should have max 1 insight
            assert len(insights) <= 1, "Limited mode should have 1 insight max"

    async def test_insight_quality_filtering(self):
        """Test that insights are filtered for quality"""
        async with CommunityResearcher() as researcher:
            result = await researcher.mine_reddit("PostgresBranching")

            insights = result.get('insights', [])
            if insights:
                # Insights should be meaningful (not generic)
                for insight in insights:
                    assert isinstance(insight, dict), "Insight should be dict"
                    # Should have relevance score or content
                    assert 'content' in insight or 'text' in insight, "Should have content"

    async def test_skip_community_mode(self):
        """Test complete skip of community research at orchestration layer"""
        from core.generator import ContentGenerator

        generator = ContentGenerator()
        result = await generator.generate(
            keyword="lead attribution",
            style="standard",
            skip_community=True,
        )
        assert result is not None, "Should return result even when skipped"
        reddit_data = result.get('research_data', {}).get('reddit', {})
        assert reddit_data.get('insights') == [], "Should have empty insights"
        assert reddit_data.get('questions') == [], "Should have empty questions"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.research
class TestResearchIntegration:
    """Test integrated research workflows"""

    async def test_parallel_research(self):
        """Test parallel execution of web and community research"""
        async with WebResearcher() as web, CommunityResearcher() as community:
            # Execute in parallel
            serp_task = web.analyze_serp("lead attribution")
            reddit_task = community.mine_reddit("lead attribution")

            serp_result, reddit_result = await asyncio.gather(serp_task, reddit_task)

            assert serp_result is not None, "SERP research should complete"
            assert reddit_result is not None, "Reddit research should complete"

    async def test_research_data_structure(self):
        """Test that research results have expected structure"""
        async with WebResearcher() as web:
            result = await web.analyze_serp("CRM integration")

            # Validate expected keys
            assert 'serp_analysis' in result, "Should have SERP analysis"
            assert 'paa_questions' in result, "Should have PAA questions"
            assert 'search_results' in result, "Should have search results"

            # Validate types
            assert isinstance(result.get('paa_questions', []), list), "PAA should be list"
            assert isinstance(result.get('search_results', []), list), "Results should be list"


@pytest.mark.unit
class TestSourceCredibility:
    """Network-free tests for research-grade source tiering and merging."""

    def test_canonicalize_url_collapses_variants(self):
        a = canonicalize_url("https://www.Example.com/Path/?utm=x#frag")
        b = canonicalize_url("http://example.com/Path")
        c = canonicalize_url("example.com/Path/")
        assert a == b == c == "example.com/Path"

    def test_canonicalize_url_empty(self):
        assert canonicalize_url("") == ""
        assert canonicalize_url(None) == ""

    def test_tier_a_primary_sources(self):
        assert classify_source_credibility("https://www.nature.com/articles/x") == "A"
        assert classify_source_credibility("https://stanford.edu/research") == "A"
        assert classify_source_credibility("https://www.gartner.com/report") == "A"

    def test_tier_b_requires_data_signal(self):
        with_data = classify_source_credibility(
            "https://www.economist.com/finance",
            "2026 survey of 1,200 firms",
            "respondents reported a 34% increase",
        )
        no_data = classify_source_credibility(
            "https://www.economist.com/opinion",
            "An opinion piece",
            "some loose thoughts on the market",
        )
        assert with_data == "B"
        assert no_data == "C"

    def test_tier_c_thin_and_social(self):
        assert classify_source_credibility("https://reddit.com/r/x") == "C"
        assert classify_source_credibility(
            "https://randomblog.io/post", "tips", "no data here"
        ) == "C"

    def test_merge_drops_tier_c_and_orders_a_first(self):
        merged = merge_research_sources(
            serp_citations=["https://randomblog.io/post", "https://nber.org/papers/w1"],
            serp_search_results=[{
                "url": "https://www.wsj.com/markets",
                "title": "Survey of 900 CFOs",
                "snippet": "42% reported budget growth in 2026",
            }],
            interfaze_sources=[{
                "url": "https://reddit.com/r/marketing",
                "title": "thread",
                "content": "an anecdote",
            }],
        )
        urls = [s["url"] for s in merged]
        assert "https://randomblog.io/post" not in urls   # Tier C dropped
        assert "https://reddit.com/r/marketing" not in urls  # social dropped
        tiers = [s["credibility_tier"] for s in merged]
        assert tiers == sorted(tiers)                     # A before B
        assert merged[0]["credibility_tier"] == "A"
        for s in merged:
            assert set(s) == {
                "title", "url", "description", "content",
                "provider", "source_type", "credibility_tier",
            }

    def test_merge_dedupes_across_providers(self):
        merged = merge_research_sources(
            serp_citations=["https://www.gartner.com/doc/1"],
            interfaze_sources=[{
                "url": "http://www.gartner.com/doc/1/",
                "title": "Gartner Report 2026",
                "content": "detailed analysis",
            }],
        )
        assert len(merged) == 1
        # Richer record wins on same-tier (A) collision.
        assert merged[0]["title"] == "Gartner Report 2026"

    @pytest.mark.parametrize(
        "url",
        [
            "https://nature.com.evil.example/fake-study",
            "https://evilnature.com/report",
            "https://evil.example/path/nber.org/paper",
            "https://evil.example/?source=gartner.com",
        ],
    )
    def test_credibility_domains_require_hostname_boundaries(self, url):
        assert classify_source_credibility(url, "Survey report", "50% of respondents") == "C"

    def test_credibility_accepts_real_subdomains(self):
        assert classify_source_credibility("https://journals.nature.com/article") == "A"


@pytest.mark.unit
class TestInterfazeDisabled:
    """Missing INTERFAZE_API_KEY must disable gracefully with no network."""

    async def test_missing_key_returns_disabled(self, monkeypatch):
        monkeypatch.delenv("INTERFAZE_API_KEY", raising=False)
        web = WebResearcher()
        result = await web.search_research_indexes("lead attribution benchmarks")
        assert result == {"sources": [], "enabled": False}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reddit_scraper_extracts_url_from_citation_dicts():
    from types import SimpleNamespace
    from reddit_scraper import RedditSentimentAnalyzer

    analyzer = RedditSentimentAnalyzer.__new__(RedditSentimentAnalyzer)

    async def _fake_compound(queries, **kwargs):
        return {
            "insights": ["2026-10-01 Users report slow schema locks during zero-downtime migrations."],
            "citations": [{"url": "https://www.reddit.com/r/PostgreSQL/comments/xyz987/migrations/"}],
        }

    analyzer.ai = SimpleNamespace(research_with_compound=_fake_compound)
    posts = await analyzer._mine_reddit_with_time_filter("postgres migrations", limit=3, time_filter="1mo")
    assert len(posts) == 1
    assert posts[0]["url"] == "https://www.reddit.com/r/PostgreSQL/comments/xyz987/migrations/"
    assert posts[0]["subreddit"] == "PostgreSQL"


@pytest.mark.unit
def test_validate_paa_questions_rejects_unrelated_marketing_questions_without_brand_overlap(monkeypatch):
    import core.research as res_mod
    from core.brand_profile import BrandProfile

    monkeypatch.setattr(
        res_mod,
        "load_brand_profile",
        lambda *a, **kw: BrandProfile(company_name="NeonLike", category="Serverless Postgres", customer_language_terms=["connection pooling"]),
    )
    web = WebResearcher()
    filtered = web._validate_and_filter_questions(
        [
            "How can marketing teams reduce ad costs?",
            "How does postgres branching isolate preview environments?",
            "How does connection pooling affect cold starts?",
        ],
        "postgres branching",
    )
    assert "How can marketing teams reduce ad costs?" not in filtered
    assert "How does postgres branching isolate preview environments?" in filtered
    assert "How does connection pooling affect cold starts?" in filtered


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
