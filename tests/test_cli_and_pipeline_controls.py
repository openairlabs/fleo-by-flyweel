"""Consolidated unit tests for CLI progress, note threading, run context, competitor suppression,
research lifecycle, IndexNow, citation verification, and depth profiles."""
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.cli import note_input
from core.cli.live_display import LiveStatusDisplay
from core.cli.note_input import ask_with_note
from core.cli.output import OutputManager, OutputMode
from core.cli.progress import ProgressManager
from core.generator import ContentGenerator
from core.indexnow import IndexNowClient
from core.progress_reporter import CallbackReporter
from core.research import CommunityResearcher, DEPTH_PROFILES, WebResearcher, resolve_research_plan
from core.user_note import NoteBundle, STEP_KEYWORD, STEP_STYLE, adapt_note

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# CLI Progress & Live Display
# ---------------------------------------------------------------------------

def test_progress_update_emits_plain_progress(capsys):
    output = OutputManager(mode=OutputMode.PLAIN)
    progress = ProgressManager(output=output)

    progress.update("Drafting part 1/3", advance=0)
    progress.update("Drafting part 1/3", advance=0)
    progress.update("Drafting part 2/3", advance=1)

    out = capsys.readouterr().out
    assert "[PROGRESS] Drafting part 1/3" in out
    assert "[PROGRESS] Drafting part 2/3" in out
    assert out.count("Drafting part 1/3") == 1


def test_live_display_fail_state_advances_once():
    display = LiveStatusDisplay()
    advances = []
    display.bind_progress(lambda message, advance=0: advances.append(advance))
    display.add_task("Citation Verify")

    display.start_task("Citation Verify", "Checking")
    display.fail_task("Citation Verify", "Verification failed")
    display.complete_task("Citation Verify", "Recovered", "✓")

    assert display.tasks[0][1] == "done"
    assert advances == [1]


def test_live_display_skip_state_advances_once():
    display = LiveStatusDisplay()
    advances = []
    display.bind_progress(lambda message, advance=0: advances.append(advance))
    display.add_task("Citation Fixes")

    display.skip_task("Citation Fixes", "No fixes needed")
    display.fail_task("Citation Fixes", "Should not double count")

    assert display.tasks[0][1] == "failed"
    assert advances == [1]


# ---------------------------------------------------------------------------
# Tab-Note Fallback
# ---------------------------------------------------------------------------

class _PlainQuestion:
    def __init__(self, answer="standard"):
        self._answer = answer
        self.ask_calls = 0

    @property
    def application(self):
        raise AttributeError("Question.application removed in this version")

    def ask(self):
        self.ask_calls += 1
        return self._answer


def test_no_bundle_returns_plain_ask():
    q = _PlainQuestion("guide")
    assert ask_with_note(q, STEP_STYLE, bundle=None) == "guide"
    assert q.ask_calls == 1


def test_non_tty_returns_plain_ask(monkeypatch):
    monkeypatch.setattr(note_input, "_tty", lambda: False)
    q = _PlainQuestion("research")
    b = NoteBundle()
    assert ask_with_note(q, STEP_STYLE, bundle=b) == "research"
    assert q.ask_calls == 1
    assert b.is_empty()


def test_api_drift_falls_back_without_crash(monkeypatch):
    monkeypatch.setattr(note_input, "_tty", lambda: True)
    q = _PlainQuestion("comparison")
    b = NoteBundle()
    assert ask_with_note(q, STEP_STYLE, bundle=b) == "comparison"
    assert q.ask_calls == 1
    assert b.is_empty()


# ---------------------------------------------------------------------------
# User Note Threading
# ---------------------------------------------------------------------------

