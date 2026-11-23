"""Display-agnostic pipeline progress reporting.

The generator reports every phase/task transition through a
``PipelineReporter`` instead of driving Rich display objects directly.
Consumers plug in as adapters:

- ``RichReporter`` (core/cli/rich_reporter.py) renders the classic Rich
  live panels for generate.py — visually identical to the pre-reporter CLI.
- ``BridgeReporter`` (core/tui_bridge.py) emits JSONL ``task_progress``
  events for the Ink TUI — the first-class machine-readable path.
- ``CallbackReporter`` wraps a plain ``callback(message, advance)`` so
  legacy callers (non-TTY generate.py, external scripts) keep receiving
  coarse progress messages.

This module must stay free of Rich imports and stdout side effects.

Status vocabulary (shared with the Ink TUI and the Rich glyph map):
``pending`` ○ · ``running`` ◐ · ``complete`` ● · ``skipped`` ⊘ · ``failed`` ✗

Advance semantics mirror ``LiveStatusDisplay``: each *terminal* transition
(complete/fail/skip) counts one discrete pipeline step; ``update_task`` is a
display-only refinement that never advances the pipeline bar (used e.g. for
the Research Index Search row so the research phase keeps its 4-step math).
"""
from __future__ import annotations

import re
import inspect
from typing import Callable, Optional, Sequence

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

TASK_STATUSES = {
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_COMPLETE,
    STATUS_SKIPPED,
    STATUS_FAILED,
}


def slugify_task(name: str) -> str:
    """Stable slug for task ids: ``"Part 1/3"`` → ``"part-1-3"``."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "task"


def task_id(phase: str, task: str) -> str:
    """Phase-scoped task id: ``("research", "SERP Analysis")`` → ``"research:serp-analysis"``."""
    return f"{phase}:{slugify_task(task)}"


class PipelineReporter:
    """No-op base reporter. Adapters override the methods they render.

    ``phase`` is always a manifest key from ``core.cli.progress.PIPELINE_PHASES``
    (``gsc``, ``research``, ``draft``, ``formatting``, ``refinement``,
    ``validation``, …); ``label`` is the human phase title ("Research").
    ``elapsed`` is seconds as a float (adapters format it for display).
    """

    #: True only for adapters that own a Rich console (gates the Rich-only
    #: summary panels in the generator; reporting itself is unconditional).
    renders_rich_console: bool = False

    #: True for adapters that render live-updating rows (Rich Live panels,
    #: the Ink TUI). Gates high-frequency cosmetic updates like the SERP
    #: pulse animation so plain-text consumers aren't spammed with frames.
    renders_live: bool = False

    def start_phase(self, phase: str, label: str, tasks: Sequence[str]) -> None:
        """Open a phase with its full task manifest (all tasks pending)."""

    def end_phase(self, phase: str) -> None:
        """Close a phase (Rich stops its Live region; JSONL is a no-op)."""

    def start_task(self, phase: str, task: str, detail: str = "") -> None:
        """Mark a task running. Safe to call repeatedly (pulse animations)."""

    def update_task(
        self,
        phase: str,
        task: str,
        status: str,
        detail: str = "",
        elapsed: Optional[float] = None,
    ) -> None:
        """Display-only status/detail refinement — never advances the pipeline."""

    def complete_task(
        self,
        phase: str,
        task: str,
        detail: str = "",
        elapsed: Optional[float] = None,
    ) -> None:
        """Mark a task done. Advances one discrete pipeline step."""

    def fail_task(self, phase: str, task: str, reason: str = "Failed") -> None:
        """Mark a task failed. Advances one discrete pipeline step."""

    def skip_task(self, phase: str, task: str, reason: str = "Skipped") -> None:
        """Mark a task skipped. Advances one discrete pipeline step."""

    def set_task_progress(
        self,
        phase: str,
        task: str,
        current: int,
        total: int,
        *,
        insights: Optional[int] = None,
        query: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        """Mid-task n/m progress (mining query loops) with optional query preview."""

    def message(self, text: str, advance: float = 0) -> None:
        """Legacy coarse progress message (pipeline-bar description updates)."""


class NullReporter(PipelineReporter):
    """Explicit no-op reporter for callers that pass no progress channel."""


class CallbackReporter(PipelineReporter):
    """Back-compat adapter over a plain ``callback(message, advance)``.

    Forwards phase labels and terminal task transitions as the coarse
    messages legacy consumers expect (non-TTY generate.py PLAIN/JSON modes,
    external callers). Advance math matches ``LiveStatusDisplay``: one step
    per terminal transition, nothing for display-only updates.
    """

    def __init__(self, callback: Callable[[str, int], None]):
        self._callback = callback
        signature = inspect.signature(callback)
        try:
            signature.bind("message", 0)
            self._accepts_advance = True
        except TypeError:
            signature.bind("message")
            self._accepts_advance = False

    def _emit(self, text: str, advance: float = 0) -> None:
        if self._accepts_advance:
            self._callback(text, advance)
        else:
            self._callback(text)  # type: ignore[call-arg]

    def start_phase(self, phase: str, label: str, tasks: Sequence[str]) -> None:
        self._emit(label, 0)

    def start_task(self, phase: str, task: str, detail: str = "") -> None:
        self._emit(f"{task} — {detail}" if detail else task, 0)

    def complete_task(
        self,
        phase: str,
        task: str,
        detail: str = "",
        elapsed: Optional[float] = None,
    ) -> None:
        self._emit(f"✓ {task}: {detail}" if detail else f"✓ {task}", 1)

    def fail_task(self, phase: str, task: str, reason: str = "Failed") -> None:
        self._emit(f"✗ {task}: {reason}", 1)

    def skip_task(self, phase: str, task: str, reason: str = "Skipped") -> None:
        self._emit(f"⊘ {task}: {reason}", 1)

    def message(self, text: str, advance: float = 0) -> None:
        self._emit(text, advance)


def build_reporter(
    progress_callback: Optional[Callable[[str, int], None]] = None,
) -> PipelineReporter:
    """Pick the right adapter for a legacy ``progress_callback``.

    - ``None`` → ``NullReporter``
    - callback exposing a live ``pipeline_renderable()`` (the Rich CLI's
      ``ProgressCallback`` on a TTY) → ``RichReporter``
    - any other callable → ``CallbackReporter``

    New callers should construct a reporter directly and pass it to
    ``ContentGenerator.generate(reporter=...)`` instead.
    """
    if progress_callback is None:
        return NullReporter()

    if hasattr(progress_callback, "pipeline_renderable"):
        try:
            rich_capable = progress_callback.pipeline_renderable() is not None
        except Exception:
            rich_capable = False
        if rich_capable:
            # Lazy import keeps this module Rich-free for bridge consumers.
            from .cli.rich_reporter import RichReporter

            return RichReporter(progress_callback)

    return CallbackReporter(progress_callback)


__all__ = [
    "PipelineReporter",
    "NullReporter",
    "CallbackReporter",
    "build_reporter",
    "slugify_task",
    "task_id",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_COMPLETE",
    "STATUS_SKIPPED",
    "STATUS_FAILED",
    "TASK_STATUSES",
]
