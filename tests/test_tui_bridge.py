import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import stat

import pytest

from core.tui_bridge import (
    BridgeController,
    BridgeRunCancelled,
    JsonlEmitter,
    build_progress_emitter,
    build_run_plan,
    detect_setup_status,
    event_to_jsonl,
    make_event,
    parse_command_line,
    save_setup_changes,
    save_local_result,
)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tui_bridge  # noqa: E402  (root JSONL bridge runner, not the core.tui_bridge package)


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_ideas_history(tmp_path, monkeypatch):
    """Point the accepted-ideas history at a per-test temp file so a real
    history under output/ can never leak into (or be clobbered by) tests."""
    monkeypatch.setattr(tui_bridge, "IDEAS_HISTORY_PATH", tmp_path / "ideas_history.json")


def test_event_serializes_as_jsonl_envelope():
    event = make_event(
        "task_progress",
        "run_test",
        phase="research",
        agent="Research Agent",
        task_id="serp",
        message="SERP done",
        data={"advance": 1},
    )

    line = event_to_jsonl(event)

    assert line.endswith("\n")
    payload = json.loads(line)
    assert payload["type"] == "task_progress"
    assert payload["runId"] == "run_test"
    assert payload["phase"] == "research"
    assert payload["agent"] == "Research Agent"
    assert payload["taskId"] == "serp"
    assert payload["message"] == "SERP done"
    assert payload["data"] == {"advance": 1}
    assert payload["timestamp"].endswith("Z")


def test_parse_queue_comment_command():
    command = parse_command_line(
        '{"type":"queue_comment","runId":"run_test","comment":" tighten the intro "}',
        expected_run_id="run_test",
    )

    assert command == {
        "type": "queue_comment",
        "runId": "run_test",
        "comment": "tighten the intro",
    }


def test_parse_review_decision_command():
    command = parse_command_line(
        '{"type":"review_decision","runId":"run_test","decision":"apply_comments","comment":"keep claims specific"}',
        expected_run_id="run_test",
    )

    assert command["decision"] == "apply_comments"
    assert command["comment"] == "keep claims specific"


def test_parse_cancel_command():
    command = parse_command_line(
        '{"type":"cancel","runId":"run_test"}',
        expected_run_id="run_test",
    )

    assert command == {"type": "cancel", "runId": "run_test"}


def test_parse_gsc_decision_command():
    command = parse_command_line(
        '{"type":"gsc_decision","runId":"run_test","decision":"proceed"}',
        expected_run_id="run_test",
    )

    assert command == {"type": "gsc_decision", "runId": "run_test", "decision": "proceed"}


def test_parse_gsc_decision_rejects_unknown_decision():
    with pytest.raises(ValueError, match="invalid decision"):
        parse_command_line(
            '{"type":"gsc_decision","runId":"run_test","decision":"maybe"}',
            expected_run_id="run_test",
        )


def test_malformed_command_raises_user_safe_error():
    with pytest.raises(ValueError, match="Malformed command JSON"):
        parse_command_line("{nope", expected_run_id="run_test")


def test_wrong_run_id_raises():
    with pytest.raises(ValueError, match="does not match"):
        parse_command_line(
            '{"type":"cancel","runId":"other"}',
            expected_run_id="run_test",
        )


def test_save_setup_changes_merges_known_env_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=1\nOPENROUTER_API_KEY=old\n", encoding="utf-8")

    result = save_setup_changes(
        {
            "OPENROUTER_API_KEY": "new",
            "GOOGLE_API_KEY": "google",
            "PERPLEXITY_API_KEY": "px",
            "OUTPUT_DIR": "output/test",
        },
        tmp_path,
    )

    text = env_path.read_text(encoding="utf-8")
    assert "EXISTING=1" in text
    assert "OPENROUTER_API_KEY=new" in text
    assert "GOOGLE_API_KEY=google" in text
    assert "PERPLEXITY_API_KEY=px" in text
    assert result["updatedKeys"] == [
        "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "OUTPUT_DIR", "PERPLEXITY_API_KEY"
    ]
    assert (tmp_path / "output/test").exists()


def test_save_setup_changes_removes_override_when_empty_string_passed(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENROUTER_API_KEY=sk-or-test\nFLEO_POLISH_MODEL=openai/gpt-6-sol\nFLEO_DRAFT_MODEL=anthropic/claude-opus-5.5\n",
        encoding="utf-8",
    )

    result = save_setup_changes({"FLEO_POLISH_MODEL": ""}, tmp_path)

    text = env_path.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-test" in text
    assert "FLEO_DRAFT_MODEL=anthropic/claude-opus-5.5" in text
    assert "FLEO_POLISH_MODEL" not in text
    assert result["updatedKeys"] == ["FLEO_POLISH_MODEL"]