class TestNoteBundle:
    def test_from_global_blank_is_empty(self):
        assert NoteBundle.from_global("   ").is_empty()
        assert NoteBundle.from_global(None).is_empty()

    def test_from_global_keeps_text(self):
        b = NoteBundle.from_global("  Target enterprise buyers  ")
        assert b.global_note == "Target enterprise buyers"
        assert not b.is_empty()

    def test_add_step_note_ignores_blank(self):
        b = NoteBundle()
        b.add_step_note(STEP_KEYWORD, "   ")
        b.add_step_note(STEP_KEYWORD, None)
        b.add_step_note("", "real text")
        assert b.step_notes == {}
        assert b.is_empty()

    def test_add_step_note_records_and_strips(self):
        b = NoteBundle()
        b.add_step_note(STEP_STYLE, "  make it skimmable  ")
        assert b.step_notes == {"style": "make it skimmable"}
        assert not b.is_empty()

    def test_raw_text_global_verbatim(self):
        assert NoteBundle.from_global("hi there").raw_text() == "hi there"

    def test_raw_text_step_notes_tagged(self):
        b = NoteBundle()
        b.add_step_note(STEP_KEYWORD, "lead gen software")
        b.add_step_note(STEP_STYLE, "punchy")
        assert b.raw_text() == "[keyword] lead gen software\n[style] punchy"

    def test_raw_text_empty(self):
        assert NoteBundle().raw_text() == ""


class _BoomAI:
    async def _nebius_with_fallback(self, *a, **k):
        raise RuntimeError("boom")


class _GoodAI:
    def __init__(self):
        self.calls = 0

    async def _nebius_with_fallback(self, *a, **k):
        self.calls += 1
        return "  Write for spreadsheet-weary agency owners; keep it punchy.  "


class TestAdaptNote:
    async def test_empty_returns_blank_no_llm(self):
        assert await adapt_note(None, None, keyword="k", style="standard") == ""
        assert await adapt_note(None, NoteBundle(), keyword="k", style="standard") == ""

    async def test_trivial_note_verbatim_no_llm(self):
        b = NoteBundle.from_global("punchy tone, no pricing claims")
        out = await adapt_note(None, b, keyword="k", style="standard")
        assert out == "punchy tone, no pricing claims"
        assert b._adapted == out

    async def test_llm_failure_falls_back_to_raw(self):
        long = (
            "please make sure the article speaks directly to agency owners "
            "who are drowning in spreadsheets and hate manual data entry work"
        )
        b = NoteBundle.from_global(long)
        out = await adapt_note(_BoomAI(), b, keyword="crm", style="standard")
        assert out == long
        assert b._adapted == long

    async def test_llm_success_stripped_and_cached(self):
        long = (
            "please make sure the article speaks directly to agency owners "
            "who are drowning in spreadsheets and hate manual data entry work"
        )
        ai = _GoodAI()
        b = NoteBundle.from_global(long)
        o1 = await adapt_note(ai, b, keyword="crm", style="guide")
        o2 = await adapt_note(ai, b, keyword="crm", style="guide")
        assert o1 == "Write for spreadsheet-weary agency owners; keep it punchy."
        assert o1 == o2
        assert ai.calls == 1


class TestPromptInjection:
    def test_format_user_note_empty_when_unset(self):
        gen = ContentGenerator.__new__(ContentGenerator)
        assert gen._format_user_note() == ""

    def test_format_user_note_present_when_set(self):
        gen = ContentGenerator.__new__(ContentGenerator)
        gen._adapted_note = "Keep it punchy for founders."
        block = gen._format_user_note()
        assert "USER DIRECTIVE" in block
        assert "Keep it punchy for founders." in block


# ---------------------------------------------------------------------------
# Generator Run Context & Competitor Suppression
# ---------------------------------------------------------------------------

def test_generation_run_context_is_fresh_for_every_run():
    generator = ContentGenerator(site_context={})
    reporter = CallbackReporter(lambda *_args: None)

    first = generator._new_run_context(
        keyword="first",
        style="standard",
        reporter=reporter,
        skip_icp=False,
        competitor_mentions=True,
        keyword_context="first context",
        note_bundle=None,
    )
    first.adapted_note = "changed"
    second = generator._new_run_context(
        keyword="second",
        style="guide",
        reporter=reporter,
        skip_icp=True,
        competitor_mentions=False,
        keyword_context=None,
        note_bundle=None,
    )

    assert first is not second
    assert second.adapted_note == ""
    assert second.keyword_context == ""
    assert second.skip_icp is True
    assert second.suppress_competitors is True


