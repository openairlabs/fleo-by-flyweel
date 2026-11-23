"""Unit tests for the unified FAQ builder (audit F1/F2)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.faq_builder import (
    FAQEntry,
    IMPERATIVE_VERB_DENYLIST,
    build_faq_entries,
    render_faq_body_section,
)


@pytest.mark.unit
class TestImperativeRejection:
    def test_imperative_bullet_block_is_rejected(self):
        content = (
            "## What common mistakes should I avoid?\n\n"
            "Mine Community Forums: search Quora manually with terms related to your industry. "
            "Filter Reddit threads from 2024-2026 to find recent real-world experiences. "
            "Quote actual user language in your ad copy.\n"
        )
        entries = build_faq_entries(
            ["What common mistakes should I avoid?"],
            content,
            min_confidence=3.0,
        )
        assert entries == []

    def test_denylist_contains_known_leak_verbs(self):
        for verb in ("mine", "search", "log", "navigate", "export", "download"):
            assert verb in IMPERATIVE_VERB_DENYLIST


@pytest.mark.unit
class TestParagraphEligibility:
    def test_short_paragraph_rejected(self):
        content = "## Q heading\n\nToo short.\n"
        entries = build_faq_entries(["What is X?"], content)
        assert entries == []

    def test_single_sentence_paragraph_rejected(self):
        content = (
            "## Q heading\n\n"
            "This is one long sentence about X that goes on and on without "
            "ever reaching a second sentence ending\n"
        )
        entries = build_faq_entries(["What is X?"], content)
        assert entries == []

    def test_well_formed_paragraph_accepted(self):
        content = (
            "## Section\n\n"
            "Measurement is a framework that lets teams attribute revenue "
            "to specific marketing channels with reasonable precision. Most "
            "implementations rely on a combination of tracking pixels and "
            "CRM signals to close the loop on conversions.\n"
        )
        entries = build_faq_entries(
            ["What is measurement and how does it work?"],
            content,
            min_confidence=3.0,
        )
        assert len(entries) == 1
        assert "Measurement is a framework" in entries[0].long_answer


@pytest.mark.unit
class TestNoFallbackBoilerplate:
    def test_low_confidence_drops_question_instead_of_filling_boilerplate(self):
        content = (
            "## About apples\n\n"
            "Apples are a popular fruit grown in temperate climates. They "
            "come in many varieties including Gala, Honeycrisp, and Fuji. "
            "Most apples are harvested in the fall.\n"
        )
        entries = build_faq_entries(
            ["How do rockets achieve orbital velocity?"],
            content,
            min_confidence=3.0,
        )
        assert entries == []


@pytest.mark.unit
class TestShortAnswerTruncation:
    def test_short_answer_respects_word_budget(self):
        long_para = (
            "Conversion tracking is a measurement practice that lets you "
            "attribute downstream revenue to specific advertising touchpoints "
            "by placing tracking pixels on success pages and tying each event "
            "back to the campaign and ad that drove the click. Most platforms "
            "support a base pixel plus event-specific snippets, and the data "
            "flows into a central reporting dashboard for analysis."
        )
        content = f"## Section\n\n{long_para}\n"
        entries = build_faq_entries(
            ["How does conversion tracking work?"],
            content,
            short_answer_max_words=30,
        )
        assert entries
        assert len(entries[0].short_answer.split()) <= 30
        assert entries[0].short_answer.rstrip().endswith((".", "!", "?"))


@pytest.mark.unit
class TestDeduplication:
    def test_same_question_not_used_twice(self):
        content = (
            "## A\n\nFirst topic paragraph about widgets and their many "
            "useful applications. Widgets help teams ship faster and with "
            "fewer defects.\n\n"
            "## B\n\nSecond topic paragraph about widgets and gizmos. "
            "Gizmos differ from widgets in that they require batteries.\n"
        )
        entries = build_faq_entries(
            ["What are widgets?", "What are widgets?"],
            content,
            min_confidence=3.0,
        )
        assert len(entries) == 1
        assert entries[0].question == "What are widgets?"
        assert entries[0].confidence >= 3.0

    def test_same_paragraph_not_reused_for_two_questions(self):
        content = (
            "## A\n\nFirst paragraph mentioning alpha and beta and gamma. "
            "It is long enough to qualify and has multiple sentences for "
            "the eligibility check.\n"
        )
        entries = build_faq_entries(
            ["What is alpha?", "What is beta?"],
            content,
            min_confidence=3.0,
        )
        assert len(entries) == 1
        assert entries[0].question in {"What is alpha?", "What is beta?"}
        assert entries[0].confidence >= 3.0


@pytest.mark.unit
class TestRenderFAQ:
    def test_render_empty_returns_empty_string(self):
        assert render_faq_body_section([], "FAQ") == ""

    def test_render_produces_h2_and_h3(self):
        entry = FAQEntry(
            question="What is X?",
            short_answer="X is a thing.",
            long_answer="X is a thing that does Y. It also does Z.",
            confidence=5.0,
        )
        output = render_faq_body_section([entry], "Frequently Asked Questions")
        assert "## Frequently Asked Questions" in output
        assert "### What is X?" in output
        assert "X is a thing that does Y" in output


@pytest.mark.unit
def test_keyword_words_do_not_decide_the_pairing():
    """Audit 2026-09-28: 'spend'/'financing' appear in every paragraph, so one shared
    keyword word used to pair a question with the first paragraph in the document."""
    content = (
        "These are alternative financing options for ad spend, not fixes for weak sales. "
        "A line of credit that costs $900 adds to acquisition cost.\n\n"
        "It is not a business loan by default. A pay-in-full charge card "
        "sends one monthly bill instead of carrying a balance."
    )
    entries = build_faq_entries(
        ["Is ad spend financing the same as a business loan?"], content,
        keyword="ad spend financing",
    )
    assert entries and "not a business loan" in entries[0].short_answer


@pytest.mark.unit
def test_extract_body_faq_finds_renamed_faq_section():
    from core.faq_builder import extract_body_faq
    content = (
        "## Common questions founders ask\n\n### Is it a loan?\n\nNo. A charge card.\n\n"
        "### Is it US only?\n\nYes. US companies only.\n\n## Next step\n\nGo.\n"
    )
    assert [(e.question, e.short_answer) for e in extract_body_faq(content)] == [
        ("Is it a loan?", "No. A charge card."), ("Is it US only?", "Yes. US companies only."),
    ]
    assert extract_body_faq("## Body\n\n### Only one?\n\nText.\n") == []


@pytest.mark.unit
def test_sync_faq_frontmatter_replaces_block_with_blank_lines():
    import yaml
    from core.formatter import AstroFormatter

    mdx = (
        "---\n"
        'title: "Test"\n'
        "faq:\n"
        "  - question: Stale Q1?\n"
        "    shortAnswer: Stale A1.\n"
        "\n"
        "  - question: Stale Q2?\n"
        "    shortAnswer: Stale A2.\n"
        'category: "Advertising"\n'
        "---\n\n"
        "## Frequently Asked Questions\n\n"
        "### Fresh Q1?\n\nFresh A1.\n\n"
        "### Fresh Q2?\n\nFresh A2.\n"
    )
    synced = AstroFormatter.sync_faq_frontmatter(mdx)
    assert "Stale Q1" not in synced and "Stale Q2" not in synced
    fm = yaml.safe_load(synced.split("---\n", 2)[1])
    assert [item["question"] for item in fm["faq"]] == ["Fresh Q1?", "Fresh Q2?"]
    assert fm["category"] == "Advertising"