def test_one_key_openrouter_quickstart_and_per_agent_override(tmp_path, monkeypatch):
    for key in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").touch()
    (tmp_path / "output").mkdir()

    empty_status = detect_setup_status(tmp_path)
    assert empty_status["canGenerate"] is False
    assert "ai_credentials" in empty_status["requiredMissing"]

    # 1-Key Quickstart: OPENROUTER_API_KEY alone unlocks generation!
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-quickstart\n", encoding="utf-8")
    ready = detect_setup_status(tmp_path)
    assert ready["canGenerate"] is True
    assert ready["requiredMissing"] == []


def test_save_setup_changes_rejects_unknown_keys(tmp_path):
    with pytest.raises(ValueError, match="Unsupported setup key"):
        save_setup_changes({"DANGEROUS": "1"}, tmp_path)


@pytest.mark.parametrize("value", ["good\nINJECTED=1", "good\rINJECTED=1", "good\x00bad"])
def test_save_setup_changes_rejects_control_character_injection(tmp_path, value):
    with pytest.raises(ValueError, match="control characters"):
        save_setup_changes({"OPENROUTER_API_KEY": value}, tmp_path)
    assert not (tmp_path / ".env").exists()


def test_save_setup_changes_writes_private_env_atomically(tmp_path):
    result = save_setup_changes({"OPENROUTER_API_KEY": "secret"}, tmp_path)
    env_path = Path(result["envPath"])

    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".env.*.tmp")) == []


def test_save_setup_changes_preserves_stricter_existing_permissions(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("OPENROUTER_API_KEY=old\n", encoding="utf-8")
    env_path.chmod(0o400)

    save_setup_changes({"OPENROUTER_API_KEY": "new"}, tmp_path)

    assert stat.S_IMODE(env_path.stat().st_mode) == 0o400


@pytest.mark.asyncio
async def test_controller_collects_comments_until_review_decision():
    stream = io.StringIO()
    emitter = JsonlEmitter("run_test", stream=stream)
    controller = BridgeController("run_test", emitter)
    await controller.queue.put({"type": "queue_comment", "runId": "run_test", "comment": "Make it more concrete"})
    await controller.queue.put({"type": "review_decision", "runId": "run_test", "decision": "apply_comments"})

    result = await controller.review_gate_callback({"content": "one two three", "citation_verification": {}})

    assert result["decision"] == "apply_comments"
    assert result["comments"] == ["Make it more concrete"]
    emitted = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["type"] for event in emitted] == [
        "task_progress",
        "review_gate_opened",
        "artifact_ready",
        "review_gate_closed",
    ]


@pytest.mark.asyncio
async def test_gsc_gate_callback_waits_for_decision_and_emits_events():
    stream = io.StringIO()
    emitter = JsonlEmitter("run_test", stream=stream)
    controller = BridgeController("run_test", emitter)
    await controller.queue.put({"type": "gsc_decision", "runId": "run_test", "decision": "abort"})

    result = await controller.gsc_gate_callback(
        {"keyword": "kw", "score": 12, "threshold": 25, "traffic_potential": 1.4, "primary_query": "kw variant", "impressions": 30},
    )

    assert result == {"decision": "abort"}
    emitted = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["type"] for event in emitted] == ["gsc_gate_opened", "gsc_gate_closed"]
    assert emitted[0]["data"]["score"] == 12
    assert emitted[0]["data"]["threshold"] == 25
    assert emitted[0]["data"]["primaryQuery"] == "kw variant"
    assert emitted[1]["data"]["decision"] == "abort"


@pytest.mark.asyncio
async def test_gsc_gate_opened_carries_estimated_and_neighbor_count():
    stream = io.StringIO()
    emitter = JsonlEmitter("run_test", stream=stream)
    controller = BridgeController("run_test", emitter)
    await controller.queue.put({"type": "gsc_decision", "runId": "run_test", "decision": "proceed"})

    result = await controller.gsc_gate_callback(
        {
            "keyword": "brand new topic",
            "score": 6,
            "threshold": 10,
            "traffic_potential": 0.4,
            "primary_query": "brand new topic",
            "impressions": 0,
            "estimated": True,
            "neighbor_count": 7,
        },
    )

    assert result == {"decision": "proceed"}
    emitted = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["type"] for event in emitted] == ["gsc_gate_opened", "gsc_gate_closed"]
    assert emitted[0]["data"]["estimated"] is True
    assert emitted[0]["data"]["neighborCount"] == 7
    assert emitted[0]["message"].startswith("Estimated GSC score 6%")