def test_prepare_raw_draft_recomputes_words_after_structure_repair():
    generator = ContentGenerator(site_context={})
    body = " ".join(["word"] * 1000)
    content, words, detail = generator._prepare_raw_draft(
        f"---\ntitle: Test\ndescription: Test\npublishDate: 2026-07-11\nauthor: Editorial\n---\n\n# Test\n\n{body}"
    )

    assert content.startswith("---")
    assert words >= 1000
    assert detail == "Passed"


def _suppressed_generator(suppress: bool) -> ContentGenerator:
    generator = ContentGenerator(site_context={})
    generator._suppress_competitors = suppress
    return generator


def test_has_competitors_short_circuits_when_suppressed():
    assert _suppressed_generator(True)._has_competitors("supermetrics vs funnel.io", {}) is False


def test_has_competitors_unaffected_when_not_suppressed():
    from core.brand_profile import BrandProfile

    gen = _suppressed_generator(False)
    assert gen._has_competitors("neon vs supabase", {}) is True
    assert gen._has_competitors("manual vs automated workflows", {}) is False
    gen_bp = ContentGenerator(site_context={}, brand_profile=BrandProfile(company_name="Acme", direct_competitors=["PlanetScale"]))
    assert gen_bp._has_competitors("planetscale alternatives", {}) is True


def test_approved_solutions_block_empty_when_suppressed():
    assert _suppressed_generator(True)._format_approved_solutions(["Supermetrics", "Funnel.io"]) == ""


def test_competitor_policy_block_injected_only_when_suppressed():
    assert _suppressed_generator(False)._format_competitor_policy() == ""
    policy = _suppressed_generator(True)._format_competitor_policy()
    assert "COMPETITOR POLICY" in policy
    assert "Do NOT name" in policy


def test_generate_ignores_flag_for_comparison_styles():
    generator = ContentGenerator(site_context={})
    reporter = CallbackReporter(lambda *_args: None)
    for style in ("comparison", "top-compare"):
        context = generator._new_run_context(
            keyword="test",
            style=style,
            reporter=reporter,
            skip_icp=False,
            competitor_mentions=False,
            keyword_context=None,
            note_bundle=None,
        )
        assert context.suppress_competitors is False


def test_runner_parses_no_competitors_flag():
    import tui_bridge

    parser = tui_bridge.build_parser()
    args = parser.parse_args(["generate", "-k", "x", "--no-competitors"])
    assert args.no_competitors is True
    args = parser.parse_args(["generate", "-k", "x"])
    assert args.no_competitors is False


