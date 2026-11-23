"""Progress Manager for CLI

One persistent pipeline progress bar lives for the whole generation run.
Phase context managers no longer own their own Rich Live region; instead the
pipeline bar is composed into the active LiveStatusDisplay via
``pipeline_renderable()``. This eliminates the suspend/resume dance that
previously hid the bottom bar for ~95% of every run.

Gracefully degrades in non-TTY environments.
"""
import sys
from typing import Optional, Dict, Any
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
)
from rich.console import Console, RenderableType
from rich.table import Column

from .output import OutputManager, OutputMode


@dataclass
class PhaseConfig:
    """Configuration for a pipeline phase"""
    name: str
    steps: int = 1
    description: str = ""


# Pipeline phases in order. ``steps`` here is the number of user-visible
# subtasks the LiveStatusDisplay bridges advance per phase. The pipeline bar's
# total is the sum of these.
PIPELINE_PHASES: Dict[str, PhaseConfig] = {
    'context':      PhaseConfig('Loading Context', steps=1, description='Extracting brand context from repo'),
    'gsc':          PhaseConfig('GSC Preflight',   steps=4, description='Cannibalization, score and keyword checks'),
    'discovery':    PhaseConfig('Discovery',       steps=1, description='Finding comparison candidates'),
    'research':     PhaseConfig('Research',        steps=4, description='SERP analysis and community mining'),
    'draft':        PhaseConfig('Draft',           steps=3, description='Gemini 3-part draft'),
    'formatting':   PhaseConfig('Formatting',      steps=2, description='MDX repair and Astro formatting'),
    'refinement':   PhaseConfig('Refinement',      steps=6, description='Edit, citations, polish, headings and title'),
    'validation':   PhaseConfig('Validation',      steps=2, description='Schema refresh and citation analysis'),
    'output':       PhaseConfig('Output',          steps=1, description='Save or publish content'),
    # Back-compat keys used by older call sites.
    'generation':   PhaseConfig('Draft',           steps=3, description='Gemini 3-part draft'),
    'polishing':    PhaseConfig('Refinement',      steps=6, description='Edit, citations, polish, headings and title'),
}

PIPELINE_TOTAL_STEPS: int = sum(
    p.steps for key, p in PIPELINE_PHASES.items()
    if key not in {'generation', 'polishing'}
)