@pytest.mark.asyncio
async def test_gsc_gate_opened_defaults_to_measured_score_fields():
    stream = io.StringIO()
    emitter = JsonlEmitter("run_test", stream=stream)
    controller = BridgeController("run_test", emitter)
    await controller.queue.put({"type": "gsc_decision", "runId": "run_test", "decision": "abort"})

    await controller.gsc_gate_callback(
        {"keyword": "kw", "score": 12, "threshold": 25, "traffic_potential": 1.4, "primary_query": "kw variant", "impressions": 30},
    )

    emitted = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert emitted[0]["type"] == "gsc_gate_opened"
    assert emitted[0]["data"]["estimated"] is False
    assert emitted[0]["data"]["neighborCount"] is None
    assert not emitted[0]["message"].startswith("Estimated ")


@pytest.mark.asyncio
async def test_gsc_gate_callback_auto_proceeds_when_stdin_closed():
    stream = io.StringIO()
    emitter = JsonlEmitter("run_test", stream=stream)
    controller = BridgeController("run_test", emitter)
    controller.stdin_closed = True

    result = await controller.gsc_gate_callback({"keyword": "kw", "score": 5, "threshold": 25})

    assert result == {"decision": "proceed"}
    emitted = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["type"] for event in emitted] == ["gsc_gate_opened", "gsc_gate_closed"]
    assert emitted[1]["data"]["decision"] == "proceed"


@pytest.mark.asyncio
async def test_gsc_gate_callback_cancel_maps_to_abort():
    stream = io.StringIO()
    emitter = JsonlEmitter("run_test", stream=stream)
    controller = BridgeController("run_test", emitter)
    await controller.queue.put({"type": "cancel", "runId": "run_test"})

    result = await controller.gsc_gate_callback({"keyword": "kw", "score": 5, "threshold": 25})

    assert result == {"decision": "abort"}
    assert controller.cancelled is True


@pytest.mark.asyncio
async def test_controller_warns_on_malformed_background_command():
    stream = io.StringIO()
    emitter = JsonlEmitter("run_test", stream=stream)
    controller = BridgeController("run_test", emitter)
    await controller.queue.put({"type": "_warning", "message": "bad command"})

    await controller.drain_pending_commands()

    emitted = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert emitted[0]["type"] == "warning"
    assert emitted[0]["message"] == "bad command"


@pytest.mark.asyncio
async def test_progress_emitter_drains_cancel_command_and_raises():
    stream = io.StringIO()
    emitter = JsonlEmitter("run_test", stream=stream)
    controller = BridgeController("run_test", emitter)
    progress = build_progress_emitter(emitter, controller)
    await controller.queue.put({"type": "cancel", "runId": "run_test"})

    with pytest.raises(BridgeRunCancelled):
        progress("Generating draft", 0)

    assert controller.cancelled is True
    emitted = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert emitted[0]["type"] == "warning"
    assert emitted[0]["message"] == "Cancel requested"


@pytest.mark.asyncio
async def test_save_local_result_writes_expected_artifacts(tmp_path):
    saved = await save_local_result(
        {
            "keyword": "pipeline attribution",
            "style": "standard",
            "slug": "pipeline-attribution",
            "title": "Pipeline Attribution",
            "content": "# Final",
            "raw_content": "# Raw",
            "quality_scorecard": {"composite": 90},
            "model_used": {"draft": "test-model"},
        },
        output_dir=str(tmp_path),
    )

    assert (tmp_path / "generations").exists()
    assert saved["final"].endswith("final.mdx")
    assert saved["raw"].endswith("raw.mdx")
    assert saved["metadata"].endswith("metadata.json")
    assert Path(saved["final"]).read_text(encoding="utf-8") == "# Final"
    assert Path(saved["raw"]).read_text(encoding="utf-8") == "# Raw"
    assert '"composite": 90' in Path(saved["metadata"]).read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["../escape", "..\\escape", "/absolute", "safe/../../escape", ""])
async def test_save_local_result_rejects_unsafe_slug_at_sink(tmp_path, slug):
    with pytest.raises(ValueError, match="slug"):
        await save_local_result(
            {"slug": slug, "content": "---\ntitle: 'x'\ndescription: 'y'\n---\n# Safe"},
            output_dir=str(tmp_path),
        )


@pytest.mark.asyncio
async def test_save_local_result_rejects_existing_artifact_without_overwrite(tmp_path):
    result = {
        "slug": "collision",
        "content": "---\ntitle: 'x'\ndescription: 'y'\n---\n# Safe",
    }
    await save_local_result(result, output_dir=str(tmp_path))

    with pytest.raises(FileExistsError, match="collision"):
        await save_local_result(result, output_dir=str(tmp_path))


