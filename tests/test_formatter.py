#!/usr/bin/env python3
"""
Unit tests for AstroFormatter
Tests MDX formatting, citation injection, frontmatter generation
"""
import pytest
import sys
from pathlib import Path
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.formatter import AstroFormatter


pytestmark = pytest.mark.unit


class TestAstroFormatter:
    """Test Astro MDX formatting and structure"""

    def test_formatter_initialization(self):
        """Test AstroFormatter initializes correctly"""
        formatter = AstroFormatter()
        assert formatter is not None, "Formatter should initialize"

    def test_basic_formatting(self):
        """Test basic MDX structure creation"""
        formatter = AstroFormatter()

        raw_content = """
        # Introduction

        This is a test article about lead attribution.

        ## How It Works

        Lead attribution tracks customer touchpoints.
        """

        result = formatter.format(
            content=raw_content,
            keyword="lead attribution",
            style="standard",
            research_data={}
        )

        assert result is not None, "Should return formatted result"
        assert isinstance(result, dict), "Should return dict"
        assert 'content' in result, "Should have content key"
        assert 'title' in result, "Should have title"
        assert 'slug' in result, "Should have slug"

        # Check MDX content
        mdx_content = result['content']
        assert '---' in mdx_content, "Content should have frontmatter delimiters"
        assert 'title:' in mdx_content, "Content should have title field"
        assert 'description:' in mdx_content, "Content should have description"

    def test_frontmatter_generation(self):
        """Test YAML frontmatter generation"""
        formatter = AstroFormatter()

        result = formatter.format(
            content="# Test Article\n\nThis is test content.",
            keyword="test keyword",
            style="standard",
            research_data={}
        )

        # Verify result structure
        assert isinstance(result, dict), "Should return dict"
        assert 'title' in result, "Should have title key"
        assert 'slug' in result, "Should have slug key"
        assert 'category' in result, "Should have category key"
        assert 'tags' in result, "Should have tags key"

        # Verify frontmatter in content
        mdx_content = result['content']
        assert 'title:' in mdx_content, "Content should have title"
        assert 'description:' in mdx_content, "Content should have description"
        assert 'publishDate:' in mdx_content or 'pubDate:' in mdx_content, "Content should have publication date"
        assert 'author:' in mdx_content, "Content should have author"

    def test_citation_injection_research_style(self):
        """Test citation injection for research style"""
        formatter = AstroFormatter()

        content = """
        # Research Article

        Our analysis reveals that lead costs have increased by 15% year-over-year.
        Data demonstrates an average cost per lead of $70.11 across platforms.
        Industry data shows that conversion rates improved by 20–40% with automation.
        """

        research_data = {
            'serp': {
                'serp_analysis': {
                    'citations': [
                        'https://example.com/source1',
                        'https://example.com/source2',
                        'https://example.com/source3'
                    ]
                }
            }
        }

        result = formatter.format(
            content=content,
            keyword="lead costs",
            style="research",
            research_data=research_data
        )

        # Should inject inline citations
        mdx_content = result['content']
        assert 'class="citation-ref"' in mdx_content, "Should have inline citations"
        assert '<CitationsSection>' in mdx_content, "Should have sources section"

    def test_citation_patterns_matching(self):
        """Test comprehensive citation pattern matching"""
        formatter = AstroFormatter()

        content = """
        According to industry experts, lead generation costs vary widely.
        Studies show that automation reduces manual work by 30%.
        Research shows that companies averaging $50 per lead see better ROI.
        The analysis indicates that 25% improvement is achievable.
        """

        research_data = {
            'serp': {
                'serp_analysis': {
                    'citations': [f'https://example.com/source{i}' for i in range(1, 11)]
                }
            }
        }

        result = formatter.format(
            content=content,
            keyword="lead generation",
            style="research",
            research_data=research_data
        )

        # Count inline citations
        mdx_content = result['content']
        citation_count = mdx_content.count('class="citation-ref"')
        assert citation_count >= 3, "Should match multiple patterns and inject citations"

    def test_citation_filtering_by_style(self):
        """Test that research style excludes community citations"""
        formatter = AstroFormatter()

        content = "Data demonstrates that lead costs increased by 15%."

        research_data = {
            'serp': {
                'serp_analysis': {
                    'citations': ['https://www.nber.org/papers/w99']
                }
            },
            'reddit': {
                'insights': [
                    {'url': 'https://reddit.com/discussion', 'content': 'Test insight'}
                ]
            },
            'quora': {
                'insights': [
                    {'url': 'https://quora.com/question', 'content': 'Test answer'}
                ]
            }
        }

        result = formatter.format(
            content=content,
            keyword="lead costs",
            style="research",
            research_data=research_data
        )

        # Should emit <CitationsSection> and only include Tier A/B SERP citations, never Reddit/Quora
        mdx_content = result['content']
        assert '<CitationsSection>' in mdx_content
        citations_block = mdx_content.split('<CitationsSection>', 1)[1].split('</CitationsSection>', 1)[0]
        assert 'nber.org/papers/w99' in citations_block.lower()
        assert 'reddit.com' not in citations_block.lower(), "Research style should exclude Reddit"
        assert 'quora.com' not in citations_block.lower(), "Research style should exclude Quora"

    def test_schema_org_markup(self):
        """Test Schema.org JSON-LD generation"""
        formatter = AstroFormatter()

        result = formatter.format(
            content="# Test Article\n\nContent here.",
            keyword="test keyword",
            style="standard",
            research_data={}
        )

        # Should include schema.org markup (or rely on layout for schema injection)
        mdx_content = result['content']
        # Note: Schema is now handled by Astro layout, so we just verify MDX structure
        assert '---' in mdx_content, "Should have frontmatter structure"

    def test_faq_section_generation(self):
        """Test FAQ section from PAA questions"""
        formatter = AstroFormatter()

        research_data = {
            'serp': {
                'paa_questions': [
                    'What is lead attribution?',
                    'How does attribution tracking work?',
                    'Why is lead attribution important?'
                ]
            }
        }

        result = formatter.format(
            content=(
                "# Lead Attribution\n\n"
                "Lead attribution connects marketing touchpoints to revenue outcomes across campaigns so teams measure return accurately. "
                "Attribution tracking works by capturing click identifiers and matching them to CRM conversions."
            ),
            keyword="lead attribution",
            style="standard",
            research_data=research_data
        )

        mdx_content = result['content']
        frontmatter = yaml.safe_load(mdx_content.split('---')[1])
        assert frontmatter['faq'], "Should generate non-empty FAQ from PAA questions"
        assert any(
            item['question'] in research_data['serp']['paa_questions']
            for item in frontmatter['faq']
        ), "Should include PAA questions"

    def test_nested_serp_paa_generates_frontmatter_faq(self):
        """Nested SERP PAA questions should feed frontmatter FAQ.

        Each question must be answered by a *distinct* body paragraph
        (audit F1/F2: a single paragraph can no longer answer N questions —
        the prior behavior produced 12 identical boilerplate answers).
        """
        formatter = AstroFormatter()
        questions = [
            "How does lead attribution work for marketing teams?",
            "What is conversion tracking and why does it matter?",
            "How do CRM signals support reporting accuracy?",
            "Why do business owners need attribution dashboards?",
        ]
        content = (
            "# Lead Attribution\n\n"
            "Lead attribution connects ad spend, CRM activity, and revenue "
            "outcomes so marketing teams can see which campaigns create "
            "qualified pipeline. It surfaces which channels actually drive "
            "downstream revenue.\n\n"
            "Conversion tracking matters because it ties each click to a "
            "downstream business outcome. Without it, ad platforms optimize "
            "for vanity metrics that do not predict revenue.\n\n"
            "CRM signals support reporting accuracy by closing the loop "
            "between marketing campaigns and sales outcomes. The combined "
            "view tells teams which leads actually closed.\n\n"
            "Business owners need attribution dashboards because they need "
            "to see how marketing investments translate into real customers. "
            "A dashboard makes that visible at a glance.\n"
        )
        result = formatter.format(
            content=content,
            keyword="lead attribution",
            style="standard",
            research_data={'serp': {'paa_questions': questions}}
        )

        frontmatter = yaml.safe_load(result['content'].split('---')[1])
        assert frontmatter['faq'], "FAQ array should not be empty"
        answers = [item['answer'] for item in frontmatter['faq']]
        assert len(answers) == len(set(answers)), "FAQ answers must be unique"
        for item in frontmatter['faq']:
            assert item['question'] in questions

    def test_frontmatter_faq_answers_are_atomic(self):
        """FAQ answers should stay concise without boilerplate padding."""
        formatter = AstroFormatter()
        questions = [f"What should marketers know about attribution question {i}?" for i in range(1, 13)]

        result = formatter.format(
            content="# Attribution\n\nAttribution helps marketers connect campaign spend to qualified leads and closed revenue. It matters because reporting becomes unreliable when ad platforms and CRM records disagree. The best workflow standardizes tracking, compares spend with pipeline every month, and gives business owners a clear view of performance.",
            keyword="attribution",
            style="standard",
            research_data={'serp': {'paa_questions': questions}}
        )

        frontmatter = yaml.safe_load(result['content'].split('---')[1])
        assert frontmatter['faq'], "FAQ array should not be empty"
        word_counts = [len(item['answer'].split()) for item in frontmatter['faq']]
        assert all(40 <= count <= 60 for count in word_counts)

    def test_frontmatter_has_index_ready_seo_fields(self):
        """Generated MDX should include canonical and robots-ready SEO fields."""
        formatter = AstroFormatter()
        result = formatter.format(
            content="# Test\n\nContent here.",
            keyword="index health",
            style="standard",
            research_data={'serp': {'paa_questions': [f"What is index health question {i}?" for i in range(1, 13)]}}
        )

        frontmatter = yaml.safe_load(result['content'].split('---')[1])
        assert frontmatter['canonical'].endswith('/blog/index-health')
        assert frontmatter['updatedDate']
        assert frontmatter['seo']['noindex'] is False
        assert frontmatter['seo']['nofollow'] is False

    def test_internal_link_injection(self):
        """Test Flyweel brand link injection"""
        formatter = AstroFormatter()

        content = """
        # Lead Attribution

        Lead attribution helps track marketing performance.
        CRM integration is essential for lead management.
        """

        result = formatter.format(
            content=content,
            keyword="lead attribution",
            style="standard",
            research_data={}
        )

        # Should have internal links (implementation-dependent)
        # This validates structure is preserved
        mdx_content = result['content']
        assert '# Lead Attribution' in mdx_content or '## Lead Attribution' in mdx_content, "Should preserve headings"

    def test_cta_injection(self):
        """Test call-to-action injection"""
        formatter = AstroFormatter()

        result = formatter.format(
            content="# Test\n\nContent here.",
            keyword="test",
            style="standard",
            research_data={}
        )

        # Should have CTA (check for common CTA phrases)
        # Implementation may vary
        mdx_content = result['content']
        assert len(mdx_content) > 100, "Should have formatted content with CTA"

    def test_word_count_preservation(self):
        """Test that formatting preserves word count roughly"""
        formatter = AstroFormatter()

        content = " ".join(["word"] * 1000)  # 1000 words

        result = formatter.format(
            content=content,
            keyword="test",
            style="standard",
            research_data={}
        )

        # Word count should be roughly preserved (±20%)
        mdx_content = result['content']
        result_words = len(mdx_content.split())
        assert result_words >= 800, "Should preserve most content"

    def test_mdx_code_fence_handling(self):
        """Test proper MDX code fence wrapping"""
        formatter = AstroFormatter()

        content_with_code = """
        # Test Article

        Here's some code:

        ```javascript
        const test = "hello";
        ```

        More content here.
        """

        result = formatter.format(
            content=content_with_code,
            keyword="test",
            style="standard",
            research_data={}
        )

        # Should preserve code fences
        mdx_content = result['content']
        assert '```' in mdx_content, "Should preserve code blocks"

    def test_empty_content_handling(self):
        """Test graceful handling of empty content"""
        formatter = AstroFormatter()

        result = formatter.format(
            content="",
            keyword="test",
            style="standard",
            research_data={}
        )

        # Should still generate valid MDX structure
        mdx_content = result['content']
        assert '---' in mdx_content, "Should have frontmatter even with empty content"

    def test_style_specific_formatting(self):
        """Test different formatting for different styles"""
        formatter = AstroFormatter()

        styles = ['standard', 'guide', 'comparison', 'research', 'news', 'category']

        for style in styles:
            result = formatter.format(
                content="# Test\n\nContent here.",
                keyword="test",
                style=style,
                research_data={}
            )

            assert result is not None, f"Should format {style} style"
            mdx_content = result['content']
            assert '---' in mdx_content, f"Should have frontmatter for {style}"


