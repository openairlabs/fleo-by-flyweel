"""User note / context threading.

Captures freeform user guidance — interactive per-step notes (Tab on a prompt)
or a single non-interactive ``--note`` — and produces ONE lightweight "adapted
directive" shared by the generation + refine phases. The research phase
consumes only the RAW note (:meth:`NoteBundle.raw_text`), never the adapted
form.

Pure stdlib. ``core/`` has no ``__init__.py``; this is imported relatively
(``from .user_note import ...`` in core, ``from ..user_note import ...`` in
core/cli).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# One tag per content-affecting interactive prompt, plus the non-interactive
# global bucket.
STEP_KEYWORD = "keyword"
STEP_STYLE = "style"
STEP_RESEARCH = "research-options"
STEP_FLYWEEL = "flyweel-mode"
STEP_ICP = "icp"
STEP_GLOBAL = "global"

# Notes at/under this many words skip the LLM and are used verbatim.
_TRIVIAL_WORD_LIMIT = 8


@dataclass
class NoteBundle:
    """Raw user notes plus a cache for the single adapted directive.

    - Non-interactive: ``global_note`` set once via :meth:`from_global`.
    - Interactive: ``step_notes`` accumulates ``{step: note}`` as the user
      Tabs a note onto each prompt.
    """

    global_note: Optional[str] = None
    step_notes: Dict[str, str] = field(default_factory=dict)
    _adapted: Optional[str] = field(default=None, repr=False, compare=False)

    @classmethod
    def from_global(cls, note: Optional[str]) -> "NoteBundle":
        """Build a bundle from the non-interactive ``--note`` value."""
        cleaned = note.strip() if isinstance(note, str) else None
        return cls(global_note=cleaned or None)

    def add_step_note(self, step: str, note: Optional[str]) -> None:
        """Record a per-step note. No-op for blank/whitespace input."""
        if not step or not isinstance(note, str):
            return
        cleaned = note.strip()
        if cleaned and self.step_notes.get(step) != cleaned:
            self.step_notes[step] = cleaned
            self._adapted = None

    def is_empty(self) -> bool:
        return not (self.global_note or self.step_notes)

    def raw_text(self) -> str:
        """RAW combined note for research query biasing — no transform.

        Global note is returned verbatim; interactive notes are joined with
        their originating-step tag so research keeps the origin context.
        """
        if self.global_note:
            return self.global_note
        if not self.step_notes:
            return ""
        return "\n".join(f"[{step}] {txt}" for step, txt in self.step_notes.items())


async def adapt_note(
    ai: Any,
    bundle: Optional[NoteBundle],
    *,
    keyword: str,
    style: str,
) -> str:
    """Produce ONE adapted directive (bare text) shared by generation + refine.

    Idempotent — the result is cached on ``bundle._adapted`` so generation,
    edit and polish reuse the same string with at most one LLM call. The
    research phase must NOT use this; it consumes ``bundle.raw_text()``.

    Returns ``""`` when there is no note (downstream injection is a no-op).
    Never raises and never blocks the pipeline: any LLM failure falls back to
    the raw note text.
    """
    if bundle is None or bundle.is_empty():
        return ""
    if bundle._adapted is not None:
        return bundle._adapted

    raw = bundle.raw_text().strip()
    if not raw:
        bundle._adapted = ""
        return ""

    # Trivial note → use verbatim, no LLM call.
    if len(raw.split()) <= _TRIVIAL_WORD_LIMIT:
        bundle._adapted = raw
        return raw

    system = (
        "You convert a user's freeform note into ONE short imperative "
        "directive (max 2 sentences) that steers a blog-content draft. "
        "Do NOT answer the note, do NOT invent facts or statistics, do NOT "
        "mention being an AI. Output ONLY the directive text."
    )
    user = (
        f"Article keyword: {keyword}\n"
        f"Content style: {style}\n"
        f"User note:\n{raw}\n\n"
        "Rewrite as one actionable directive for the writer/editor:"
    )

    try:
        result = await ai._nebius_with_fallback(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=220,
            purpose="note-adapt",
            reasoning_enabled=False,
        )
    except Exception as e:  # never block the pipeline on note adaptation
        logger.debug(f"adapt_note LLM call failed ({e}); using raw note")
        result = None

    bundle._adapted = result.strip() if (result and result.strip()) else raw
    return bundle._adapted