def test_build_run_plan_accepts_depth_without_changing_tasks():
    kwargs = dict(
        style="standard",
        gsc_check=True,
        gsc_keywords=True,
        community_mode="full",
        review_gate=True,
        save_local=False,
        publish=False,
    )

    default_plan = build_run_plan(**kwargs)
    quick_plan = build_run_plan(**kwargs, depth="quick")

    # Depth is accepted but does not change the task list (quick runs the same
    # rows with lighter n/m totals).
    assert quick_plan == default_plan
    assert [entry["phase"] for entry in quick_plan] == [
        "context", "gsc", "research", "draft", "formatting", "refinement", "validation",
    ]
    gsc_tasks = next(entry for entry in quick_plan if entry["phase"] == "gsc")["tasks"]
    assert [task["label"] for task in gsc_tasks] == [
        "Availability", "Cannibalization", "GSC Score", "Keyword Map",
    ]


def test_publish_artifact_missing_blog_path_emits_error(tmp_path):
    payload = {
        "slug": "missing-blog-path",
        "content": "---\ntitle: \"Missing Blog Path\"\ndescription: \"Test\"\n---\n# Body",
    }
    env = {
        **os.environ,
        "ASTRO_BLOG_PATH": str(tmp_path / "missing-blog"),
        "ASTRO_ASSETS_PATH": str(tmp_path / "assets"),
        "FLEO_BLOG_CONTENT_PATH": str(tmp_path / "missing-blog"),
        "FLEO_BLOG_ASSETS_PATH": str(tmp_path / "assets"),
    }

    result = subprocess.run(
        [
            sys.executable,
            "tui_bridge.py",
            "publish-artifact",
            "--run-id",
            "publish_fail",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )

    assert result.returncode == 1
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["type"] for event in events] == ["tool_call", "error"]
    assert "Blog path not found" in events[-1]["message"]