async def test_cli_run_generation_forwards_no_competitors_as_competitor_mentions(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from core.cli import app as cli_app
    import core.generator as gen_mod
    import core.repo_extractor as repo_mod

    captured_kwargs = []

    class _FakeExtractor:
        def extract_context(self):
            return {"internal_links": {}}

    class _FakeGenerator:
        async def generate(self, keyword, style, **kwargs):
            captured_kwargs.append(kwargs)
            return {"content": "ok", "slug": "x"}

    monkeypatch.setattr(repo_mod, "RepoContextExtractor", _FakeExtractor)
    monkeypatch.setattr(gen_mod, "ContentGenerator", _FakeGenerator)

    fake_progress = SimpleNamespace(phase=lambda *_a, **_kw: nullcontext())
    fake_output = SimpleNamespace(info=lambda *_a, **_kw: None)

    await cli_app._run_generation(
        keyword="postgres tuning",
        style="standard",
        skip_community=True,
        limit_community=False,
        flyweel_mode="full",
        progress=fake_progress,
        output=fake_output,
        no_competitors=True,
    )
    await cli_app._run_generation(
        keyword="postgres tuning",
        style="standard",
        skip_community=True,
        limit_community=False,
        flyweel_mode="full",
        progress=fake_progress,
        output=fake_output,
        no_competitors=False,
    )

    assert captured_kwargs[0].get("competitor_mentions") is False
    assert captured_kwargs[1].get("competitor_mentions") is True


# ---------------------------------------------------------------------------
# Research Resource Lifecycle
# ---------------------------------------------------------------------------

class _MockSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _MockRouter:
    def __init__(self) -> None:
        self.exit_calls = []

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.exit_calls.append((exc_type, exc_value, traceback))


class _MockConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


async def test_community_exit_closes_only_owned_resources() -> None:
    researcher = CommunityResearcher.__new__(CommunityResearcher)
    researcher.session = _MockSession()
    researcher.ai = _MockRouter()

    await researcher.__aexit__(None, None, None)

    assert researcher.session is None
    assert researcher.ai.exit_calls == [(None, None, None)]


async def test_web_exit_closes_owned_gsc_transport() -> None:
    connection = _MockConnection()
    transport = SimpleNamespace(connections={"gsc": connection})
    researcher = WebResearcher.__new__(WebResearcher)
    researcher.session = _MockSession()
    researcher.ai = _MockRouter()
    researcher._gsc_service = SimpleNamespace(_http=transport)

    await researcher.__aexit__(None, None, None)

    assert researcher.session is None
    assert connection.closed is True
    assert transport.connections == {}
    assert researcher.ai.exit_calls == [(None, None, None)]


async def test_community_exit_does_not_replace_body_exception() -> None:
    researcher = CommunityResearcher.__new__(CommunityResearcher)
    researcher.session = _MockSession()
    researcher.ai = _MockRouter()
    expected = RuntimeError("generation body failed")

    with pytest.raises(RuntimeError, match="generation body failed") as caught:
        try:
            raise expected
        except RuntimeError:
            suppress = await researcher.__aexit__(*sys.exc_info())
            if not suppress:
                raise

    assert caught.value is expected


# ---------------------------------------------------------------------------
# IndexNow Submission Client
# ---------------------------------------------------------------------------

class _FakeIndexNowResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return "accepted"


class _FakeIndexNowSession:
    def __init__(self):
        self.calls = []

    def post(self, endpoint, data=None, headers=None):
        self.calls.append({"endpoint": endpoint, "data": data, "headers": headers})
        return _FakeIndexNowResponse()


@pytest.mark.asyncio
async def test_submit_urls_posts_indexnow_payload():
    session = _FakeIndexNowSession()
    client = IndexNowClient(
        key="abc123",
        endpoint="https://api.indexnow.org/indexnow",
        key_location="https://www.acme.dev/abc123.txt",
        session=session,
    )

    result = await client.submit_urls(["https://www.acme.dev/blog/test"])

    assert result["submitted"] is True
    assert result["status"] == 200
    assert session.calls[0]["endpoint"] == "https://api.indexnow.org/indexnow"
    assert '"host": "www.acme.dev"' in session.calls[0]["data"]
    assert '"keyLocation": "https://www.acme.dev/abc123.txt"' in session.calls[0]["data"]


def test_non_https_endpoint_is_rejected():
    client = IndexNowClient(key="key", endpoint="file:///tmp/payload")
    assert client.is_available is False


@pytest.mark.asyncio
async def test_submit_urls_requires_key():
    client = IndexNowClient(key="", endpoint="https://api.indexnow.org/indexnow")
    result = await client.submit_urls(["https://www.acme.dev/blog/test"])
    assert result["submitted"] is False
    assert "INDEXNOW_KEY" in result["error"]


# ---------------------------------------------------------------------------
# Citation Extraction
# ---------------------------------------------------------------------------

class TestCitationExtraction:
    def _generator(self):
        return ContentGenerator.__new__(ContentGenerator)

    def test_extracts_markdown_external_links(self):
        gen = self._generator()
        out = gen._extract_verifiable_citations(
            "Revenue rose 30% according to [Forrester](https://forrester.com/report)."
        )
        assert out["citations"] == [("Forrester", "https://forrester.com/report")]
        assert out["labels_only"] == []

    def test_extracts_astro_citation_section_refs(self):
        gen = self._generator()
        content = """
Data shows a 30% lift <a href="#cite-1" class="citation-ref">[1]</a>.

<CitationsSection>

<span id="cite-1">[1]</span> <a href="https://www.nber.org/papers/w99" target="_blank" rel="nofollow">NBER Working Paper 99</a> - Research Study

</CitationsSection>
"""
        out = gen._extract_verifiable_citations(content)
        assert ("NBER Working Paper 99", "https://www.nber.org/papers/w99") in out["citations"]
        assert out["labels_only"] == []

    def test_reports_label_only_citations_without_urls(self):
        gen = self._generator()
        out = gen._extract_verifiable_citations("The benchmark is $300-$700 [Reddit Community, 2026].")
        assert out["citations"] == []
        assert out["labels_only"] == ["Reddit Community, 2026"]

    @pytest.mark.asyncio
    async def test_verify_returns_label_only_status(self):
        gen = self._generator()
        result = await gen._verify_citations(
            "The benchmark is $300-$700 [Reddit Community, 2026].",
            ai=None,
            web=None,
        )
        assert result["verification_status"] == "citation_labels_only"
        assert result["label_count"] == 1


# ---------------------------------------------------------------------------
# Research Depth Profiles
# ---------------------------------------------------------------------------

def test_depth_profiles_shape():
    assert set(DEPTH_PROFILES) == {"quick", "standard", "comprehensive"}
    for profile in DEPTH_PROFILES.values():
        assert set(profile) == {
            "reddit_limit", "quora_limit", "topical_insights", "serp_timeout", "wall_clock",
        }


def test_quick_profile_values():
    plan = resolve_research_plan("standard", "quick", skip_community=False, limit_community=False)
    assert plan["reddit_limit"] == 3
    assert plan["quora_limit"] == 1
    assert plan["topical_insights"] is False
    assert plan["serp_timeout"] == 180
    assert plan["wall_clock"] == 600
    assert plan["depth"] == "quick"


@pytest.mark.parametrize("depth", ["standard", "comprehensive"])
def test_standard_and_comprehensive_profiles_match(depth):
    plan = resolve_research_plan("standard", depth, skip_community=False, limit_community=False)
    assert plan["reddit_limit"] is None
    assert plan["quora_limit"] is None
    assert plan["topical_insights"] is True
    assert plan["serp_timeout"] == 300
    assert plan["wall_clock"] == 1800
    assert plan["depth"] == depth


def test_unknown_depth_falls_back_to_standard_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="core.research"):
        plan = resolve_research_plan("standard", "extreme", skip_community=False, limit_community=False)
    assert plan["depth"] == "standard"
    assert plan["reddit_limit"] is None
    assert plan["quora_limit"] is None
    assert plan["topical_insights"] is True
    assert plan["serp_timeout"] == 300
    assert plan["wall_clock"] == 1800
    assert any("Unknown research depth" in record.message for record in caplog.records)