class ProgressManager:
    """Persistent pipeline progress tracker.

    Owns one ``Progress`` instance whose single task represents the entire
    generation pipeline. Nested LiveStatusDisplays compose this renderable
    into their own Live region (see ``LiveStatusDisplay.as_group``) so the
    bar is visible at all times, not just during context/formatting.
    """

    def __init__(
        self,
        output: Optional[OutputManager] = None,
        console: Optional[Console] = None,
    ):
        self.output = output or OutputManager()
        from .console import CONSOLE
        self.console = console or CONSOLE
        self._is_tty = sys.stdout.isatty()

        self._current_phase: Optional[str] = None
        self._phase_start: Optional[datetime] = None
        self._phase_times: Dict[str, float] = {}

        self._pipeline: Optional[Progress] = None
        self._pipeline_task: Optional[int] = None
        self._pipeline_completed: float = 0.0
        self._last_plain_message: Optional[str] = None
        self._pipeline_failed: Optional[str] = None
        self._pipeline_start: Optional[datetime] = None

    # ─────────────────────────────────────────────────────────────────────
    # Pipeline bar lifecycle
    # ─────────────────────────────────────────────────────────────────────

    def _ensure_pipeline(self) -> None:
        if self._pipeline is not None or not self._can_use_rich():
            return
        # Every column to the right of BarColumn is pinned to a fixed cell
        # width so its rendered text never changes the surrounding layout.
        # Variable widths (e.g. TaskProgressColumn going "9%" → "10%") would
        # otherwise shift TimeElapsedColumn one column to the right and the
        # whole line would re-render — perceived as a per-second flash.
        self._pipeline = Progress(
            TextColumn("[bold cyan]Pipeline", table_column=Column(width=9, no_wrap=True)),
            BarColumn(bar_width=None, complete_style="cyan", finished_style="green"),
            TaskProgressColumn(
                table_column=Column(width=5, no_wrap=True, justify="right"),
            ),
            TextColumn(
                "[dim]• {task.description}",
                table_column=Column(width=42, no_wrap=True, overflow="ellipsis"),
            ),
            TimeElapsedColumn(
                table_column=Column(width=8, no_wrap=True, justify="right"),
            ),
            console=self.console,
            transient=False,
            expand=True,
        )
        self._pipeline_task = self._pipeline.add_task(
            "starting…",
            total=PIPELINE_TOTAL_STEPS,
        )

    def pipeline_renderable(self) -> Optional[RenderableType]:
        if not self._can_use_rich():
            return None
        self._ensure_pipeline()
        return self._pipeline

    def advance_pipeline(self, steps: float = 1.0, message: Optional[str] = None) -> None:
        """Advance the persistent pipeline bar.

        Args:
            steps: Number of pipeline steps to add (fractional allowed for
                smooth mid-task progress like Reddit query 5/10).
            message: Optional new description (shown after the • marker).
        """
        if not self._can_use_rich():
            return
        self._ensure_pipeline()
        if self._pipeline is None or self._pipeline_task is None:
            return
        self._pipeline_completed = min(
            float(PIPELINE_TOTAL_STEPS),
            max(0.0, self._pipeline_completed + float(steps)),
        )
        kwargs: Dict[str, Any] = {"completed": self._pipeline_completed}
        if message is not None:
            kwargs["description"] = message
        self._pipeline.update(self._pipeline_task, **kwargs)

    def advance_pipeline_fractional(self, fractional_steps: float) -> None:
        """Bridge target for LiveStatusDisplay.bind_fractional()."""
        self.advance_pipeline(steps=fractional_steps)

    def finalize_pipeline(self) -> None:
        if not self._can_use_rich() or self._pipeline is None or self._pipeline_task is None:
            return
        if self._pipeline_failed:
            self._pipeline.update(
                self._pipeline_task,
                description=f"failed: {self._pipeline_failed}",
            )
            try:
                self.console.print(self._pipeline)
            except Exception:
                pass
            return
        self._pipeline_completed = float(PIPELINE_TOTAL_STEPS)
        self._pipeline.update(
            self._pipeline_task,
            completed=self._pipeline_completed,
            description="complete",
        )
        try:
            self.console.print(self._pipeline)
        except Exception:
            pass

    def fail_pipeline(self, message: str) -> None:
        """Mark the pipeline as failed so final output does not claim success."""
        self._pipeline_failed = (message or "failed").splitlines()[0][:80]
        if self._can_use_rich():
            self._ensure_pipeline()
            if self._pipeline is not None and self._pipeline_task is not None:
                self._pipeline.update(self._pipeline_task, description=f"failed: {self._pipeline_failed}")
        elif self.output.mode == OutputMode.JSON:
            self.output._log("error", self._pipeline_failed)
        elif self.output.mode == OutputMode.PLAIN:
            print(f"[FAILED] {self._pipeline_failed}")

    # ─────────────────────────────────────────────────────────────────────
    # Capability checks
    # ─────────────────────────────────────────────────────────────────────

    def _can_use_rich(self) -> bool:
        return (
            self.output.mode == OutputMode.RICH
            and self._is_tty
            and self.console is not None
        )

    # ─────────────────────────────────────────────────────────────────────
    # Phase tracking (timing + label only — no nested Live)
    # ─────────────────────────────────────────────────────────────────────

    @contextmanager
    def phase(self, phase_key: str, description: Optional[str] = None):
        config = PIPELINE_PHASES.get(phase_key, PhaseConfig(phase_key))
        desc = description or config.description or config.name

        self._current_phase = phase_key
        self._phase_start = datetime.now()

        if self._can_use_rich():
            self._ensure_pipeline()
            self.advance_pipeline(steps=0.0, message=f"{config.name} — {desc}")
        elif self.output.mode == OutputMode.PLAIN:
            print(f"[PHASE] {config.name}: {desc}")

        phase_succeeded = False
        try:
            yield self
            phase_succeeded = True
        finally:
            elapsed = (datetime.now() - self._phase_start).total_seconds()
            self._phase_times[phase_key] = elapsed
            if phase_succeeded:
                if self._can_use_rich():
                    self.console.print(f"[green]✓[/green] {config.name} ({elapsed:.1f}s)")
                elif self.output.mode == OutputMode.PLAIN:
                    print(f"[DONE] {config.name} ({elapsed:.1f}s)")
            else:
                if self._can_use_rich():
                    self.console.print(f"[red]✗[/red] {config.name} failed ({elapsed:.1f}s)")
                elif self.output.mode == OutputMode.PLAIN:
                    print(f"[FAILED] {config.name} ({elapsed:.1f}s)")
            self._current_phase = None
            self._phase_start = None

    def update(self, message: str, advance: int = 0) -> None:
        """Update pipeline progress (back-compat for ProgressCallback.__call__).

        An empty ``message`` is normalised to ``None`` so step-done advances
        from ``LiveStatusDisplay`` don't stomp the phase label.
        """
        if advance or message:
            if self._can_use_rich():
                self.advance_pipeline(steps=float(advance or 0), message=(message or None))
            else:
                if advance:
                    self._pipeline_completed = min(
                        float(PIPELINE_TOTAL_STEPS),
                        max(0.0, self._pipeline_completed + float(advance)),
                    )
                if message:
                    if self.output.mode == OutputMode.JSON:
                        self.output._log(
                            "progress",
                            message,
                            {
                                "advance": advance,
                                "completed": self._pipeline_completed,
                                "total": PIPELINE_TOTAL_STEPS,
                            },
                        )
                    elif self.output.mode == OutputMode.PLAIN and message != self._last_plain_message:
                        print(f"[PROGRESS] {message}")
                        self._last_plain_message = message

    # ─────────────────────────────────────────────────────────────────────
    # Back-compat no-ops (former suspend/resume dance is no longer needed)
    # ─────────────────────────────────────────────────────────────────────

    def mark_bridged(self) -> None:
        return None

    @contextmanager
    def suspend(self):
        yield

    @contextmanager
    def spinner(self, message: str):
        if self._can_use_rich():
            with Progress(
                SpinnerColumn(spinner_name="dots", style="cyan"),
                TextColumn("[dim]{task.description}"),
                console=self.console,
                transient=True,
            ) as progress:
                progress.add_task(message, total=None)
                yield
        else:
            if self.output.mode == OutputMode.PLAIN:
                print(f"[...] {message}")
            yield

    @contextmanager
    def full_pipeline(self):
        self._pipeline_start = datetime.now()
        if self._can_use_rich():
            self.console.print()
            self.console.print("[bold cyan]Starting generation pipeline…[/bold cyan]")
            self.console.print()
            self._ensure_pipeline()

        try:
            yield self
        except BaseException as exc:
            if not self._pipeline_failed:
                self.fail_pipeline(str(exc) or type(exc).__name__)
            raise
        finally:
            total_time = (
                (datetime.now() - self._pipeline_start).total_seconds()
                if self._pipeline_start else sum(self._phase_times.values())
            )
            if self._can_use_rich():
                self.finalize_pipeline()
                self.console.print()
                if self._pipeline_failed:
                    self.console.print(f"[bold red]Pipeline failed[/bold red] ({total_time:.1f}s)")
                else:
                    self.console.print(f"[bold green]Pipeline complete[/bold green] ({total_time:.1f}s)")
            elif self.output.mode == OutputMode.PLAIN:
                status = "FAILED" if self._pipeline_failed else "COMPLETE"
                print(f"\n[{status}] Pipeline finished in {total_time:.1f}s")

    def get_phase_times(self) -> Dict[str, float]:
        return self._phase_times.copy()


class ProgressCallback:
    """Callback bridge passed into core modules.

    Forwards ``__call__`` to ``ProgressManager.update`` (back-compat) and
    exposes the methods LiveStatusDisplay reaches for: ``pipeline_renderable``,
    ``advance_pipeline_fractional``, and the back-compat no-ops
    ``suspend`` / ``mark_bridged``.
    """

    def __init__(self, manager: ProgressManager):
        self._manager = manager

    def __call__(self, message: str, advance: int = 0) -> None:
        self._manager.update(message, advance)

    def pipeline_renderable(self) -> Optional[RenderableType]:
        return self._manager.pipeline_renderable()

    def advance_pipeline_fractional(self, fractional_steps: float) -> None:
        self._manager.advance_pipeline_fractional(fractional_steps)

    def suspend(self):
        return self._manager.suspend()

    def mark_bridged(self) -> None:
        self._manager.mark_bridged()