def test_publish_artifact_emits_conflict_and_requires_explicit_overwrite(tmp_path):
    blog = tmp_path / "blog"
    assets = tmp_path / "assets"
    blog.mkdir()
    assets.mkdir()
    (blog / "collision.mdx").write_text("original", encoding="utf-8")
    payload = {
        "slug": "collision",
        "content": "---\ntitle: 'Replacement'\ndescription: 'A secure replacement article.'\n---\n## Replacement",
    }
    env = {
        **os.environ,
        "ASTRO_BLOG_PATH": str(blog),
        "ASTRO_ASSETS_PATH": str(assets),
        "FLEO_BLOG_CONTENT_PATH": str(blog),
        "FLEO_BLOG_ASSETS_PATH": str(assets),
    }
    command = [sys.executable, "tui_bridge.py", "publish-artifact", "--run-id", "collision"]

    conflict = subprocess.run(
        command,
        cwd=ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    overwrite = subprocess.run(
        [*command, "--overwrite"],
        cwd=ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )

    events = [json.loads(line) for line in conflict.stdout.splitlines()]
    assert conflict.returncode == 3
    assert [event["type"] for event in events] == ["tool_call", "artifact_conflict"]
    assert events[-1]["data"]["slug"] == "collision"
    assert overwrite.returncode == 0
    assert "Replacement" in (blog / "collision.mdx").read_text(encoding="utf-8")


def test_generate_honors_queued_cancel_before_long_running_work():
    env = {
        **os.environ,
        "OPENROUTER_API_KEY": "unit-test-openrouter",
        "PERPLEXITY_API_KEY": "unit-test-perplexity",
    }

    result = subprocess.run(
        [
            sys.executable,
            "tui_bridge.py",
            "generate",
            "--run-id",
            "cancel_early",
            "--keyword",
            "cancel acceptance smoke",
            "--style",
            "standard",
            "--depth",
            "quick",
            "--community-mode",
            "skip",
            "--flyweel-mode",
            "none",
            "--no-gsc-check",
            "--no-gsc-keywords",
            "--no-icp",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        input='{"type":"cancel","runId":"cancel_early"}\n',
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )

    assert result.returncode == 130
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[0]["type"] == "setup_status"
    assert any(event["type"] == "warning" and "Cancel requested" in event["message"] for event in events)
    assert not any(event.get("taskId") == "content-generator" for event in events)


@pytest.mark.asyncio
async def test_generate_cancel_precedes_required_setup_failure(monkeypatch, capsys):
    setup_status = {
        "canGenerate": False,
        "requiredMissing": ["venv"],
        "optionalMissing": [],
        "checks": [],
    }
    monkeypatch.setattr(tui_bridge, "detect_setup_status", lambda _root: setup_status)

    def queue_cancel(controller):
        controller.queue.put_nowait({"type": "cancel", "runId": "cancel_missing_setup"})

    monkeypatch.setattr(BridgeController, "start_stdin_reader", queue_cancel)
    args = tui_bridge.build_parser().parse_args(
        [
            "generate",
            "--run-id",
            "cancel_missing_setup",
            "--keyword",
            "cancel before setup failure",
        ]
    )

    result = await tui_bridge.run_generation(args)

    assert result == 130
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "setup_status"
    assert any(event["type"] == "warning" and "Cancel requested" in event["message"] for event in events)
    assert not any(event.get("taskId") == "required-setup" for event in events)


# ---------------------------------------------------------------------------
# suggest-keywords
# ---------------------------------------------------------------------------

def _fake_generate_keyword_ideas(calls, suggestions=None, raise_exc=None):
    async def fake(seed, style="standard", *, steer=None, previous=None, target=60,
                   trends_fresh=False, mode="angle"):
        calls["seed"] = seed
        calls["style"] = style
        calls["steer"] = steer
        calls["previous"] = previous
        calls["target"] = target
        calls["trends_fresh"] = trends_fresh
        calls["mode"] = mode
        if raise_exc is not None:
            raise raise_exc
        return {
            "suggestions": suggestions if suggestions is not None else [],
            "target": target,
            "gsc_available": True,
            "mode": mode,
        }

    return fake


def test_suggest_keywords_emits_artifact_ready(monkeypatch, capsys):
    calls = {}
    fake_suggestions = [
        {"keyword": "attribution software", "score": 62, "grounded": True, "estimated": False,
         "trafficPotential": 12.0, "rationale": "GSC growth"},
        {"keyword": "marketing ops playbook", "score": None, "grounded": False, "estimated": False,
         "trafficPotential": None, "rationale": "novel angle"},
    ]
    monkeypatch.setattr(
        "core.keyword_ideas.generate_keyword_ideas",
        _fake_generate_keyword_ideas(calls, suggestions=fake_suggestions),
    )

    args = tui_bridge.build_parser().parse_args(
        ["suggest-keywords", "--seed", "crm", "--previous", '["old idea"]']
    )
    exit_code = asyncio.run(tui_bridge.suggest_keywords(args))

    assert exit_code == 0
    assert calls["previous"] == ["old idea"]
    assert calls["target"] == 60

    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [json.loads(line) for line in out_lines]
    assert [event["type"] for event in events] == ["tool_call", "artifact_ready"]

    artifact = events[1]
    assert artifact["data"]["artifact"] == "keyword_suggestions"
    assert artifact["data"]["suggestions"] == fake_suggestions
    assert artifact["data"]["gscAvailable"] is True
    assert artifact["data"]["seed"] == "crm"
    # Default mode is angle, present on both the tool_call and artifact_ready events.
    assert calls["mode"] == "angle"
    assert events[0]["data"]["mode"] == "angle"
    assert artifact["data"]["mode"] == "angle"


def test_suggest_keywords_mode_flag_round_trips(monkeypatch, capsys):
    calls = {}
    monkeypatch.setattr(
        "core.keyword_ideas.generate_keyword_ideas",
        _fake_generate_keyword_ideas(calls, suggestions=[{"keyword": "x", "angle": "y", "score": None}]),
    )

    args = tui_bridge.build_parser().parse_args(
        ["suggest-keywords", "--seed", "crm", "--mode", "keyword"]
    )
    assert args.mode == "keyword"
    exit_code = asyncio.run(tui_bridge.suggest_keywords(args))

    assert exit_code == 0
    assert calls["mode"] == "keyword"
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    artifact = events[-1]
    assert artifact["type"] == "artifact_ready"
    assert artifact["data"]["mode"] == "keyword"


def test_suggest_keywords_error_path_on_exception(monkeypatch, capsys):
    calls = {}
    monkeypatch.setattr(
        "core.keyword_ideas.generate_keyword_ideas",
        _fake_generate_keyword_ideas(calls, raise_exc=RuntimeError("ideation exploded")),
    )

    args = tui_bridge.build_parser().parse_args(["suggest-keywords", "--seed", "crm"])
    exit_code = asyncio.run(tui_bridge.suggest_keywords(args))

    assert exit_code == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [event["type"] for event in events] == ["tool_call", "error"]
    assert "ideation exploded" in events[-1]["message"]


def test_suggest_keywords_empty_suggestions_is_an_error(monkeypatch, capsys):
    calls = {}
    monkeypatch.setattr(
        "core.keyword_ideas.generate_keyword_ideas",
        _fake_generate_keyword_ideas(calls, suggestions=[]),
    )

    args = tui_bridge.build_parser().parse_args(["suggest-keywords", "--seed", "crm"])
    exit_code = asyncio.run(tui_bridge.suggest_keywords(args))

    assert exit_code == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [event["type"] for event in events] == ["tool_call", "error"]
    assert events[-1]["message"] == "No keyword ideas produced"


def test_suggest_keywords_subparser_round_trips_flags():
    args = tui_bridge.build_parser().parse_args(
        ["suggest-keywords", "--trends-fresh", "--target", "70"]
    )

    assert args.command == "suggest-keywords"
    assert args.target == 70
    assert args.trends_fresh is True
    assert args.seed is None
    assert args.steer is None
    assert args.previous is None
    assert args.mode == "angle"


def test_suggest_keywords_malformed_previous_json_degrades_to_empty_list(monkeypatch, capsys):
    calls = {}
    monkeypatch.setattr(
        "core.keyword_ideas.generate_keyword_ideas",
        _fake_generate_keyword_ideas(calls, suggestions=[{"keyword": "x", "score": None}]),
    )

    args = tui_bridge.build_parser().parse_args(
        ["suggest-keywords", "--seed", "crm", "--previous", "not-json"]
    )
    exit_code = asyncio.run(tui_bridge.suggest_keywords(args))

    assert exit_code == 0
    assert calls["previous"] == []
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [event["type"] for event in events] == ["tool_call", "artifact_ready"]


# ---------------------------------------------------------------------------
# Accepted-ideas history (rolling, generation-time only)
# ---------------------------------------------------------------------------

def test_record_accepted_idea_appends_and_dedupes():
    tui_bridge._record_accepted_idea("A 90-day ad payback model")
    tui_bridge._record_accepted_idea("   ")  # blank never recorded
    assert tui_bridge._load_ideas_history() == ["A 90-day ad payback model"]

    # Re-accepting an old topic moves it to most-recent without duplicating.
    tui_bridge._record_accepted_idea("second topic")
    tui_bridge._record_accepted_idea("A 90-DAY AD PAYBACK MODEL")
    history = tui_bridge._load_ideas_history()
    assert len(history) == 2
    assert history[-1] == "A 90-DAY AD PAYBACK MODEL"


def test_ideas_history_caps_at_limit():
    path = tui_bridge.IDEAS_HISTORY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([f"idea {i}" for i in range(tui_bridge.IDEAS_HISTORY_LIMIT)]))

    tui_bridge._record_accepted_idea("newest accepted idea")
    history = tui_bridge._load_ideas_history()
    assert len(history) == tui_bridge.IDEAS_HISTORY_LIMIT
    assert history[-1] == "newest accepted idea"
    assert "idea 0" not in history  # oldest rotated out


def test_ideas_history_corrupt_file_never_blocks():
    tui_bridge.IDEAS_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tui_bridge.IDEAS_HISTORY_PATH.write_text("{definitely not json")
    assert tui_bridge._load_ideas_history() == []
    tui_bridge._record_accepted_idea("still works")
    assert tui_bridge._load_ideas_history() == ["still works"]


def test_suggest_keywords_merges_history_behind_staged_previous(monkeypatch, capsys):
    """History rides behind the TUI-staged batch, most-recent-first, deduped —
    so fresh launches exclude previously ACCEPTED topics."""
    tui_bridge._record_accepted_idea("older accepted topic")
    tui_bridge._record_accepted_idea("newer accepted topic")
    tui_bridge._record_accepted_idea("staged idea")  # also staged by the TUI → deduped

    calls = {}
    monkeypatch.setattr(
        "core.keyword_ideas.generate_keyword_ideas",
        _fake_generate_keyword_ideas(calls, suggestions=[{"keyword": "x", "angle": "y", "score": None}]),
    )
    args = tui_bridge.build_parser().parse_args(
        ["suggest-keywords", "--seed", "crm", "--previous", '["staged idea"]']
    )
    exit_code = asyncio.run(tui_bridge.suggest_keywords(args))

    assert exit_code == 0
    assert calls["previous"] == ["staged idea", "newer accepted topic", "older accepted topic"]
    # Suggesting alone records nothing — only acceptance (generation) writes.
    assert tui_bridge._load_ideas_history() == [
        "older accepted topic", "newer accepted topic", "staged idea"
    ]
    capsys.readouterr()


def test_parser_output_defaults_to_none_so_output_dir_env_applies(monkeypatch, tmp_path):
    from core.paths import output_root

    monkeypatch.setenv("FLEO_WORKDIR", str(tmp_path))
    monkeypatch.setenv("OUTPUT_DIR", "custom-out")

    parser = tui_bridge.build_parser()
    gen_args = parser.parse_args(["generate", "-k", "topic"])
    save_args = parser.parse_args(["save-artifact"])
    explicit_args = parser.parse_args(["save-artifact", "--output", "override-out"])

    assert gen_args.output is None
    assert save_args.output is None
    assert output_root(gen_args.output) == (tmp_path / "custom-out").resolve()
    assert output_root(save_args.output) == (tmp_path / "custom-out").resolve()
    assert output_root(explicit_args.output) == (tmp_path / "override-out").resolve()


def test_setup_status_runs_without_third_party_packages_and_flags_missing_runtime(tmp_path):
    """Simulates first-run `npx flyweel-fleo` on bare system Python before ~/.fleo/venv exists."""
    script = (
        "import importlib.abc, os, sys\n"
        "BLOCKED = {'openai', 'aiohttp', 'pydantic', 'yaml', 'bs4', 'defusedxml', 'rich', 'click', 'questionary'}\n"
        "class _Blocker(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path, target=None):\n"
        "        if fullname.split('.')[0] in BLOCKED:\n"
        "            raise ImportError(f'blocked {fullname}')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Blocker())\n"
        "import tui_bridge\n"
        "from core import paths\n"
        "paths.PACKAGE_ROOT = paths.Path(os.environ['TEST_PKG_ROOT'])\n"
        "sys.argv = ['tui_bridge.py', 'setup-status', '--run-id', 'bare_py']\n"
        "raise SystemExit(tui_bridge.main())\n"
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "home").mkdir()
    (tmp_path / "work" / ".fleo" / "brand").mkdir(parents=True)
    (tmp_path / "work" / ".fleo" / "brand" / "profile.json").write_text("null", encoding="utf-8")
    (tmp_path / "work" / ".fleo" / "site-contract.json").write_text("[]", encoding="utf-8")
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "FLEO_WORKDIR": str(tmp_path / "work"),
        "TEST_PKG_ROOT": str(tmp_path / "pkg"),
    }
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr
    ev = json.loads(proc.stdout.strip().splitlines()[0])
    assert ev["type"] == "setup_status"
    checks = {c["id"]: c for c in ev["data"]["checks"]}
    assert checks["venv"]["ok"] is False
    assert "venv" in ev["data"]["requiredMissing"]
    assert ev["data"]["canGenerate"] is False


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1d"])
def test_save_setup_changes_rejects_unicode_line_separators(tmp_path, sep):
    from core.tui_bridge import save_setup_changes

    with pytest.raises(ValueError, match="forbidden control characters"):
        save_setup_changes(
            {"OPENROUTER_API_KEY": f"sk-or-v1-abc{sep}FLEO_DRAFT_BASE_URL=https://evil.example/v1"},
            tmp_path,
        )


