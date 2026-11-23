"""Rich adapter for the display-agnostic PipelineReporter protocol.

Renders phase/task transitions through the existing ``LiveStatusDisplay``
machinery so generate.py's live panels stay visually identical to the
pre-reporter CLI. Rich is one pluggable consumer of the reporter protocol;
the generator itself no longer knows about display objects.

Every display operation is guarded — a rendering glitch must never break
the generation pipeline.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from ..progress_reporter import PipelineReporter
from .live_display import LiveStatusDisplay


def _fmt_elapsed(elapsed: Optional[float]) -> str:
    return f"{elapsed:.1f}s" if elapsed is not None else "✓"


class RichReporter(PipelineReporter):
    """Adapts reporter calls onto LiveStatusDisplay + the pipeline bar.

    ``callback`` is the Rich CLI's ``ProgressCallback``: callable
    ``(message, advance)`` exposing ``pipeline_renderable()`` and
    ``advance_pipeline_fractional()``. Discrete/fractional pipeline-bar
    advances keep flowing through ``LiveStatusDisplay``'s own bindings, so
    the bar math is unchanged.
    """

    renders_rich_console = True
    renders_live = True

    def __init__(self, callback: Callable[[str, int], None]):
        self._callback = callback
        self._display: Optional[LiveStatusDisplay] = None
        self._live = None
        self._phase: Optional[str] = None

    # ── phase lifecycle ──────────────────────────────────────────────────

    def start_phase(self, phase: str, label: str, tasks: Sequence[str]) -> None:
        self.end_phase(self._phase or "")
        try:
            display = LiveStatusDisplay()
            display.set_phase(label)
            for task in tasks:
                display.add_task(task)
            display.bind_progress(self._callback)
            if hasattr(self._callback, "advance_pipeline_fractional"):
                display.bind_fractional(self._callback.advance_pipeline_fractional)

            # Override the outer phase description so the pipeline bar
            # reflects the sub-phase running now.
            self._callback(label, 0)

            pipeline_bar = self._callback.pipeline_renderable() \
                if hasattr(self._callback, "pipeline_renderable") else None
            live = display.start_live_grouped(pipeline_bar, refresh_per_second=2)
            live.start()
            self._display = display
            self._live = live
            self._phase = phase
        except Exception:
            self._display = None
            self._live = None
            self._phase = None

    def end_phase(self, phase: str) -> None:
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
        self._display = None
        self._live = None
        self._phase = None

    # ── task transitions ─────────────────────────────────────────────────

    def _refresh(self) -> None:
        if self._display is not None:
            try:
                self._display.refresh()
            except Exception:
                pass

    def start_task(self, phase: str, task: str, detail: str = "") -> None:
        if self._display is None:
            return
        try:
            self._display.start_task(task, detail)
        except Exception:
            pass
        self._refresh()

    def update_task(
        self,
        phase: str,
        task: str,
        status: str,
        detail: str = "",
        elapsed: Optional[float] = None,
    ) -> None:
        if self._display is None:
            return
        try:
            self._display.update_task(
                task, status, detail,
                _fmt_elapsed(elapsed) if elapsed is not None else "",
            )
        except Exception:
            pass
        self._refresh()

    def complete_task(
        self,
        phase: str,
        task: str,
        detail: str = "",
        elapsed: Optional[float] = None,
    ) -> None:
        if self._display is None:
            return
        try:
            self._display.complete_task(task, detail, _fmt_elapsed(elapsed))
        except Exception:
            pass
        self._refresh()

    def fail_task(self, phase: str, task: str, reason: str = "Failed") -> None:
        if self._display is None:
            return
        try:
            self._display.fail_task(task, reason)
        except Exception:
            pass
        self._refresh()

    def skip_task(self, phase: str, task: str, reason: str = "Skipped") -> None:
        if self._display is None:
            return
        try:
            self._display.skip_task(task, reason)
        except Exception:
            pass
        self._refresh()

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
        if self._display is None:
            return
        try:
            self._display.set_task_progress(
                task, current, total,
                insights=insights, query=query, note=note,
            )
        except Exception:
            pass
        self._refresh()

    # ── coarse messages ──────────────────────────────────────────────────

    def message(self, text: str, advance: float = 0) -> None:
        try:
            self._callback(text, advance)
        except Exception:
            pass


__all__ = ["RichReporter"]