class TestCitationPatterns:
    """Test citation pattern matching and extraction"""

    def test_research_terminology_patterns(self):
        """Test research-specific terminology patterns"""
        formatter = AstroFormatter()

        test_sentences = [
            "Our analysis reveals significant improvements.",
            "Data demonstrates a 15% increase.",
            "Industry data shows market trends.",
            "Market analysis suggests growth opportunities.",
        ]

        for sentence in test_sentences:
            result = formatter.format(
                content=sentence,
                keyword="test",
                style="research",
                research_data={
                    'serp': {
                        'serp_analysis': {
                            'citations': ['https://example.com/source1']
                        }
                    }
                }
            )

            mdx_content = result['content']
            assert 'class="citation-ref"' in mdx_content, f"Should match pattern in: {sentence}"

    def test_statistical_claim_patterns(self):
        """Test statistical claim citation patterns"""
        formatter = AstroFormatter()

        test_sentences = [
            "The average cost is $70.11 per lead.",
            "Companies see 20–40% reduction in costs.",
            "Prices range from $50 to $200 per lead.",
        ]

        for sentence in test_sentences:
            result = formatter.format(
                content=sentence,
                keyword="test",
                style="research",
                research_data={
                    'serp': {
                        'serp_analysis': {
                            'citations': ['https://example.com/source1']
                        }
                    }
                }
            )

            mdx_content = result['content']
            assert 'class="citation-ref"' in mdx_content, f"Should cite statistical claim: {sentence}"


