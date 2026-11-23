"""Unit tests for the display-agnostic PipelineReporter protocol and its
adapters (CallbackReporter, RichReporter, BridgeReporter) plus the bridge
run_plan manifest."""
import io
from functools import partial
import json

import pytest

from core.progress_reporter import (
    CallbackReporter,
    NullReporter,
    build_reporter,
    slugify_task,
    task_id,
)
from core.tui_bridge import (
    BridgeReporter,
    BridgeRunCancelled,
    JsonlEmitter,
    _fuzzy_phase,
    build_run_plan,
)


pytestmark = pytest.mark.unit


# ── slugs ────────────────────────────────────────────────────────────────


def test_slugify_task_handles_labels():
    assert slugify_task("SERP Analysis") == "serp-analysis"
    assert slugify_task("Part 1/3") == "part-1-3"
    assert slugify_task("TLDR AEO") == "tldr-aeo"
    assert slugify_task("///") == "task"


def test_task_id_is_phase_scoped():
    assert task_id("research", "Reddit Mining") == "research:reddit-mining"


# ── CallbackReporter ─────────────────────────────────────────────────────


class RecordingCallback:
    def __init__(self):
        self.calls = []

    def __call__(self, message, advance=0):
        self.calls.append((message, advance))


def test_callback_reporter_forwards_terminal_transitions_with_advance():
    cb = RecordingCallback()
    reporter = CallbackReporter(cb)

    reporter.start_phase("research", "Research", ["SERP Analysis"])
    reporter.start_task("research", "SERP Analysis", "Perplexity")
    reporter.complete_task("research", "SERP Analysis", "12 questions", 3.2)
    reporter.fail_task("research", "Reddit Mining", "timeout")
    reporter.skip_task("research", "Quora Extraction", "Disabled")
    reporter.message("✓ Research complete", 0)

    assert cb.calls[0] == ("Research", 0)
    assert cb.calls[1] == ("SERP Analysis — Perplexity", 0)
    assert cb.calls[2] == ("✓ SERP Analysis: 12 questions", 1)
    assert cb.calls[3] == ("✗ Reddit Mining: timeout", 1)
    assert cb.calls[4] == ("⊘ Quora Extraction: Disabled", 1)
    assert cb.calls[5] == ("✓ Research complete", 0)


def test_callback_reporter_display_only_updates_are_silent():
    cb = RecordingCallback()
    reporter = CallbackReporter(cb)

    reporter.update_task("research", "Research Index Search", "complete", "5 sources", 2.0)
    reporter.set_task_progress("research", "Reddit Mining", 3, 10, query="best crm")
    reporter.end_phase("research")

    assert cb.calls == []


def test_callback_reporter_supports_one_arg_partial_and_variadic_callables():
    one_arg = []
    CallbackReporter(lambda message: one_arg.append(message)).message("one", 2)
    assert one_arg == ["one"]

    calls = []

    def target(prefix, message, advance=0):
        calls.append((prefix, message, advance))

    CallbackReporter(partial(target, "prefix")).message("two", 3)
    assert calls == [("prefix", "two", 3)]

    variadic = []
    CallbackReporter(lambda *args: variadic.append(args)).message("three", 4)
    assert variadic == [("three", 4)]


def test_callback_reporter_propagates_typeerror_raised_inside_callback():
    def broken(_message, _advance=0):
        raise TypeError("callback bug")

    with pytest.raises(TypeError, match="callback bug"):
        CallbackReporter(broken).message("boom", 0)


# ── build_reporter selection ─────────────────────────────────────────────


def test_build_reporter_none_returns_null():
    assert isinstance(build_reporter(None), NullReporter)


def test_build_reporter_plain_callable_returns_callback_adapter():
    reporter = build_reporter(lambda msg, adv=0: None)
    assert isinstance(reporter, CallbackReporter)


def test_build_reporter_non_tty_rich_callback_falls_back_to_callback_adapter():
    class FakeProgressCallback:
        def __call__(self, message, advance=0):
            pass

        def pipeline_renderable(self):
            return None  # non-TTY: no Rich bar available

    reporter = build_reporter(FakeProgressCallback())
    assert isinstance(reporter, CallbackReporter)


def test_build_reporter_rich_capable_callback_returns_rich_reporter():
    class FakeProgressCallback:
        def __call__(self, message, advance=0):
            pass

        def pipeline_renderable(self):
            return object()

        def advance_pipeline_fractional(self, steps):
            pass

    from core.cli.rich_reporter import RichReporter

    reporter = build_reporter(FakeProgressCallback())
    assert isinstance(reporter, RichReporter)
    assert reporter.renders_rich_console is True
    assert reporter.renders_live is True


