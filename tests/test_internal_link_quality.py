"""Regression tests for smart internal-link selection and validation (company-agnostic)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.brand_profile import BrandProfile
from core.content_validator import ContentValidator
from core.formatter import AstroFormatter

pytestmark = pytest.mark.unit

ACME = BrandProfile(company_name="Acme Cloud", site_url="https://acme.dev")


@pytest.fixture(autouse=True)
def _mock_brand(monkeypatch):
    monkeypatch.setattr("core.brand_profile.load_brand_profile", lambda *a, **k: ACME)


def _formatter() -> AstroFormatter:
    formatter = AstroFormatter(
        brand_profile=ACME,
        site_context={
            "blog_posts": [
                {
                    "title": "Postgres Branching Benchmarks",
                    "description": "Benchmarks for copy-on-write Postgres branching in CI pipelines.",
                    "url": "https://acme.dev/blog/postgres-branching-benchmarks",
                    "tags": ["postgres", "benchmarks"],
                    "category": "engineering",
                },
                {
                    "title": "Schema Migration Strategies",
                    "description": "Zero-downtime migration patterns for SaaS engineering teams.",
                    "url": "https://acme.dev/blog/schema-migration-strategies",
                    "tags": ["migrations", "schema"],
                    "category": "engineering",
                },
            ],
            "internal_links": {
                "pricing": "https://acme.dev/pricing",
                "integrations": "https://acme.dev/integrations",
            },
            "internal_link_metadata": {
                "https://acme.dev/pricing": {
                    "title": "Pricing",
                    "type": "commercial page",
                    "priority": 8,
                }
            },
        },
    )
    formatter._current_keyword = "postgres branching"
    formatter._current_article_url = "https://acme.dev/blog/current-article"
    return formatter


class TestInternalLinkQualityValidator:
    def test_flags_generic_self_duplicate_and_malformed_links(self):
        validator = ContentValidator()
        content = """
---
title: Test
canonical: https://acme.dev/blog/current-article
---

## [Postgres Branching](https://acme.dev/blog/current-article)

The [standard](https://acme.dev/blog/unrelated-post) dashboard hides latency context.
Teams can read [branch benchmarks](https://acme.dev/blog/postgres-branching) and [branch setup](https://acme.dev/blog/postgres-branching) before using [branch checklist](https://acme.dev/blog/postgres-branching).
A malformed [tracking](https://acme.dev/blog/ci-[tools](https://acme.dev/tools)) link should fail.
"""
        issues = validator.internal_link_text_issues(
            content,
            "postgres branching",
            current_url="https://acme.dev/blog/current-article",
        )

        assert any("generic internal-link anchor" in issue for issue in issues)
        assert any("self-link" in issue for issue in issues)
        assert any("more than twice" in issue for issue in issues)
        assert any("heading" in issue for issue in issues)
        assert any("malformed" in issue for issue in issues)

    def test_passes_sparse_relevant_internal_links_and_scorecard_dimension(self):
        validator = ContentValidator()
        content = """
---
title: Postgres Branching Benchmarks
canonical: https://acme.dev/blog/current-article
---

## Measurement

Teams use [postgres branching benchmarks](https://acme.dev/blog/postgres-branching-benchmarks) to compare CI preview provisioning times across pull requests.
"""
        frontmatter = {
            "title": "Postgres Branching Benchmarks",
            "canonical": "https://acme.dev/blog/current-article",
            "seo": {"title": "Postgres Branching Benchmarks"},
            "faq": [],
        }

        report = validator.validate_internal_link_quality(content, frontmatter, "postgres branching")
        assert report.overall_score == 100

        scorecard = validator.build_scorecard(content, frontmatter, keyword="postgres branching")
        assert "internal_link_quality" in scorecard["dimensions"]


class TestFormatterInternalLinkSelection:
    def test_format_initializes_article_link_state_for_every_article(self):
        formatter = _formatter()
        content = "## Measurement\n\nPostgres branching isolates database schema migrations in CI."

        formatter.format(content, "first article", slug="first-article")
        assert formatter._current_keyword == "first article"
        assert formatter._current_article_url == "https://acme.dev/blog/first-article"

        formatter.format(content, "second article", slug="second-article")
        assert formatter._current_keyword == "second article"
        assert formatter._current_article_url == "https://acme.dev/blog/second-article"

    def test_selects_relevant_specific_anchor_and_rejects_generic_anchor(self):
        formatter = _formatter()
        content = """
## Measurement

Postgres branching benchmarks help teams compare CI runtime and storage overhead. The standard dashboard is not enough.
"""
        linked = formatter._add_contextual_links(content, formatter.link_opportunities)
        assert "[Postgres branching benchmarks](/blog/postgres-branching-benchmarks)" in linked
        assert "[standard]" not in linked

    def test_avoids_self_links_and_caps_repeated_destinations(self):
        formatter = _formatter()
        formatter.link_opportunities["current article"] = {
            "url": "https://acme.dev/blog/current-article",
            "title": "Current Article",
            "anchor_text": "current article",
            "destination_tokens": ["current", "article"],
            "term_tokens": ["current", "article"],
            "priority": 10,
        }
        content = """
## Measurement

Current article references should not self-link. Postgres branching benchmarks appear here. Postgres branching benchmarks appear again. Postgres branching benchmarks appear a third time.
"""
        linked = formatter._add_contextual_links(content, formatter.link_opportunities, max_total_links=5, max_links_per_destination=2)
        assert "[Current article]" not in linked
        assert linked.count("/blog/postgres-branching-benchmarks") <= 2

    def test_skips_fenced_and_inline_code_when_inserting_internal_links(self):
        formatter = _formatter()
        link_map = {
            "Postgres": {
                "url": "/postgres",
                "title": "Postgres",
                "anchor_text": "Postgres",
                "destination_tokens": ["postgres", "branching"],
                "term_tokens": ["postgres"],
                "priority": 12,
            }
        }
        js_example = '```js\nimport Postgres from "postgres";\nconst db = Postgres();\n```'
        content = (
            "## Setup\n\n"
            f"{js_example}\n\n"
            "Install `Postgres` locally, then connect Postgres branching to your preview pipeline.\n"
        )
        linked = formatter._add_contextual_links(content, link_map)
        assert js_example in linked
        assert "`Postgres`" in linked
        assert "connect [Postgres](/postgres) branching" in linked