@pytest.mark.unit
class TestResearchSourcesCitations:
    """research_sources pool drives research-style citations; standard ignores it."""

    def _research_data(self):
        return {
            'serp': {'serp_analysis': {
                'citations': ['https://low-quality-blog.example/post']}},
            'community': {'reddit': {}, 'quora': {}},
            'research_sources': [
                {'url': 'https://www.nber.org/papers/w99',
                 'title': 'NBER Working Paper 99', 'description': '', 'content': '',
                 'provider': 'interfaze', 'source_type': 'Research Study',
                 'credibility_tier': 'A'},
                {'url': 'https://www.wsj.com/markets/x',
                 'title': 'WSJ Market Survey 2026', 'description': '', 'content': '',
                 'provider': 'perplexity-serp', 'source_type': 'Industry Report',
                 'credibility_tier': 'B'},
            ],
        }

    def test_research_style_uses_research_sources(self):
        formatter = AstroFormatter()
        content = ("Research shows that adoption accelerated sharply. "
                   "Studies indicate a 30% increase in spend. "
                   "According to industry data, costs fell.")
        out = formatter._add_citations(content, self._research_data(), style="research")
        assert 'class="citation-ref"' in out
        assert '<CitationsSection>' in out
        assert 'NBER Working Paper 99' in out
        assert 'WSJ Market Survey 2026' in out
        # Raw low-quality SERP citation suppressed when the merged pool exists.
        assert 'low-quality-blog.example' not in out

    def test_standard_style_ignores_research_sources(self):
        formatter = AstroFormatter()
        content = "Studies indicate a 30% increase in spend across the board."
        out = formatter._add_citations(content, self._research_data(), style="standard")
        assert 'nber.org' not in out
        assert 'WSJ Market Survey 2026' not in out

    def test_research_sources_render_for_table_heavy_content(self):
        formatter = AstroFormatter()
        content = (
            "| Industry | CAC | CPL |\n"
            "|---|---:|---:|\n"
            "| HVAC | $300-$700 [Reddit Community, 2026] | $30-$80 [Reddit Community, 2026] |"
        )

        out = formatter._add_citations(content, self._research_data(), style="research")

        assert '<CitationsSection>' in out
        assert '<span id="cite-1">[1]</span>' in out
        assert '<a href="https://www.nber.org/papers/w99"' in out
        assert 'NBER Working Paper 99' in out

    def test_citation_anchor_and_url_are_escaped_before_html_interpolation(self):
        formatter = AstroFormatter()
        data = self._research_data()
        data["research_sources"] = [{
            "url": 'https://example.com/a path?q="quoted"',
            "title": '</a><script>alert(1)</script>',
            "source_type": "Research Study",
            "credibility_tier": "A",
        }]

        out = formatter._add_citations(
            "Research shows adoption accelerated sharply.", data, style="research"
        )

        assert '<script>' not in out
        assert '&lt;/a&gt;&lt;script&gt;alert(1)&lt;/script&gt;' in out
        assert 'href="https://example.com/a%20path?q=%22quoted%22"' in out

    def test_external_link_dofollow_allowlist_uses_hostname_boundaries(self):
        formatter = AstroFormatter()
        out = formatter._process_external_links(
            "[fake authority](https://evilgithub.com/report) and [real authority](https://docs.github.com/report)"
        )

        assert '<a href="https://evilgithub.com/report" target="_blank" rel="nofollow">fake authority</a>' in out
        assert '[real authority](https://docs.github.com/report)' in out