# ── RichReporter drives LiveStatusDisplay ────────────────────────────────


class _StubLive:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _rich_reporter_with_stub_live(monkeypatch):
    from core.cli.live_display import LiveStatusDisplay
    from core.cli.rich_reporter import RichReporter

    stub = _StubLive()
    monkeypatch.setattr(
        LiveStatusDisplay,
        "start_live_grouped",
        lambda self, bar, refresh_per_second=2: stub,
    )
    monkeypatch.setattr(LiveStatusDisplay, "refresh", lambda self: None)

    class FakeProgressCallback:
        def __init__(self):
            self.calls = []

        def __call__(self, message, advance=0):
            self.calls.append((message, advance))

        def pipeline_renderable(self):
            return object()

        def advance_pipeline_fractional(self, steps):
            pass

    cb = FakeProgressCallback()
    return RichReporter(cb), cb, stub


def test_rich_reporter_state_transitions_match_live_display(monkeypatch):
    reporter, cb, stub = _rich_reporter_with_stub_live(monkeypatch)

    reporter.start_phase("research", "Research", ["SERP Analysis", "Reddit Mining", "Quality Filter"])
    assert stub.started
    assert cb.calls[0] == ("Research", 0)

    reporter.start_task("research", "SERP Analysis", "Perplexity")
    reporter.complete_task("research", "SERP Analysis", "12 questions", 3.2)
    reporter.skip_task("research", "Reddit Mining", "Disabled")
    reporter.fail_task("research", "Quality Filter", "boom")

    states = {name: (state, detail, time) for name, state, detail, time in reporter._display.tasks}
    assert states["SERP Analysis"][0] == "done"
    assert states["SERP Analysis"][1] == "12 questions"
    assert states["SERP Analysis"][2] == "3.2s"
    assert states["Reddit Mining"][0] == "skipped"
    assert states["Quality Filter"][0] == "failed"

    reporter.end_phase("research")
    assert stub.stopped


def test_rich_reporter_update_task_is_display_only(monkeypatch):
    reporter, cb, _ = _rich_reporter_with_stub_live(monkeypatch)
    reporter.start_phase("research", "Research", ["Research Index Search"])
    advances_before = sum(adv for _, adv in cb.calls)

    reporter.update_task("research", "Research Index Search", "complete", "5 sources", 2.0)

    states = {name: state for name, state, _, _ in reporter._display.tasks}
    assert states["Research Index Search"] == "done"
    # update_task must not advance the pipeline bar.
    assert sum(adv for _, adv in cb.calls) == advances_before


# ── BridgeReporter JSONL emission ────────────────────────────────────────


def _bridge_reporter():
    stream = io.StringIO()
    emitter = JsonlEmitter("run_test", stream)
    return BridgeReporter(emitter), stream


