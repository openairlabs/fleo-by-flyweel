"""Live status display for content generation pipeline.

One animated spinner per panel (in the subtitle, not the rows). The active
row is bold cyan; pending rows are dim. State glyphs in front of each row
(○ pending, ◐ running, ● complete, ⊘ skipped) replace the per-row spinner
zoo. Composes a persistent pipeline progress bar at the bottom of the Live
region via ``as_group(pipeline_bar)``.
"""
from typing import Callable, List, Optional
from rich.console import Group, RenderableType
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text
from rich import box


GLYPH_PENDING = "○"
GLYPH_RUNNING = "◐"
GLYPH_DONE = "●"
GLYPH_SKIPPED = "⊘"

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_SKIPPED = "skipped"
STATE_FAILED = "failed"


class _Row:
    """Single task row state."""

    __slots__ = ("name", "state", "detail", "time")

    def __init__(self, name: str):
        self.name = name
        self.state: str = STATE_PENDING
        self.detail: str = "—"
        self.time: str = "—"


class LiveStatusDisplay:
    """Real-time status dashboard for a generation phase."""

    def __init__(self):
        self.rows: List[_Row] = []
        self.current_phase: Optional[str] = None
        self.current_action: Optional[str] = None
        self._live: Optional[Live] = None
        self._on_step_done: Optional[Callable[[str, int], None]] = None
        self._on_fractional: Optional[Callable[[float], None]] = None
        # name -> last fractional position reported (0..1) so we emit deltas.
        self._fraction_seen: dict = {}
        # The single animated spinner used in the panel subtitle.
        self._spinner = Spinner("dots", style="cyan")
        self._pipeline_bar: Optional[RenderableType] = None
        # Subtitle cycler state — index advances at most once per 1.6 s wall
        # clock instead of being recomputed from monotonic() on every render,
        # so two adjacent frames around a 1.6 s boundary can't flip.
        self._cycle_index: int = 0
        self._cycle_last: float = 0.0

    # Back-compat aliases ─────────────────────────────────────────────────
    @property
    def tasks(self) -> list:
        """Legacy accessor — returns [[name, state, detail, time], ...]."""
        return [[r.name, r.state, r.detail, r.time] for r in self.rows]

    # ─────────────────────────────────────────────────────────────────────
    # Bridge wiring
    # ─────────────────────────────────────────────────────────────────────

    def bind_progress(self, callback: Callable[[str, int], None]) -> None:
        """Wire discrete advance: one step per complete_task / skip_task."""
        self._on_step_done = callback

    def bind_fractional(self, callback: Callable[[float], None]) -> None:
        """Wire fractional advance: emitted on each set_task_progress.

        ``callback`` receives the *delta* the persistent pipeline bar should
        advance — already scaled to pipeline-step units (one subtask = 1
        step), so Reddit query 4 → 5 of 10 emits ``0.1``.
        """
        self._on_fractional = callback

    # ─────────────────────────────────────────────────────────────────────
    # Phase / task setup
    # ─────────────────────────────────────────────────────────────────────

    def set_phase(self, phase_name: str) -> None:
        self.current_phase = phase_name

    def add_task(self, name: str) -> None:
        self.rows.append(_Row(name))

    def _find(self, name: str) -> Optional[_Row]:
        for row in self.rows:
            if row.name == name:
                return row
        return None

    # ─────────────────────────────────────────────────────────────────────
    # Row state transitions
    # ─────────────────────────────────────────────────────────────────────

    def update_task(
        self,
        name: str,
        status: str,
        detail: str = "",
        time: str = "",
    ) -> None:
        """Back-compat: accept a string status and infer state."""
        state = STATE_RUNNING
        s = status.lower()
        if "complete" in s or "✓" in status or "done" in s:
            state = STATE_DONE
        elif "fail" in s or "error" in s or "✗" in status:
            state = STATE_FAILED
        elif "skip" in s or "⊘" in status:
            state = STATE_SKIPPED
        elif "pend" in s:
            state = STATE_PENDING
        elif "run" in s or "⏳" in status:
            state = STATE_RUNNING
        row = self._find(name)
        if row is None:
            return
        row.state = state
        row.detail = detail or "—"
        row.time = time or "—"
        if state == STATE_RUNNING:
            self.current_action = f"{name} — {detail}" if detail else name
        elif self.current_action and self.current_action.startswith(f"{name} —"):
            self.current_action = None

    def start_task(self, name: str, detail: str = "") -> None:
        row = self._find(name)
        if row is None:
            return
        row.state = STATE_RUNNING
        row.detail = detail or "—"
        row.time = "…"
        self.current_action = f"{name} — {detail}" if detail else name

    def set_task_progress(
        self,
        name: str,
        current: int,
        total: int,
        *,
        insights: Optional[int] = None,
        query: Optional[str] = None,
        note: Optional[str] = None,
        bar_length: int = 12,
    ) -> None:
        """Render an inline ``[████░░] n/m`` bar in a running task's detail."""
        row = self._find(name)
        if row is None:
            return
        if row.state != STATE_RUNNING:
            row.state = STATE_RUNNING

        total_safe = max(total, 1)
        filled = max(0, min(bar_length, int(bar_length * current / total_safe)))
        bar = "█" * filled + "░" * (bar_length - filled)
        detail = f"[{bar}] {current}/{total_safe}"
        if insights is not None:
            detail += f" · {insights} ins"
        if query:
            q = query.strip().strip('"').strip("'")
            if len(q) > 40:
                q = q[:39] + "…"
            detail += f' · "{q}"'
        if note:
            detail += f" · {note}"
        row.detail = detail
        row.time = "…"
        self.current_action = f"{name} — {current}/{total_safe}"

        # Fractional bridge: each subtask occupies 1 pipeline step. Emit only
        # the delta since the last call so the bar inches forward smoothly.
        if self._on_fractional is not None and total_safe > 0:
            new_fraction = max(0.0, min(1.0, current / total_safe))
            last = self._fraction_seen.get(name, 0.0)
            delta = new_fraction - last
            if delta > 0:
                self._fraction_seen[name] = new_fraction
                try:
                    self._on_fractional(delta)
                except Exception:
                    pass

    def complete_task(self, name: str, detail: str, time: str) -> None:
        row = self._find(name)
        if row is None:
            return
        was_terminal = row.state in {STATE_DONE, STATE_SKIPPED, STATE_FAILED}
        row.state = STATE_DONE
        row.detail = detail or "—"
        row.time = time or "—"
        if self.current_action and self.current_action.startswith(f"{name} —"):
            self.current_action = None

        # Reconcile fractional bridge: the discrete bridge adds 1 step on
        # completion; subtract any fractional credit we already emitted so
        # net contribution per subtask is always exactly 1.
        if self._on_fractional is not None:
            already = self._fraction_seen.pop(name, 0.0)
            if already > 0:
                try:
                    self._on_fractional(-already)
                except Exception:
                    pass

        if not was_terminal and self._on_step_done is not None:
            try:
                # Empty message: advance the pipeline bar by 1 step but do NOT
                # rewrite the phase description (which the phase context set).
                self._on_step_done("", 1)
            except Exception:
                pass

    def skip_task(self, name: str, reason: str = "Skipped") -> None:
        row = self._find(name)
        if row is None:
            return
        was_terminal = row.state in {STATE_DONE, STATE_SKIPPED, STATE_FAILED}
        row.state = STATE_SKIPPED
        row.detail = reason or "—"
        row.time = "—"

        if self._on_fractional is not None:
            already = self._fraction_seen.pop(name, 0.0)
            if already > 0:
                try:
                    self._on_fractional(-already)
                except Exception:
                    pass

        if not was_terminal and self._on_step_done is not None:
            try:
                # Empty message: advance the pipeline bar by 1 step but keep
                # the existing phase description.
                self._on_step_done("", 1)
            except Exception:
                pass

    def fail_task(self, name: str, reason: str = "Failed") -> None:
        row = self._find(name)
        if row is None:
            return
        was_terminal = row.state in {STATE_DONE, STATE_SKIPPED, STATE_FAILED}
        row.state = STATE_FAILED
        row.detail = reason or "—"
        row.time = "—"

        if self._on_fractional is not None:
            already = self._fraction_seen.pop(name, 0.0)
            if already > 0:
                try:
                    self._on_fractional(-already)
                except Exception:
                    pass

        if not was_terminal and self._on_step_done is not None:
            try:
                self._on_step_done("", 1)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────
    # Rendering
    # ─────────────────────────────────────────────────────────────────────

    def _row_glyph(self, row: _Row) -> Text:
        if row.state == STATE_PENDING:
            return Text(GLYPH_PENDING, style="dim")
        if row.state == STATE_RUNNING:
            return Text(GLYPH_RUNNING, style="bold cyan")
        if row.state == STATE_DONE:
            return Text(GLYPH_DONE, style="green")
        if row.state == STATE_SKIPPED:
            return Text(GLYPH_SKIPPED, style="yellow")
        if row.state == STATE_FAILED:
            return Text("✗", style="red")
        return Text("·")

    def _row_name(self, row: _Row) -> Text:
        if row.state == STATE_RUNNING:
            return Text(row.name, style="bold cyan")
        if row.state == STATE_PENDING:
            return Text(row.name, style="dim")
        if row.state == STATE_DONE:
            return Text(row.name, style="white")
        if row.state == STATE_FAILED:
            return Text(row.name, style="red")
        return Text(row.name, style="yellow")

    def _row_detail(self, row: _Row) -> Text:
        if row.state == STATE_RUNNING:
            return Text(row.detail, style="cyan")
        if row.state == STATE_FAILED:
            return Text(row.detail, style="red")
        return Text(row.detail, style="dim")

    def generate_table(self) -> Panel:
        table = Table(
            show_header=False,
            box=box.SIMPLE,
            padding=(0, 1),
            expand=True,
            collapse_padding=True,
        )
        table.add_column("", width=2)
        table.add_column("Task", width=22)
        table.add_column("Detail", no_wrap=False, overflow="ellipsis")
        table.add_column("Time", width=6, justify="right", style="cyan")

        for row in self.rows:
            table.add_row(
                self._row_glyph(row),
                self._row_name(row),
                self._row_detail(row),
                row.time,
            )

        title_text = Text(self.current_phase or "Processing", style="bold")

        # Single animated spinner + a header label that *cycles* through all
        # currently-running tasks (so parallel work like Reddit + Quora mining
        # is visible in the subtitle, not just the last-touched one). Cycles
        # every ~1.6s on a wall-clock basis so it's stable across re-renders.
        running_rows = [r for r in self.rows if r.state == STATE_RUNNING]
        if running_rows:
            if len(running_rows) == 1:
                active = running_rows[0]
                self._cycle_index = 0
            else:
                import time
                now = time.monotonic()
                if now - self._cycle_last >= 1.6:
                    self._cycle_index = (self._cycle_index + 1) % len(running_rows)
                    self._cycle_last = now
                active = running_rows[self._cycle_index % len(running_rows)]
            # Compose label: "Reddit Mining — 5/10 · 23 ins  (also: Quora Extraction)"
            # when there are siblings, so the user sees that other work is
            # running in parallel without losing the focal task.
            primary = f"{active.name} — {active.detail}"
            siblings = [r.name for r in running_rows if r is not active]
            if siblings:
                primary += f"   [also: {', '.join(siblings)}]"
            # Pin width to 80 so a longer label can't wrap into a second row
            # and push the pipeline bar below it down by a line.
            if len(primary) > 80:
                primary = primary[:79] + "…"
            else:
                primary = primary.ljust(80)
            header = Table.grid(padding=(0, 1))
            header.add_column(width=2)
            header.add_column()
            header.add_row(
                self._spinner,
                Text(primary, style="dim cyan", no_wrap=True),
            )
            body: RenderableType = Group(header, table)
        else:
            body = table

        return Panel(
            body,
            title=title_text,
            border_style="blue",
            expand=True,
            padding=(0, 1),
        )

    def as_group(self, pipeline_bar: Optional[RenderableType] = None) -> Group:
        """Compose phase panel above the persistent pipeline bar.

        Returns a Rich ``Group`` suitable for a single ``Live`` region.
        """
        parts: List[RenderableType] = [self.generate_table()]
        if pipeline_bar is not None:
            parts.append(pipeline_bar)
        return Group(*parts)

    # ─────────────────────────────────────────────────────────────────────
    # Live lifecycle
    # ─────────────────────────────────────────────────────────────────────

    def start_live(self, refresh_per_second: int = 2) -> Live:
        """Start a Live region rendering just the phase panel.

        Prefer ``start_live_grouped(pipeline_bar)`` so the persistent
        pipeline bar is visible at the bottom of the region.
        """
        from .console import CONSOLE
        self._pipeline_bar = None
        self._live = Live(
            self.generate_table(),
            console=CONSOLE,
            refresh_per_second=refresh_per_second,
            transient=True,
        )
        return self._live

    def start_live_grouped(
        self,
        pipeline_bar: Optional[RenderableType] = None,
        refresh_per_second: int = 2,
    ) -> Live:
        """Start a Live region rendering panel + persistent pipeline bar."""
        from .console import CONSOLE
        self._pipeline_bar = pipeline_bar
        self._live = Live(
            self.as_group(pipeline_bar),
            console=CONSOLE,
            refresh_per_second=refresh_per_second,
            transient=True,
        )
        return self._live

    def refresh(self) -> None:
        """Swap in the latest renderable.

        ``Live(renderable, auto_refresh=True)`` captures the renderable at
        construction time — row-state changes (e.g. ``start_task`` flipping
        ○ → ◐) only become visible after ``Live.update()`` is called with a
        freshly-generated renderable. We pass ``refresh=False`` so the new
        renderable is installed but the actual redraw waits for the next
        auto-refresh tick (≤125 ms at 8 Hz). This eliminates the double-
        render flicker on the pipeline bar while keeping the rows live.

        When ``auto_refresh=False`` we trigger an immediate redraw, since
        nobody else will.
        """
        if not (self._live and self._live.is_started):
            return
        new_renderable = (
            self.as_group(self._pipeline_bar)
            if self._pipeline_bar is not None
            else self.generate_table()
        )
        do_refresh = not getattr(self._live, "auto_refresh", False)
        try:
            self._live.update(new_renderable, refresh=do_refresh)
        except TypeError:
            # Older Rich versions: ``refresh`` kwarg unsupported.
            self._live.update(new_renderable)

    def clear_tasks(self) -> None:
        self.rows = []
        self._fraction_seen = {}


# ─────────────────────────────────────────────────────────────────────────
# Pre-wired phase displays
# ─────────────────────────────────────────────────────────────────────────

def create_research_display() -> LiveStatusDisplay:
    display = LiveStatusDisplay()
    display.set_phase("Research")
    display.add_task("SERP Analysis")
    display.add_task("Reddit Mining")
    display.add_task("Quora Extraction")
    display.add_task("Quality Filter")
    return display


def create_generation_display() -> LiveStatusDisplay:
    display = LiveStatusDisplay()
    display.set_phase("Draft")
    display.add_task("Part 1/3")
    display.add_task("Part 2/3")
    display.add_task("Part 3/3")
    return display


def create_polish_display() -> LiveStatusDisplay:
    display = LiveStatusDisplay()
    display.set_phase("Refinement")
    display.add_task("Edit Pass")
    display.add_task("Citation Verify")
    display.add_task("Citation Fixes")
    display.add_task("Polish Pass")
    display.add_task("Heading SEO")
    display.add_task("Title SEO")
    return display