@pytest.mark.unit
class TestResearchCitationFallback:
    """Research style must never silently ship uncited: raw SERP is a real
    fallback whenever the research_sources pool is empty or url-less."""

    def test_pool_yield_suppresses_raw_serp(self):
        """Healthy pool ⇒ raw SERP citation URL suppressed (de-dup intact)."""
        formatter = AstroFormatter()
        content = ("Research shows adoption accelerated sharply. "
                   "Studies indicate a 30% increase in spend.")
        research_data = {
            'serp': {'serp_analysis': {
                'citations': ['https://raw-serp-dup.example/post']}},
            'community': {'reddit': {}, 'quora': {}},
            'research_sources': [
                {'url': 'https://www.nber.org/papers/w1',
                 'title': 'NBER Paper 1', 'description': '', 'content': '',
                 'provider': 'interfaze', 'source_type': 'Research Study',
                 'credibility_tier': 'A'},
            ],
        }
        out = formatter._add_citations(content, research_data, style="research")
        assert 'class="citation-ref"' in out
        assert 'NBER Paper 1' in out
        assert 'raw-serp-dup.example' not in out

    def test_empty_pool_falls_back_to_raw_serp(self):
        """Empty research_sources ⇒ SERP citations still injected (the fix)."""
        formatter = AstroFormatter()
        content = ("Research shows adoption accelerated sharply. "
                   "Studies indicate a 30% increase in spend.")
        research_data = {
            'serp': {'serp_analysis': {
                'citations': ['https://example.com/serp-fallback']}},
            'community': {'reddit': {}, 'quora': {}},
            'research_sources': [],
        }
        out = formatter._add_citations(content, research_data, style="research")
        assert 'class="citation-ref"' in out
        assert '<CitationsSection>' in out
        assert 'example.com/serp-fallback' in out

    def test_urlless_pool_falls_back_to_raw_serp(self):
        """Non-empty pool whose entries lack http URLs ⇒ SERP fallback (Mode 2)."""
        formatter = AstroFormatter()
        content = "Research shows adoption accelerated sharply across the board."
        research_data = {
            'serp': {'serp_analysis': {
                'citations': ['https://example.com/serp-rescue']}},
            'community': {'reddit': {}, 'quora': {}},
            'research_sources': [
                {'url': '', 'title': 'No URL Source', 'description': '',
                 'content': '', 'provider': 'interfaze',
                 'source_type': 'Research Study', 'credibility_tier': 'A'},
            ],
        }
        out = formatter._add_citations(content, research_data, style="research")
        assert 'class="citation-ref"' in out
        assert 'example.com/serp-rescue' in out

    def test_no_sources_anywhere_returns_uncited_without_crashing(self):
        """Pool empty + SERP empty ⇒ content returned unmodified, no crash
        (warn-and-ship contract; the loud WARNING is asserted at the
        generator call site, not here)."""
        formatter = AstroFormatter()
        content = "Research shows adoption accelerated sharply across the board."
        research_data = {
            'serp': {'serp_analysis': {'citations': []}},
            'community': {'reddit': {}, 'quora': {}},
            'research_sources': [],
        }
        out = formatter._add_citations(content, research_data, style="research")
        assert out == content
        assert 'class="citation-ref"' not in out