@pytest.mark.parametrize("depth", ["quick", "standard", "comprehensive"])
def test_research_style_overrides_every_depth(depth):
    plan = resolve_research_plan("research", depth, skip_community=False, limit_community=False)
    assert plan["reddit_limit"] == 30
    assert plan["quora_limit"] is None
    assert plan["topical_insights"] is False
    assert plan["serp_timeout"] == 1200
    assert plan["wall_clock"] == 2400
    assert plan["depth"] == depth


def test_research_style_with_limit_community():
    plan = resolve_research_plan("research", "standard", skip_community=False, limit_community=True)
    assert plan["reddit_limit"] == 3
    assert plan["quora_limit"] == 1
    assert plan["topical_insights"] is False
    assert plan["serp_timeout"] == 1200
    assert plan["wall_clock"] == 2400


@pytest.mark.parametrize("limit_community", [False, True])
def test_skip_community_beats_research_style(limit_community):
    plan = resolve_research_plan("research", "standard", skip_community=True, limit_community=limit_community)
    assert plan["reddit_limit"] == 0
    assert plan["quora_limit"] == 0
    assert plan["serp_timeout"] == 1200
    assert plan["wall_clock"] == 2400


@pytest.mark.parametrize("depth", ["quick", "standard", "comprehensive"])
def test_limit_community_overrides_depth_limits(depth):
    plan = resolve_research_plan("guide", depth, skip_community=False, limit_community=True)
    assert plan["reddit_limit"] == 3
    assert plan["quora_limit"] == 1
    assert plan["serp_timeout"] == DEPTH_PROFILES[depth]["serp_timeout"]
    assert plan["wall_clock"] == DEPTH_PROFILES[depth]["wall_clock"]
    assert plan["topical_insights"] == DEPTH_PROFILES[depth]["topical_insights"]


