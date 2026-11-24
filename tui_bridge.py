#!/usr/bin/env python3
"""JSONL bridge between the Ink TUI and the Python content engine."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.tui_bridge import (  # noqa: E402
    BridgeController,
    BridgeReporter,
    BridgeRunCancelled,
    JsonlEmitter,
    build_run_plan,
    detect_setup_status,
    new_run_id,
    save_local_result,
    save_setup_changes,
    summarize_generation_result,
)


def configure_bridge_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s:%(name)s:%(message)s",
        stream=sys.stderr,
        force=True,
    )


def _load_env_file() -> None:
    try:
        from core.paths import load_repo_env

        load_repo_env()
    except OSError as exc:
        print(f"WARNING: failed to read .env: {exc}", file=sys.stderr)


def add_generation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-k", "--keyword", required=True)
    parser.add_argument(
        "--style",
        default="standard",
        choices=["standard", "guide", "comparison", "research", "news", "category", "top-compare", "feature"],
    )
    parser.add_argument("--depth", default="standard", choices=["quick", "standard", "comprehensive"])
    parser.add_argument("--community-mode", default="full", choices=["full", "limited", "skip"])
    parser.add_argument("--brand-mode", "--flyweel-mode", dest="flyweel_mode", default="full", choices=["full", "limited", "none"])
    parser.add_argument("--no-icp", action="store_true")
    parser.add_argument("--no-gsc-check", action="store_false", dest="gsc_check", default=True)
    parser.add_argument("--no-gsc-keywords", action="store_false", dest="gsc_keywords", default=True)
    parser.add_argument("--gsc-gate-threshold", type=int, default=25, help="0-100 GSC score below which the HITL gate opens")
    parser.add_argument("--gsc-gate-estimated-threshold", type=int, default=10,
                        help="Stricter 0-100 bar for scores estimated from related queries (no direct GSC data)")
    parser.add_argument("--solutions", type=int, default=8, choices=[5, 8, 10, 15])
    parser.add_argument("--competitors", default=None, help="Comma-separated approved competitor or solution names")
    parser.add_argument("--no-competitors", action="store_true", help="Suppress competitor mentions/comparisons (ignored for comparison styles)")
    parser.add_argument("--note", default=None)
    parser.add_argument("--keyword-context", default=None)
    parser.add_argument("--save-local", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--overwrite", action="store_true", help="Replace only the exact collided artifact slug")
    parser.add_argument("--draft", action="store_true", help="Publish as draft: true (default is draft: false)")
    parser.add_argument("--no-draft", action="store_true")
    parser.add_argument("--save-research", action="store_true")
    parser.add_argument("--output", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--verbose", action="store_true")


async def emit_setup_status(run_id: str) -> int:
    from core.paths import workspace_root
    emitter = JsonlEmitter(run_id)
    status = detect_setup_status(workspace_root())
    emitter.emit(
        "setup_status",
        phase="setup",
        agent="Setup Agent",
        task_id="setup-status",
        message="Setup status detected",
        data=status,
    )
    return 0 if status.get("canGenerate") else 2


async def interpret_keyword(args: argparse.Namespace) -> int:
    _load_env_file()
    configure_bridge_logging(args.verbose)
    run_id = args.run_id or new_run_id()
    emitter = JsonlEmitter(run_id)
    if not args.keyword.strip():
        emitter.emit("error", phase="config", agent="Config Agent", task_id="topic-interpretation", message="Keyword is required")
        return 2
    try:
        emitter.emit(
            "tool_call",
            phase="config",
            agent="Config Agent",
            task_id="interpret-keyword",
            message="SmartAIRouter.interpret_keyword_context",
            data={"keyword": args.keyword, "style": args.style},
        )
        from core.ai_router import SmartAIRouter

        async with SmartAIRouter() as ai:
            context = await ai.interpret_keyword_context(
                args.keyword,
                args.style,
                user_feedback=args.feedback,
                previous_interpretation=args.previous,
            )
    except Exception as exc:  # noqa: BLE001
        emitter.emit(
            "error",
            phase="config",
            agent="Config Agent",
            task_id="topic-interpretation",
            message=f"Topic interpretation failed: {exc}",
        )
        return 1
    emitter.emit(
        "artifact_ready",
        phase="config",
        agent="Config Agent",
        task_id="topic-interpretation",
        message="Topic interpretation ready",
        data={
            "artifact": "topic_interpretation",
            "keyword": args.keyword,
            "style": args.style,
            "context": context or "",
        },
    )
    return 0


# Rolling history of ACCEPTED ideas — topics the user actually ran a generation
# for — so a fresh app launch doesn't re-suggest ground already covered (the TUI
# only stages the ideas shown within its own session). Deliberately NOT every
# shown idea: a shown-but-unpicked idea may still be good later, and permanently
# excluding it would starve future runs; short-term repetition of shown ideas is
# handled by the TUI's staged `previous` + the prompt's orthogonality push.
# Lives in the bridge runner, not core/keyword_ideas.py, so the core pipeline
# stays side-effect free for tests and library callers.
from core.paths import workspace_root as _workspace_root

IDEAS_HISTORY_PATH = _workspace_root() / "output" / ".keyword_ideas_history.json"
_DEFAULT_IDEAS_HISTORY_PATH = IDEAS_HISTORY_PATH


def _ideas_history_path() -> Path:
    if IDEAS_HISTORY_PATH != _DEFAULT_IDEAS_HISTORY_PATH:
        return IDEAS_HISTORY_PATH
    from core.paths import output_root, workspace_root
    return output_root(repo_root=workspace_root()) / ".keyword_ideas_history.json"


IDEAS_HISTORY_LIMIT = 500


def _load_ideas_history() -> list:
    try:
        data = json.loads(_ideas_history_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 — a corrupt history must never block ideation
        logging.warning("Ignoring unreadable keyword-ideas history: %s", exc)
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def _save_ideas_history(items: list) -> None:
    try:
        history_path = _ideas_history_path()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps(items[-IDEAS_HISTORY_LIMIT:], ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 — persistence is best-effort
        logging.warning("Could not persist keyword-ideas history: %s", exc)


async def suggest_keywords(args: argparse.Namespace) -> int:
    _load_env_file()
    configure_bridge_logging(args.verbose)
    run_id = args.run_id or new_run_id()
    emitter = JsonlEmitter(run_id)

    seed = args.seed
    steer = args.steer

    emitter.emit(
        "tool_call",
        phase="config",
        agent="Config Agent",
        task_id="suggest-keywords",
        message="generate_keyword_ideas",
        data={
            "seed": seed or "",
            "steer": steer or "",
            "style": args.style,
            "target": args.target,
            "trendsFresh": bool(args.trends_fresh),
            "mode": args.mode,
        },
    )

    previous_list: list = []
    if args.previous:
        try:
            parsed = json.loads(args.previous)
            if isinstance(parsed, list):
                previous_list = [str(item) for item in parsed]
            else:
                logging.warning(
                    "Ignoring --previous: expected a JSON list, got %s", type(parsed).__name__
                )
        except json.JSONDecodeError as exc:
            logging.warning("Ignoring malformed --previous JSON: %s", exc)

    # Merge the on-disk rolling history behind the TUI-staged batch so fresh
    # launches don't regenerate the same first five ideas every time. History
    # is stored oldest→newest; reverse it so merged_previous is ordered
    # most-recent-first (the prompt shows only the most recent slice, while the
    # exact-match dedupe consumes the whole list).
    history = _load_ideas_history()
    staged = {p.strip().casefold() for p in previous_list}
    merged_previous = previous_list + [
        h for h in reversed(history) if h.strip().casefold() not in staged
    ]

    try:
        from core.keyword_ideas import generate_keyword_ideas

        result = await generate_keyword_ideas(
            seed,
            args.style,
            steer=steer,
            previous=merged_previous,
            target=args.target,
            trends_fresh=args.trends_fresh,
            mode=args.mode,
        )
    except Exception as exc:  # noqa: BLE001
        emitter.emit(
            "error",
            phase="config",
            agent="Config Agent",
            task_id="keyword-suggestions",
            message=f"Keyword suggestion failed: {exc}",
        )
        return 1

    suggestions = result.get("suggestions") or []
    if not suggestions:
        emitter.emit(
            "error",
            phase="config",
            agent="Config Agent",
            task_id="keyword-suggestions",
            message="No keyword ideas produced",
        )
        return 1

    emitter.emit(
        "artifact_ready",
        phase="config",
        agent="Config Agent",
        task_id="keyword-suggestions",
        message="Keyword ideas ready",
        data={
            "artifact": "keyword_suggestions",
            "seed": seed or "",
            "steer": steer or "",
            "style": args.style,
            "target": result.get("target", args.target),
            "gscAvailable": bool(result.get("gsc_available")),
            "mode": result.get("mode", args.mode),
            "suggestions": suggestions,
        },
    )
    # NOTE: nothing is written back here — only ACCEPTED ideas enter the history,
    # recorded by run_generation when a topic actually becomes a run.
    return 0


def _record_accepted_idea(keyword: str) -> None:
    """Append an accepted topic (a generation was started for it) to the rolling
    ideas history. Best-effort; never blocks or fails a run."""
    accepted = (keyword or "").strip()
    if not accepted:
        return
    history = _load_ideas_history()
    accepted_cf = accepted.casefold()
    _save_ideas_history(
        [h for h in history if h.strip().casefold() != accepted_cf] + [accepted]
    )


async def run_generation(args: argparse.Namespace) -> int:
    _load_env_file()
    configure_bridge_logging(args.verbose)

    from core.generator import ContentGenerator
    from core.repo_extractor import RepoContextExtractor
    from core.user_note import NoteBundle

    run_id = args.run_id or new_run_id()
    emitter = JsonlEmitter(run_id)
    controller = BridgeController(run_id, emitter)
    controller.start_stdin_reader()

    async def stop_if_cancelled(phase: str) -> bool:
        # Give the stdin reader thread a chance to enqueue commands that were
        # already piped in before this bridge process reached async work.
        await asyncio.sleep(0.01)
        await controller.drain_pending_commands()
        if controller.cancelled:
            emitter.emit(
                "warning",
                phase=phase,
                agent="Bridge",
                message="Generation cancelled by TUI",
            )
            return True
        return False

    from core.paths import workspace_root
    setup_status = detect_setup_status(workspace_root())
    emitter.emit(
        "setup_status",
        phase="setup",
        agent="Setup Agent",
        task_id="setup-status",
        message="Setup status detected",
        data=setup_status,
    )
    if await stop_if_cancelled("setup"):
        return 130
    if not setup_status.get("canGenerate"):
        emitter.emit(
            "error",
            phase="setup",
            agent="Setup Agent",
            task_id="required-setup",
            message="Required setup checks failed",
            data={"requiredMissing": setup_status.get("requiredMissing", [])},
        )
        return 2

    # Seed the TUI's todo list before any work starts: the full config-aware
    # phase/task manifest for this run.
    emitter.emit(
        "run_plan",
        phase="setup",
        agent="Config Agent",
        task_id="run-plan",
        message="Run plan ready",
        data={
            "plan": build_run_plan(
                style=args.style,
                gsc_check=args.gsc_check,
                gsc_keywords=args.gsc_keywords,
                community_mode=args.community_mode,
                review_gate=True,
                save_local=args.save_local,
                publish=args.publish,
                depth=args.depth,
            ),
            "depth": args.depth,
        },
    )

    emitter.emit(
        "phase_started",
        phase="context",
        agent="Context Agent",
        task_id="context:repo-context",
        message="Loading Context",
    )
    try:
        emitter.emit(
            "tool_call",
            phase="context",
            agent="Context Agent",
            task_id="repo-context-extractor",
            message="RepoContextExtractor.extract_context",
        )
        emitter.emit(
            "task_progress",
            phase="context",
            agent="Context Agent",
            task_id="context:repo-context",
            message="Extracting brand context from repo",
            data={"status": "running", "label": "Repo Context"},
        )
        site_context = await asyncio.to_thread(RepoContextExtractor().extract_context)
        emitter.emit(
            "task_progress",
            phase="context",
            agent="Context Agent",
            task_id="context:repo-context",
            message="Repo context loaded",
            data={
                "status": "complete",
                "label": "Repo Context",
                "internalLinks": len(site_context.get("internal_links", {})),
                "features": len(site_context.get("features", [])),
            },
        )
    except Exception as exc:  # noqa: BLE001
        emitter.emit(
            "warning",
            phase="context",
            agent="Context Agent",
            task_id="context:repo-context",
            message=f"Repo context failed; falling back to live site context: {exc}",
        )
        site_context = None
    if await stop_if_cancelled("context"):
        return 130

    generator = ContentGenerator(site_context=site_context)
    reporter = BridgeReporter(emitter, controller)
    depth_to_solutions = {"quick": 8, "standard": 12, "comprehensive": 15}
    solution_count = args.solutions if args.style == "top-compare" else depth_to_solutions.get(args.depth, args.solutions)
    note_bundle = NoteBundle.from_global(args.note)
    approved_solutions = [
        item.strip()
        for item in (args.competitors or "").split(",")
        if item.strip()
    ] or None

    no_competitors = getattr(args, "no_competitors", False)
    if no_competitors and args.style in {"comparison", "top-compare"}:
        emitter.emit(
            "warning",
            phase="config",
            agent="Config Agent",
            message=f"--no-competitors is ignored for {args.style} style (competitor coverage is the point)",
        )

    emitter.emit(
        "phase_started",
        phase="generation",
        agent="Config Agent",
        task_id="generation-config",
        message="Starting generation",
        data={
            "keyword": args.keyword,
            "style": args.style,
            "depth": args.depth,
            "communityMode": args.community_mode,
            "flyweelMode": args.flyweel_mode,
            "skipIcp": args.no_icp,
            "gscCheck": args.gsc_check,
            "gscKeywords": args.gsc_keywords,
            "solutionCount": solution_count,
            "competitors": approved_solutions,
            "competitorMentions": not no_competitors,
        },
    )
    if await stop_if_cancelled("generation"):
        return 130

    try:
        emitter.emit(
            "tool_call",
            phase="generation",
            agent="ContentGenerator",
            task_id="content-generator",
            message="ContentGenerator.generate",
            data={"keyword": args.keyword, "style": args.style},
        )
        if await stop_if_cancelled("generation"):
            return 130
        result = await generator.generate(
            args.keyword,
            args.style,
            skip_community=args.community_mode == "skip",
            limit_community=args.community_mode == "limited",
            flyweel_mode=args.flyweel_mode,
            skip_icp=args.no_icp,
            solution_count=solution_count,
            gsc_check=args.gsc_check,
            gsc_keywords=args.gsc_keywords,
            approved_solutions=approved_solutions,
            note_bundle=note_bundle if not note_bundle.is_empty() else None,
            keyword_context=args.keyword_context,
            reporter=reporter,
            competitor_mentions=not no_competitors,
            review_gate_callback=controller.review_gate_callback,
            gsc_gate_callback=controller.gsc_gate_callback,
            gsc_gate_threshold=args.gsc_gate_threshold,
            gsc_gate_estimated_threshold=args.gsc_gate_estimated_threshold,
            depth=args.depth,
        )
    except BridgeRunCancelled as exc:
        emitter.emit("warning", phase="generation", agent="Bridge", message=str(exc))
        return 130
    except Exception as exc:  # noqa: BLE001
        emitter.emit("error", phase="generation", agent="Bridge", message=str(exc))
        return 1

    if not result.get("success"):
        event_type = "warning" if result.get("cancelled") else "error"
        emitter.emit(
            event_type,
            phase="generation",
            agent="ContentGenerator",
            message=result.get("error", "Generation failed"),
            data=result,
        )
        return 130 if result.get("cancelled") else 1

    # The topic is truly used only once an article was actually produced —
    # record it so future suggestion rounds don't re-pitch covered ground.
    # Cancelled/failed/gate-aborted runs never get here, so their topics stay
    # suggestible (and shown-but-unpicked ideas are never recorded at all).
    _record_accepted_idea(args.keyword)

    from core.mdx_security import assert_safe_mdx

    try:
        assert_safe_mdx(str(result.get("content", "")))
        if result.get("raw_content"):
            assert_safe_mdx(str(result["raw_content"]))
    except Exception as exc:  # noqa: BLE001
        emitter.emit(
            "error",
            phase="validation",
            agent="Validation Agent",
            task_id="mdx-security",
            message=str(exc),
        )
        return 1

    summary = summarize_generation_result(result)
    emitter.emit(
        "generation_complete",
        phase="preview",
        agent="Preview Agent",
        task_id="generation-complete",
        message="Generation complete",
        data=summary,
    )
    emitter.emit(
        "artifact_ready",
        phase="preview",
        agent="Preview Agent",
        task_id="final-mdx",
        message="Final MDX ready",
        data={
            "artifact": "final_mdx",
            "content": result.get("content", ""),
            "rawContent": result.get("raw_content", ""),
            "metadata": summary,
        },
    )

    if args.save_local or args.publish:
        try:
            saved = await save_local_result(
                result,
                output_dir=args.output,
                save_research=args.save_research,
                overwrite=args.overwrite,
            )
        except FileExistsError as exc:
            emitter.emit(
                "artifact_conflict",
                phase="output",
                agent="Output Agent",
                task_id="local-save",
                message=str(exc),
                data={"slug": result.get("slug"), "action": "save", "overwriteAllowed": True},
            )
            return 3
        emitter.emit(
            "saved",
            phase="output",
            agent="Output Agent",
            task_id="local-save",
            message="Saved local generation files",
            data=saved,
        )

    if args.publish:
        try:
            from core.publisher import AstroPublisher

            published = AstroPublisher().publish(
                content=result.get("content", ""),
                slug=result.get("slug", ""),
                draft=bool(getattr(args, "draft", False)) and not getattr(args, "no_draft", False),
                overwrite=args.overwrite,
            )
            if not published.get("success", False):
                if published.get("conflict"):
                    emitter.emit(
                        "artifact_conflict",
                        phase="output",
                        agent="Output Agent",
                        task_id="astro-publish",
                        message=published.get("error", "Publish target exists"),
                        data={
                            **published,
                            "slug": published.get("existing_slug") or result.get("slug"),
                            "action": "publish",
                            "overwriteAllowed": True,
                        },
                    )
                    return 3
                emitter.emit(
                    "error",
                    phase="output",
                    agent="Output Agent",
                    task_id="astro-publish",
                    message=published.get("error", "Publish failed"),
                    data=published,
                )
                return 1
            emitter.emit(
                "published",
                phase="output",
                agent="Output Agent",
                task_id="astro-publish",
                message="Published to Astro",
                data=published,
            )
        except Exception as exc:  # noqa: BLE001
            emitter.emit(
                "error",
                phase="output",
                agent="Output Agent",
                task_id="astro-publish",
                message=f"Publish failed: {exc}",
            )
            return 1

    return 0


def _read_artifact_result() -> Dict[str, Any]:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid artifact JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid artifact JSON: expected object")
    return payload


async def save_artifact(args: argparse.Namespace) -> int:
    configure_bridge_logging(args.verbose)
    run_id = args.run_id or new_run_id()
    emitter = JsonlEmitter(run_id)
    try:
        result = _read_artifact_result()
        emitter.emit(
            "tool_call",
            phase="output",
            agent="Output Agent",
            task_id="local-save",
            message="core.cli.app._save_local",
            data={"slug": result.get("slug")},
        )
        saved = await save_local_result(
            result,
            output_dir=args.output,
            save_research=args.save_research,
            overwrite=args.overwrite,
        )
    except FileExistsError as exc:
        emitter.emit(
            "artifact_conflict",
            phase="output",
            agent="Output Agent",
            task_id="local-save",
            message=str(exc),
            data={"slug": result.get("slug"), "action": "save", "overwriteAllowed": True},
        )
        return 3
    except Exception as exc:  # noqa: BLE001
        emitter.emit("error", phase="output", agent="Output Agent", task_id="local-save", message=str(exc))
        return 1
    emitter.emit(
        "saved",
        phase="output",
        agent="Output Agent",
        task_id="local-save",
        message="Saved local generation files",
        data=saved,
    )
    return 0


def _read_setup_changes() -> Dict[str, Any]:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid setup JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Invalid setup JSON: expected object")
    values = payload.get("values")
    if not isinstance(values, dict):
        raise ValueError("Invalid setup JSON: values object is required")
    return values


async def save_setup(args: argparse.Namespace) -> int:
    configure_bridge_logging(args.verbose)
    run_id = args.run_id or new_run_id()
    emitter = JsonlEmitter(run_id)
    try:
        values = _read_setup_changes()
        emitter.emit(
            "tool_call",
            phase="setup",
            agent="Setup Agent",
            task_id="save-setup",
            message="save_setup_changes",
            data={"keys": sorted(str(key).upper() for key in values.keys())},
        )
        from core.paths import workspace_root
        ws = workspace_root()
        saved = save_setup_changes(values, ws)
        for k, v in values.items():
            norm_k = str(k).strip().upper()
            if v is not None and str(v).strip():
                os.environ[norm_k] = str(v).strip()
            else:
                os.environ.pop(norm_k, None)
        status = detect_setup_status(ws)
    except Exception as exc:  # noqa: BLE001
        emitter.emit("error", phase="setup", agent="Setup Agent", task_id="save-setup", message=str(exc))
        return 1
    emitter.emit(
        "saved",
        phase="setup",
        agent="Setup Agent",
        task_id="save-setup",
        message="Setup changes saved",
        data=saved,
    )
    if saved.get("gitIgnoreWarning"):
        emitter.emit(
            "warning",
            phase="setup",
            agent="Setup Agent",
            task_id="save-setup",
            message=str(saved["gitIgnoreWarning"]),
        )
    emitter.emit(
        "setup_status",
        phase="setup",
        agent="Setup Agent",
        task_id="setup-status",
        message="Setup status detected",
        data=status,
    )
    return 0


async def publish_artifact(args: argparse.Namespace) -> int:
    configure_bridge_logging(args.verbose)
    run_id = args.run_id or new_run_id()
    emitter = JsonlEmitter(run_id)
    try:
        result = _read_artifact_result()
        from core.publisher import AstroPublisher

        is_draft = bool(getattr(args, "draft", False)) and not getattr(args, "live", False)
        emitter.emit(
            "tool_call",
            phase="output",
            agent="Output Agent",
            task_id="astro-publish",
            message="AstroPublisher.publish",
            data={"slug": result.get("slug"), "draft": is_draft},
        )
        published = AstroPublisher().publish(
            content=result.get("content", ""),
            slug=result.get("slug", ""),
            draft=is_draft,
            overwrite=args.overwrite,
        )
        if not published.get("success", False):
            if published.get("conflict"):
                emitter.emit(
                    "artifact_conflict",
                    phase="output",
                    agent="Output Agent",
                    task_id="astro-publish",
                    message=published.get("error", "Publish target exists"),
                    data={
                        **published,
                        "slug": published.get("existing_slug") or result.get("slug"),
                        "action": "publish",
                        "overwriteAllowed": True,
                    },
                )
                return 3
            emitter.emit(
                "error",
                phase="output",
                agent="Output Agent",
                task_id="astro-publish",
                message=published.get("error", "Publish failed"),
                data=published,
            )
            return 1
    except Exception as exc:  # noqa: BLE001
        emitter.emit("error", phase="output", agent="Output Agent", task_id="astro-publish", message=str(exc))
        return 1
    emitter.emit(
        "published",
        phase="output",
        agent="Output Agent",
        task_id="astro-publish",
        message="Published to Astro",
        data=published,
    )
    return 0


async def test_keys_cmd(args: argparse.Namespace) -> int:
    _load_env_file()
    configure_bridge_logging(args.verbose)
    run_id = args.run_id or new_run_id()
    emitter = JsonlEmitter(run_id)
    try:
        raw = sys.stdin.read().strip()
        payload = json.loads(raw) if raw else {}
        overrides = payload.get("values") if isinstance(payload, dict) and isinstance(payload.get("values"), dict) else {}
        from core.agent_credentials import test_agent_keys

        results = await test_agent_keys(overrides=overrides)
    except Exception as exc:  # noqa: BLE001
        emitter.emit("error", phase="setup", agent="Setup Agent", task_id="test-keys", message=str(exc))
        return 1
    emitter.emit(
        "artifact_ready",
        phase="setup",
        agent="Setup Agent",
        task_id="test-keys",
        message="API key check finished",
        data={"artifact": "key_test_results", **results},
    )
    return 0


async def scan_brand_url_cmd(args: argparse.Namespace) -> int:
    _load_env_file()
    configure_bridge_logging(args.verbose)
    run_id = args.run_id or new_run_id()
    emitter = JsonlEmitter(run_id)
    try:
        from core.ai_router import SmartAIRouter
        from core.brand_profile import synthesize_brand_profile_from_pages
        from core.site_adapters import detect_framework_from_html
        from core.site_markdown import load_site_pages_async
        from core.url_safety import safe_fetch_url_text, validate_public_http_url

        clean_url = validate_public_http_url(args.url)
        emitter.emit(
            "task_progress",
            phase="setup",
            agent="Brand Extractor",
            task_id="scan-brand",
            message=f"Crawling {clean_url}…",
            data={"status": "running", "label": "Scan Website"},
        )
        detected_fw = "markdown"
        try:
            _final_u, _ct, home_html = await safe_fetch_url_text(None, clean_url, timeout_seconds=10)
            detected_fw = detect_framework_from_html(home_html)
        except Exception:
            pass

        pages = await load_site_pages_async(site_url=clean_url)
        emitter.emit(
            "task_progress",
            phase="setup",
            agent="Brand Extractor",
            task_id="scan-brand",
            message=f"Synthesizing brand profile from {len(pages)} page(s)…",
            data={"status": "running", "label": "Synthesize Profile"},
        )
        async with SmartAIRouter() as ai:
            profile = await synthesize_brand_profile_from_pages(clean_url, pages, ai)
    except Exception as exc:  # noqa: BLE001
        emitter.emit("error", phase="setup", agent="Brand Extractor", task_id="scan-brand", message=str(exc))
        return 1

    emitter.emit(
        "artifact_ready",
        phase="setup",
        agent="Brand Extractor",
        task_id="scan-brand",
        message=f"Extracted brand profile for {profile.company_name}",
        data={
            "artifact": "brand_profile_draft",
            "profile": profile.model_dump(),
            "pagesCrawled": len(pages),
            "detectedFramework": detected_fw,
        },
    )
    return 0


async def save_brand_profile_cmd(args: argparse.Namespace) -> int:
    _load_env_file()
    configure_bridge_logging(args.verbose)
    run_id = args.run_id or new_run_id()
    emitter = JsonlEmitter(run_id)
    try:
        from core.brand_profile import (
            BrandProfile,
            build_profile_from_interview,
            discover_product_marketing_file,
            parse_product_marketing_markdown,
            save_brand_profile,
        )
        from core.paths import atomic_write_text, fleo_dir, workspace_root

        ws = workspace_root()
        payload = json.loads(sys.stdin.read() or "{}")
        if payload.get("skipBrand"):
            profile = BrandProfile()
            saved_paths = save_brand_profile(profile, repo_root=ws, sync_product_marketing_md=False)
        elif payload.get("importMarkdown"):
            pm_file = discover_product_marketing_file(repo_root=ws)
            if pm_file is None:
                raise FileNotFoundError("No .agents/product-marketing.md file found in workspace")
            profile = parse_product_marketing_markdown(pm_file.read_text(encoding="utf-8"))
            saved_paths = save_brand_profile(profile, repo_root=ws, sync_product_marketing_md=False)
        elif isinstance(payload.get("interview"), dict):
            profile = build_profile_from_interview(payload["interview"])
            saved_paths = save_brand_profile(profile, repo_root=ws)
        elif isinstance(payload.get("profile"), dict):
            profile = BrandProfile.model_validate(payload["profile"])
            saved_paths = save_brand_profile(profile, repo_root=ws)
        else:
            raise ValueError("Expected `profile`, `interview`, `importMarkdown`, or `skipBrand`")

        marker = fleo_dir(ws) / "onboarding.json"
        atomic_write_text(marker, json.dumps({"completed": True}, indent=2) + "\n", root=ws)
        status = detect_setup_status(ws)
    except Exception as exc:  # noqa: BLE001
        emitter.emit("error", phase="setup", agent="Setup Agent", task_id="save-brand-profile", message=str(exc))
        return 1

    emitter.emit("saved", phase="setup", agent="Setup Agent", task_id="save-brand-profile", message="Brand profile saved", data=saved_paths)
    emitter.emit("setup_status", phase="setup", agent="Setup Agent", task_id="setup-status", message="Setup status detected", data=status)
    return 0


async def detect_site_target_cmd(args: argparse.Namespace) -> int:
    _load_env_file()
    configure_bridge_logging(args.verbose)
    run_id = args.run_id or new_run_id()
    emitter = JsonlEmitter(run_id)
    try:
        from core.paths import atomic_write_text, fleo_dir, workspace_root
        from core.site_adapters import (
            SUPPORTED_FRAMEWORKS,
            SiteContract,
            learn_site_contract_from_repo,
            save_site_contract,
        )

        ws = workspace_root()
        cli_repo_path = getattr(args, "repo_path", None)
        cli_content_dir = getattr(args, "content_dir", None)
        cli_framework = getattr(args, "framework", None)
        raw_stdin = "" if (cli_repo_path or cli_content_dir or cli_framework or sys.stdin.isatty()) else sys.stdin.read().strip()
        payload = json.loads(raw_stdin) if raw_stdin else {}
        payload_content_dir = payload.get("contentDir") or payload.get("content_dir")
        raw_content_dir = str(cli_content_dir or payload_content_dir or "").strip() or None
        mode = (
            "repo"
            if cli_repo_path or cli_content_dir or (not cli_framework and not payload)
            else (
                "handoff"
                if cli_framework
                else str(payload.get("mode") or ("repo" if (payload.get("repoPath") or raw_content_dir) else "handoff")).strip()
            )
        )

        if mode == "repo":
            raw_repo_path = str(cli_repo_path or payload.get("repoPath") or "").strip()
            if not raw_repo_path and raw_content_dir:
                cand_cd = Path(raw_content_dir).expanduser()
                if cand_cd.is_absolute():
                    repo_dir = cand_cd.resolve()
                    explicit_cd: Optional[Path] = repo_dir
                else:
                    repo_dir = ws
                    explicit_cd = cand_cd
            else:
                repo_dir = Path(raw_repo_path or str(ws)).expanduser().resolve()
                explicit_cd = Path(raw_content_dir) if raw_content_dir else None
            contract = learn_site_contract_from_repo(repo_dir, content_dir=explicit_cd)
        else:
            fw = str(cli_framework or payload.get("framework") or "markdown").strip().lower()
            if fw not in SUPPORTED_FRAMEWORKS:
                fw = "markdown"
            contract = SiteContract(mode="handoff", framework=fw)  # type: ignore[arg-type]

        contract_path = save_site_contract(contract, repo_root=ws)
        marker = fleo_dir(ws) / "onboarding.json"
        atomic_write_text(marker, json.dumps({"completed": True}, indent=2) + "\n", root=ws)
        status = detect_setup_status(ws)
    except Exception as exc:  # noqa: BLE001
        emitter.emit("error", phase="setup", agent="Setup Agent", task_id="detect-site-target", message=str(exc))
        return 1

    emitter.emit(
        "artifact_ready",
        phase="setup",
        agent="Setup Agent",
        task_id="detect-site-target",
        message="Site target configured",
        data={"artifact": "site_contract", "contract": contract.to_dict(), "path": contract_path},
    )
    emitter.emit("setup_status", phase="setup", agent="Setup Agent", task_id="setup-status", message="Setup status detected", data=status)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fleo by Flyweel TUI bridge")
    parser.add_argument("--run-id", default=None)
    subparsers = parser.add_subparsers(dest="command")

    setup = subparsers.add_parser("setup-status", help="Emit setup status JSONL and exit")
    setup.add_argument("--run-id", default=None)

    generate = subparsers.add_parser("generate", help="Run generation and emit JSONL events")
    add_generation_args(generate)

    interpret = subparsers.add_parser("interpret-keyword", help="Generate a topic interpretation JSONL artifact")
    interpret.add_argument("--run-id", default=None)
    interpret.add_argument("-k", "--keyword", required=True)
    interpret.add_argument("--style", default="standard", choices=["standard", "guide", "comparison", "research", "news", "category", "top-compare", "feature"])
    interpret.add_argument("--feedback", default=None)
    interpret.add_argument("--previous", default=None)
    interpret.add_argument("--verbose", action="store_true")

    suggest = subparsers.add_parser("suggest-keywords", help="Generate 5 GSC-scored keyword ideas as a JSONL artifact")
    suggest.add_argument("--run-id", default=None)
    suggest.add_argument("--seed", default=None)
    suggest.add_argument("--style", default="standard", choices=["standard", "guide", "comparison", "research", "news", "category", "top-compare", "feature"])
    suggest.add_argument("--steer", default=None)
    suggest.add_argument("--previous", default=None, help="JSON list of previously suggested keywords")
    suggest.add_argument("--target", type=int, default=60)
    suggest.add_argument("--trends-fresh", action="store_true")
    suggest.add_argument("--mode", default="angle", choices=["keyword", "angle"])
    suggest.add_argument("--verbose", action="store_true")

    save = subparsers.add_parser("save-artifact", help="Read a result JSON object on stdin and save local files")
    save.add_argument("--run-id", default=None)
    save.add_argument("--output", default=None)
    save.add_argument("--save-research", action="store_true")
    save.add_argument("--overwrite", action="store_true")
    save.add_argument("--verbose", action="store_true")

    setup_save = subparsers.add_parser("save-setup", help="Read confirmed setup values on stdin and merge them into .env")
    setup_save.add_argument("--run-id", default=None)
    setup_save.add_argument("--verbose", action="store_true")

    publish = subparsers.add_parser("publish-artifact", help="Read a result JSON object on stdin and publish to connected site")
    publish.add_argument("--run-id", default=None)
    publish.add_argument("--draft", action="store_true")
    publish.add_argument("--live", action="store_true")
    publish.add_argument("--overwrite", action="store_true")
    publish.add_argument("--verbose", action="store_true")

    tkeys = subparsers.add_parser("test-keys", help="Live-test API keys passed via stdin JSON")
    tkeys.add_argument("--run-id", default=None)
    tkeys.add_argument("--verbose", action="store_true")

    scan_url = subparsers.add_parser("scan-brand-url", help="Crawl a website URL and synthesize a BrandProfile draft")
    scan_url.add_argument("--run-id", default=None)
    scan_url.add_argument("--url", required=True)
    scan_url.add_argument("--verbose", action="store_true")

    save_bp = subparsers.add_parser("save-brand-profile", help="Persist a BrandProfile JSON or interview answers from stdin")
    save_bp.add_argument("--run-id", default=None)
    save_bp.add_argument("--verbose", action="store_true")

    site_tgt = subparsers.add_parser("detect-site-target", help="Configure handoff framework or inspect a local website repo")
    site_tgt.add_argument("--run-id", default=None)
    site_tgt.add_argument("--repo-path", default=None, help="Target website repository path (or content subfolder)")
    site_tgt.add_argument("--content-dir", default=None, help="Target folder path for published articles (relative to repo or absolute)")
    site_tgt.add_argument("--framework", default=None, help="Target handoff framework")
    site_tgt.add_argument("--verbose", action="store_true")

    return parser


async def async_main(argv: Optional[list[str]] = None) -> int:
    _load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "setup-status":
        return await emit_setup_status(args.run_id or new_run_id())
    if args.command == "interpret-keyword":
        return await interpret_keyword(args)
    if args.command == "suggest-keywords":
        return await suggest_keywords(args)
    if args.command == "generate":
        return await run_generation(args)
    if args.command == "save-artifact":
        return await save_artifact(args)
    if args.command == "save-setup":
        return await save_setup(args)
    if args.command == "publish-artifact":
        return await publish_artifact(args)
    if args.command == "test-keys":
        return await test_keys_cmd(args)
    if args.command == "scan-brand-url":
        return await scan_brand_url_cmd(args)
    if args.command == "save-brand-profile":
        return await save_brand_profile_cmd(args)
    if args.command == "detect-site-target":
        return await detect_site_target_cmd(args)
    parser.print_help(sys.stderr)
    return 2


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        print("Bridge interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