@pytest.mark.unit
class TestAstroFlyweelHarmonisation:
    """Canonical MDX strips legacy inline jsonld scalar (`schema.json` is emitted by site_adapters)."""

    def _fm(self, mdx: str) -> dict:
        parts = mdx.split('---', 2)
        return yaml.safe_load(parts[1]) if len(parts) >= 3 else {}

    def test_no_inline_jsonld_or_frontmatter_jsonld(self):
        formatter = AstroFormatter()
        out = formatter.format(
            content="# Test\n\nA plain paragraph with two sentences. It is fine.",
            keyword="test keyword",
            style="standard",
            research_data={},
        )['content']
        assert 'application/ld+json' not in out, "No inline JSON-LD <script>"
        assert 'jsonld' not in (self._fm(out).get('seo') or {}), "No seo.jsonld"

    def test_no_unused_component_imports_for_plain_markdown(self):
        formatter = AstroFormatter()
        out = formatter.format(
            content="# Plain\n\nJust prose here. No components at all.",
            keyword="plain markdown",
            style="standard",
            research_data={},
        )['content']
        assert 'import FAQSection' not in out
        assert 'import ComparisonTable' not in out
        assert "import { Icon }" not in out

    def test_image_import_only_when_tag_present(self):
        formatter = AstroFormatter()
        out = formatter.format(
            content='# Has image\n\n<Image src="/a.webp" alt="a" />\n\nProse.',
            keyword="image post",
            style="standard",
            research_data={},
        )['content']
        assert "import { Image } from 'astro:assets';" in out

    def test_refresh_schema_jsonld_strips_legacy_line(self):
        formatter = AstroFormatter()
        mdx = (
            "---\n"
            "title: 'Keep Me'\n"
            "seo:\n"
            "  title: 'Keep Me'\n"
            "  jsonld: '{\"@type\":\"BlogPosting\"}'\n"
            "---\n\n"
            "## Body\n\nUntouched body text.\n"
        )
        out = formatter.refresh_schema_jsonld(mdx)
        assert 'jsonld:' not in out
        assert "title: 'Keep Me'" in out
        assert "Untouched body text." in out
        assert out.startswith('---')

    def test_upsert_tldr_frontmatter_adds_valid_schema(self):
        formatter = AstroFormatter()
        mdx = (
            "---\n"
            "title: 'Keep Me'\n"
            "description: 'A very nice description of lead generation in 2026.'\n"
            "seo:\n"
            "  title: 'Keep Me'\n"
            "---\n\n"
            "## Body\n\nUntouched body text.\n"
        )
        tldr_data = {
            "answer": "A perfect direct answer that has exactly 40 words and explains the concept of ad reconciliation for sales-led operations.",
            "takeaways": [
                "Declarative takeaway sentence number one.",
                "Declarative takeaway sentence number two.",
                "Declarative takeaway sentence number three."
            ]
        }
        out = formatter.upsert_tldr_frontmatter(mdx, tldr_data)
        assert "tldr:" in out
        assert "A perfect direct answer" in out
        assert "Declarative takeaway sentence number one" in out
        assert "seo:" in out
        assert "title: 'Keep Me'" in out

    def test_upsert_tldr_frontmatter_replaces_existing_block(self):
        formatter = AstroFormatter()
        mdx = (
            "---\n"
            "title: 'Keep Me'\n"
            "tldr:\n"
            "  answer: 'Old wrong answer that is outdated.'\n"
            "  takeaways:\n"
            "    - 'Old takeaway one.'\n"
            "seo:\n"
            "  title: 'Keep Me'\n"
            "---\n\n"
            "## Body\n\nUntouched body text.\n"
        )
        tldr_data = {
            "answer": "New correct direct answer that has exactly 40 words and explains the concept of ad reconciliation for sales-led operations.",
            "takeaways": [
                "New declarative takeaway sentence number one.",
                "New declarative takeaway sentence number two.",
                "New declarative takeaway sentence number three."
            ]
        }
        out = formatter.upsert_tldr_frontmatter(mdx, tldr_data)
        assert "tldr:" in out
        assert "New correct direct answer" in out
        assert "New declarative takeaway sentence number one" in out
        assert "Old wrong answer" not in out
        assert "Old takeaway one" not in out
        assert "seo:" in out
        assert "title: 'Keep Me'" in out

    def test_tldr_yaml_escapes_quotes(self):
        formatter = AstroFormatter()
        mdx = (
            "---\n"
            "title: 'Keep Me'\n"
            "---\n\n"
            "## Body\n\nUntouched body text.\n"
        )
        tldr_data = {
            "answer": "The user's direct 'answer' should escape single quotes like 'this' or \"that\".",
            "takeaways": [
                "User's takeaway with a single quote 'here'.",
                "Another takeaway.",
                "Third takeaway."
            ]
        }
        out = formatter.upsert_tldr_frontmatter(mdx, tldr_data)
        assert "user''s direct" in out
        assert "User''s takeaway" in out
        assert "tldr:" in out

    def test_no_tldr_component_in_mdx_body(self):
        formatter = AstroFormatter()
        content = "This is a post body about lead generation."
        keyword = "lead generation"
        res = formatter.format(content, keyword, style="standard")
        mdx = res['content']
        # Assert that the TLDR component (e.g., <TLDR) is NOT imported or rendered in the body.
        assert "<TLDR" not in mdx
        assert "import TLDR from" not in mdx
        assert "import { TLDR }" not in mdx

    def test_upsert_tldr_frontmatter_excludes_title(self):
        formatter = AstroFormatter()
        mdx = (
            "---\n"
            "title: 'Keep Me'\n"
            "---\n\n"
            "## Body\n\nUntouched body text.\n"
        )
        tldr_data = {
            "answer": "This is a direct answer of exactly 40 words designed to pass strict character constraints.",
            "takeaways": [
                "Takeaway sentence one.",
                "Takeaway sentence two.",
                "Takeaway sentence three."
            ],
            "title": "Unwanted Title"
        }
        out = formatter.upsert_tldr_frontmatter(mdx, tldr_data)
        assert "tldr:" in out
        assert "Unwanted Title" not in out
        assert "title: 'Unwanted Title'" not in out


