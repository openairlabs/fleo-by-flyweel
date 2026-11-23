"""Tab-to-note keybinding wrapper for questionary prompts.

Press **Tab** on a content-affecting prompt to attach a freeform note; the
note is recorded against the originating step and the chosen option is
submitted normally on **Enter**.

This relies on questionary's internal prompt_toolkit Application
(``Question.application`` / ``.key_bindings``) — NOT public API. The exact
versions are pinned in ``requirements.txt``. Any attribute drift or runtime
error degrades silently to a plain prompt (note skipped, never crashes).
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Optional

from ..user_note import NoteBundle

logger = logging.getLogger(__name__)


def _tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def ask_with_note(question: Any, step: str, *, bundle: Optional[NoteBundle]) -> Any:
    """Run ``question`` with a Tab-activated note buffer.

    Returns exactly what ``question.ask()`` would (the answer only). Any
    captured note is written via ``bundle.add_step_note(step, ...)``.
    """
    if bundle is None or not _tty():
        return question.ask()

    state = {"note": ""}
    try:
        from prompt_toolkit.application import run_in_terminal
        from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings

        app = getattr(question, "application", None)
        existing = getattr(app, "key_bindings", None)
        if app is None or existing is None:
            raise AttributeError("questionary Application/key_bindings unavailable")

        kb = KeyBindings()

        @kb.add("tab", eager=True)
        def _(event):  # noqa: ANN001 - prompt_toolkit handler signature
            def _capture() -> str:
                try:
                    return input("  \U0001F4DD note (Enter saves, blank skips) ▸ ")
                except (EOFError, KeyboardInterrupt):
                    return ""

            fut = run_in_terminal(_capture)

            def _done(f):
                try:
                    val = f.result()
                except Exception:
                    val = ""
                if val and val.strip():
                    state["note"] = val.strip()

            fut.add_done_callback(_done)

        # Merge (do not mutate) so questionary's own bindings stay intact.
        app.key_bindings = merge_key_bindings([existing, kb])
    except Exception as e:
        logger.debug(
            f"ask_with_note fallback ({type(e).__name__}: {e}); note capture skipped"
        )
        return question.ask()

    # Setup succeeded — ask normally; let KeyboardInterrupt propagate to the
    # caller exactly as plain questionary would.
    answer = question.ask()
    if state["note"]:
        bundle.add_step_note(step, state["note"])
    return answer