def _events(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_bridge_reporter_start_phase_emits_manifest():
    reporter, stream = _bridge_reporter()
    reporter.start_phase("research", "Research", ["SERP Analysis", "Reddit Mining"])

    event = _events(stream)[0]
    assert event["type"] == "phase_started"
    assert event["phase"] == "research"
    assert event["message"] == "Research"
    assert event["data"]["tasks"] == [
        {"id": "research:serp-analysis", "label": "SERP Analysis"},
        {"id": "research:reddit-mining", "label": "Reddit Mining"},
    ]


def test_bridge_reporter_task_transitions_carry_status_and_task_id():
    reporter, stream = _bridge_reporter()
    reporter.start_task("research", "SERP Analysis", "Perplexity sonar")
    reporter.complete_task("research", "SERP Analysis", "12 questions", 3.21)
    reporter.fail_task("refinement", "TLDR AEO", "Validation failed")
    reporter.skip_task("gsc", "Keyword Map", "Disabled")
    reporter.update_task("research", "Research Index Search", "complete", "5 sources", 2.0)

    events = _events(stream)
    assert [e["type"] for e in events] == ["task_progress"] * 5
    assert events[0]["taskId"] == "research:serp-analysis"
    assert events[0]["data"]["status"] == "running"
    assert events[0]["message"] == "Perplexity sonar"
    assert events[1]["data"]["status"] == "complete"
    assert events[1]["data"]["elapsed"] == 3.21
    assert events[2]["taskId"] == "refinement:tldr-aeo"
    assert events[2]["data"]["status"] == "failed"
    assert events[3]["data"]["status"] == "skipped"
    assert events[4]["data"]["status"] == "complete"
    assert all(e["data"]["label"] for e in events)


def test_bridge_reporter_set_task_progress_carries_bar_and_query_preview():
    reporter, stream = _bridge_reporter()
    long_query = "q" * 200
    reporter.set_task_progress(
        "research", "Reddit Mining", 5, 10, insights=7, query=long_query,
    )

    event = _events(stream)[0]
    assert event["taskId"] == "research:reddit-mining"
    assert event["data"]["progressCurrent"] == 5
    assert event["data"]["progressTotal"] == 10
    assert event["data"]["insights"] == 7
    assert len(event["data"]["query"]) == 80
    assert event["data"]["query"].endswith("…")
    assert event["data"]["status"] == "running"


def test_bridge_reporter_legacy_message_uses_fuzzy_phase():
    reporter, stream = _bridge_reporter()
    reporter.message("Polishing with Qwen3 235B Instruct...", 0)

    event = _events(stream)[0]
    assert event["phase"] == "refinement"
    assert "taskId" not in event
    assert event["data"]["advance"] == 0


def test_bridge_reporter_raises_on_cancel():
    from core.tui_bridge import BridgeController

    stream = io.StringIO()
    emitter = JsonlEmitter("run_test", stream)
    controller = BridgeController("run_test", emitter)
    controller.cancelled = True
    reporter = BridgeReporter(emitter, controller)

    with pytest.raises(BridgeRunCancelled):
        reporter.start_task("research", "SERP Analysis")
    with pytest.raises(BridgeRunCancelled):
        reporter.message("still going", 0)


# ── fuzzy phase mapping (manifest keys only) ─────────────────────────────


def test_fuzzy_phase_maps_to_manifest_keys():
    assert _fuzzy_phase("GSC preflight skipped") == "gsc"
    assert _fuzzy_phase("Generating guide content with Gemini 3.1 Pro...") == "draft"
    assert _fuzzy_phase("Formatting Astro MDX...") == "formatting"
    assert _fuzzy_phase("Polishing with Qwen3 235B Instruct...") == "refinement"
    assert _fuzzy_phase("Citation batch 2/4") == "refinement"
    assert _fuzzy_phase("Refreshing schema JSON-LD...") == "validation"
    assert _fuzzy_phase("SERP landscape ready") == "research"
    assert _fuzzy_phase("Queued comment for review") == "review"
    assert _fuzzy_phase("something unrecognisable") == "draft"


# ── run_plan manifest ────────────────────────────────────────────────────


def _phase_map(plan):
    return {p["phase"]: p for p in plan}


def test_run_plan_default_guide_shape():
    plan = build_run_plan(style="guide")
    phases = [p["phase"] for p in plan]
    assert phases == ["context", "gsc", "research", "draft", "formatting", "refinement", "validation"]

    by_phase = _phase_map(plan)
    research_labels = [t["label"] for t in by_phase["research"]["tasks"]]
    assert research_labels == ["SERP Analysis", "Reddit Mining", "Quora Extraction", "Quality Filter"]
    refinement_labels = [t["label"] for t in by_phase["refinement"]["tasks"]]
    assert "Review Gate" in refinement_labels
    assert refinement_labels.index("Citation Fixes") < refinement_labels.index("Review Gate") < refinement_labels.index("Polish Pass")
    assert by_phase["draft"]["tasks"][0]["id"] == "draft:part-1-3"


def test_run_plan_gsc_phase_includes_score_row():
    plan = build_run_plan(style="standard")
    gsc = _phase_map(plan)["gsc"]
    assert [t["id"] for t in gsc["tasks"]] == [
        "gsc:availability",
        "gsc:cannibalization",
        "gsc:gsc-score",
        "gsc:keyword-map",
    ]


def test_run_plan_research_style_adds_index_search():
    plan = build_run_plan(style="research")
    research = _phase_map(plan)["research"]
    assert [t["label"] for t in research["tasks"]][:2] == ["SERP Analysis", "Research Index Search"]


def test_run_plan_respects_config_toggles():
    plan = build_run_plan(
        style="standard",
        gsc_check=False,
        gsc_keywords=False,
        community_mode="skip",
        review_gate=False,
    )
    by_phase = _phase_map(plan)
    assert "gsc" not in by_phase
    assert [t["label"] for t in by_phase["research"]["tasks"]] == ["SERP Analysis"]
    assert "Review Gate" not in [t["label"] for t in by_phase["refinement"]["tasks"]]
    assert "output" not in by_phase


def test_run_plan_output_phase_when_saving_or_publishing():
    plan = build_run_plan(style="guide", save_local=True, publish=True)
    output = _phase_map(plan)["output"]
    assert [t["label"] for t in output["tasks"]] == ["Save Local", "Publish"]