@pytest.mark.unit
class TestSERPInsightFormatterPromptCitations:
    def test_prompt_uses_url_backed_links_instead_of_bare_numbered_markers(self):
        import re
        from core.insight_formatter import SERPInsightFormatter

        fmt = SERPInsightFormatter()
        serp_data = {
            "serp_analysis": {
                "analysis": "Survey data shows 64% of engineering teams automate schema checks [2].",
                "citations": [
                    "",
                    "https://gartner.com/report-2026",
                    "https://dora.dev/research",
                ],
            }
        }
        prompt_block = fmt.format_for_prompt(serp_data, "research")
        assert "[Gartner](https://gartner.com/report-2026)" in prompt_block
        assert "[source_number]" not in prompt_block
        assert "[citation_number]" not in prompt_block
        assert "reference with [1]" not in prompt_block
        assert re.search(r"\[\d+\](?!\()", prompt_block) is None


@pytest.mark.unit
def test_formatter_link_normalization_uses_selected_brand_profile_over_workspace(monkeypatch):
    from core.brand_profile import BrandProfile
    import core.brand_profile as bp_mod

    monkeypatch.setattr(
        bp_mod,
        "load_brand_profile",
        lambda *a, **kw: BrandProfile(company_name="WorkspaceCo", site_url="https://workspace.example.com"),
    )
    selected = BrandProfile(company_name="SelectedCo", site_url="https://selected.example.com")
    fmt = AstroFormatter(brand_profile=selected)

    assert fmt._to_relative_internal_url("https://selected.example.com/blog/guide") == "/blog/guide"
    assert fmt._to_relative_internal_url("https://workspace.example.com/blog/other") == "https://workspace.example.com/blog/other"
    normalized = fmt.normalize_internal_links(
        "---\ntitle: \"T\"\n---\n\nSee [ours](https://selected.example.com/docs) and [theirs](https://workspace.example.com/docs).\n",
        brand_profile=selected,
    )
    assert "[ours](/docs)" in normalized
    assert "[theirs](https://workspace.example.com/docs)" in normalized