@pytest.mark.parametrize("depth", ["quick", "standard", "comprehensive"])
@pytest.mark.parametrize("limit_community", [False, True])
def test_skip_community_always_wins(depth, limit_community):
    plan = resolve_research_plan("standard", depth, skip_community=True, limit_community=limit_community)
    assert plan["reddit_limit"] == 0
    assert plan["quora_limit"] == 0


def test_resolver_returns_copy_not_profile_reference():
    plan = resolve_research_plan("standard", "quick", skip_community=False, limit_community=False)
    plan["reddit_limit"] = 999
    plan["serp_timeout"] = 999
    assert DEPTH_PROFILES["quick"]["reddit_limit"] == 3
    assert DEPTH_PROFILES["quick"]["serp_timeout"] == 180


def test_interactive_publish_exception_falls_back_to_local_save(tmp_path, monkeypatch):
    import click
    import core.cli.app as app_mod

    saved_calls = []

    class _FakeGen:
        def __init__(self, *a, **kw):
            pass

        async def generate(self, *a, **kw):
            return {
                "success": True,
                "title": "Test Title",
                "slug": "test-slug",
                "content": "---\ntitle: \"Test Title\"\n---\n\nBody\n",
                "metrics": {"word_count": 120},
            }

    async def _boom_publish(**kw):
        raise PermissionError("connected blog dir unreadable")

    async def _ok_local(result, output, output_dir="output", save_research=False):
        saved_calls.append(result["slug"])
        return {"final": str(tmp_path / "final.mdx")}

    async def _noop_ctx(keyword, style, output):
        return None

    monkeypatch.setattr(app_mod, "_publish_to_astro", _boom_publish)
    monkeypatch.setattr(app_mod, "_save_local", _ok_local)
    monkeypatch.setattr(app_mod, "_confirm_keyword_context", _noop_ctx)
    import core.generator as gen_mod
    monkeypatch.setattr(gen_mod, "ContentGenerator", _FakeGen)

    with pytest.raises(SystemExit) as exc_info:
        app_mod.interactive_generate(
            ["-k", "postgres branching", "--publish", "--json", "--output", str(tmp_path)],
            standalone_mode=False,
        )
    assert exc_info.value.code == 1
    assert saved_calls == ["test-slug"]


@pytest.mark.asyncio
async def test_generate_legacy_honors_explicit_min_tools_for_comparison(tmp_path, monkeypatch):
    import argparse
    import generate as gen_script

    captured = {}

    class _FakeGen:
        async def generate(self, *a, **kw):
            captured.update(kw)
            return {
                "success": True,
                "title": "Compare Tools",
                "slug": "compare-tools",
                "content": "---\ntitle: \"Compare Tools\"\n---\n\nBody\n",
                "raw_content": "Body",
                "metrics": {"word_count": 100},
                "generation_time": 0.1,
            }

    import core.artifacts as art_mod
    monkeypatch.setattr(art_mod, "save_generation_artifacts", lambda *a, **kw: {"final": str(tmp_path / "final.mdx")})
    monkeypatch.setattr(gen_script, "ContentGenerator", _FakeGen)
    args = argparse.Namespace(
        keyword="postgres branching tools",
        style="comparison",
        depth="standard",
        min_tools=15,
        solutions=8,
        output=str(tmp_path),
        no_flyweel=False,
        limited_flyweel=False,
        skip_icp=True,
        augment_serp=False,
        no_reddit=True,
        reddit_limited=False,
        use_llama_polish=False,
        gsc_check=False,
        gsc_keywords=False,
        judge=False,
        json_output=True,
        save_research=False,
        note=None,
        no_competitors=False,
        competitors="ToolA,ToolB",
    )
    await gen_script.original_main(args)
    assert captured["solution_count"] == 15