def test_env_symlink_is_rejected_on_save_and_ignored_on_load(tmp_path, monkeypatch):
    from core.paths import load_repo_env
    from core.tui_bridge import save_setup_changes

    secret_outside = tmp_path / "outside_creds"
    secret_outside.write_text("OPENROUTER_API_KEY=sk-or-v1-stolen\n[default]\naws_secret=123\n", encoding="utf-8")

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".env").symlink_to(secret_outside)

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert load_repo_env(ws) == {}
    assert "OPENROUTER_API_KEY" not in os.environ

    with pytest.raises(ValueError, match="regular file"):
        save_setup_changes({"OPENROUTER_API_KEY": "sk-or-v1-new"}, ws)
    assert (ws / ".env").is_symlink()


def test_save_setup_changes_warns_when_git_repo_does_not_ignore_dotenv(tmp_path):
    from core.tui_bridge import save_setup_changes

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    unignored = save_setup_changes({"OPENROUTER_API_KEY": "sk-or-v1-test"}, tmp_path)
    assert "gitIgnoreWarning" in unignored

    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    ignored = save_setup_changes({"OPENROUTER_API_KEY": "sk-or-v1-test"}, tmp_path)
    assert "gitIgnoreWarning" not in ignored


def test_detect_site_target_accepts_repo_path_flag_and_empty_stdin(tmp_path, monkeypatch, capsys):
    import io

    site_repo = tmp_path / "next-site"
    (site_repo / "content" / "blog").mkdir(parents=True)
    (site_repo / "next.config.mjs").write_text("export default {};\n", encoding="utf-8")
    (site_repo / "content" / "blog" / "hello.mdx").write_text(
        '---\ntitle: "Hello"\ndate: "2026-11-01"\n---\n\nBody\n',
        encoding="utf-8",
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("FLEO_WORKDIR", str(ws))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    rc = asyncio.run(tui_bridge.async_main(["detect-site-target", "--repo-path", str(site_repo)]))
    assert rc == 0
    events = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    ready = next(e for e in events if e["type"] == "artifact_ready")
    assert ready["data"]["contract"]["mode"] == "repo"
    assert ready["data"]["contract"]["framework"] == "nextjs"
    assert ready["data"]["contract"]["existing_post_count"] == 1

    # Bare invocation with empty stdin also inspects workspace_root()
    monkeypatch.setenv("FLEO_WORKDIR", str(site_repo))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc2 = asyncio.run(tui_bridge.async_main(["detect-site-target"]))
    assert rc2 == 0
    events2 = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    ready2 = next(e for e in events2 if e["type"] == "artifact_ready")
    assert ready2["data"]["contract"]["framework"] == "nextjs"


def test_detect_site_target_supports_content_dir_flag_and_json_payload(tmp_path, monkeypatch, capsys):
    import io

    site_repo = tmp_path / "astro-custom"
    custom_dir = site_repo / "src" / "content" / "guides"
    custom_dir.mkdir(parents=True)
    (site_repo / "astro.config.mjs").write_text("export default {};\n", encoding="utf-8")
    (custom_dir / "alpha.mdx").write_text(
        '---\ntitle: "Alpha"\nexcerpt: "Short excerpt"\npubDate: "2026-11-20"\n---\n\nBody\n',
        encoding="utf-8",
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("FLEO_WORKDIR", str(ws))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    # 1. CLI --repo-path + relative --content-dir
    rc = asyncio.run(
        tui_bridge.async_main(
            ["detect-site-target", "--repo-path", str(site_repo), "--content-dir", "src/content/guides"]
        )
    )
    assert rc == 0
    events = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    contract = next(e for e in events if e["type"] == "artifact_ready")["data"]["contract"]
    assert contract["framework"] == "astro"
    assert contract["repo_root"] == str(site_repo.resolve())
    assert contract["content_dir"] == str(custom_dir.resolve())
    assert contract["description_key"] == "excerpt"
    assert contract["date_key"] == "pubDate"

    # 2. CLI absolute --content-dir alone walks up to discover Astro repo root
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc2 = asyncio.run(tui_bridge.async_main(["detect-site-target", "--content-dir", str(custom_dir)]))
    assert rc2 == 0
    events2 = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    contract2 = next(e for e in events2 if e["type"] == "artifact_ready")["data"]["contract"]
    assert contract2["framework"] == "astro"
    assert contract2["repo_root"] == str(site_repo.resolve())
    assert contract2["content_dir"] == str(custom_dir.resolve())

    # 3. Stdin JSON payload with contentDir from the TUI Setup Wizard
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"mode": "repo", "repoPath": str(site_repo), "contentDir": "src/content/guides"})),
    )
    rc3 = asyncio.run(tui_bridge.async_main(["detect-site-target"]))
    assert rc3 == 0
    events3 = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    contract3 = next(e for e in events3 if e["type"] == "artifact_ready")["data"]["contract"]
    assert contract3["content_dir"] == str(custom_dir.resolve())

    # 4. Relative --content-dir pointing at a sibling repo from a .git workspace discovers sibling repo root
    (ws / ".git").mkdir()
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc4 = asyncio.run(
        tui_bridge.async_main(["detect-site-target", "--content-dir", "../astro-custom/src/content/guides"])
    )
    assert rc4 == 0
    events4 = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    contract4 = next(e for e in events4 if e["type"] == "artifact_ready")["data"]["contract"]
    assert contract4["framework"] == "astro"
    assert contract4["repo_root"] == str(site_repo.resolve())
    assert contract4["content_dir"] == str(custom_dir.resolve())

    # 5. detect_setup_status prioritizes workspace .env FLEO_BLOG_CONTENT_PATH over process ASTRO_BLOG_PATH and reports source
    override_dir = tmp_path / "env-override-posts"
    override_dir.mkdir()
    legacy_dir = tmp_path / "legacy-posts"
    legacy_dir.mkdir()
    (ws / ".env").write_text(f"FLEO_BLOG_CONTENT_PATH={override_dir}\n", encoding="utf-8")
    monkeypatch.delenv("FLEO_BLOG_CONTENT_PATH", raising=False)
    monkeypatch.setenv("ASTRO_BLOG_PATH", str(legacy_dir))
    status = detect_setup_status(ws)
    assert status["siteTarget"]["contentDir"] == str(override_dir.resolve())
    assert status["siteTarget"]["contentDirOverrideEnv"] == "FLEO_BLOG_CONTENT_PATH"

    # 6. Whitespace-only FLEO_BLOG_CONTENT_PATH resolves identically in blog_content_path and detect_setup_status
    from core.paths import blog_content_path

    monkeypatch.setenv("FLEO_BLOG_CONTENT_PATH", "   ")
    assert blog_content_path(repo_root=ws) == legacy_dir.resolve()
    status_ws = detect_setup_status(ws)
    assert status_ws["siteTarget"]["contentDir"] == str(legacy_dir.resolve())
    assert status_ws["siteTarget"]["contentDirOverrideEnv"] == "ASTRO_BLOG_PATH"