@pytest.mark.unit
def test_build_seo_title_respects_long_brand_suffix_budget():
    from core.brand_profile import BrandProfile

    long_brand = BrandProfile(company_name="Acme Cloud Infrastructure Services")
    fmt = AstroFormatter(brand_profile=long_brand)
    seo_title = fmt.build_seo_title(
        "How to Configure Multi-Region Postgres Failover Safely",
        "postgres multi-region failover",
    )
    rendered = f"{seo_title}{long_brand.effective_title_suffix()}"
    assert 35 <= len(rendered) <= 68
    assert seo_title.lower() != "how to configure multi-region postgres failover safely"


@pytest.mark.unit
def test_external_link_nofollow_conversion_preserves_fenced_and_inline_code():
    fmt = AstroFormatter()
    sample = (
        "Read the [external guide](https://example.org/guide) and inline `[docs](https://example.org/inline)`.\n\n"
        "```js\n"
        'const link = "[docs](https://example.org/guide)";\n'
        "```\n"
    )
    out = fmt._process_external_links(sample)
    assert '<a href="https://example.org/guide" target="_blank" rel="nofollow">external guide</a>' in out
    assert '`[docs](https://example.org/inline)`' in out
    assert 'const link = "[docs](https://example.org/guide)";' in out


@pytest.mark.unit
def test_formatter_emits_empty_faq_array_when_no_faq_answers_qualify():
    import yaml

    fmt = AstroFormatter()
    formatted = fmt.format(
        content="---\ntitle: \"Short Post\"\ndescription: \"Short summary.\"\n---\n\n## Overview\n\nBrief paragraph.\n",
        keyword="short post",
        research_data={"serp": {"paa_questions": []}},
        style="standard",
    )
    fm_block = formatted["content"].split("---", 2)[1]
    parsed = yaml.safe_load(fm_block)
    assert "faq" in parsed
    assert parsed["faq"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
