"""Post-generation content scrubber.

Catches imperative meta-directives and template artifacts that leaked from
prompts into the final article body. Acts as a defense-in-depth layer behind
the prompt cleanups in `core/generator.py` and the FAQ extractor in
`core/faq_builder.py` (see audit findings F3 and F8).

Behavior:
- `scrub(mdx, strict=False)` returns a `ScrubResult` with the cleaned text
  and a list of `Violation` records.
- In strict mode (CI / dev), the caller can fail the build if violations is
  non-empty.
- In lenient mode (default), violating lines are stripped and the build
  continues with a warning logged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


# Each pattern matches a single line of suspected leakage. Patterns are
# whole-line (anchored to `^` ... `$` with re.MULTILINE) so we can strip the
# offending line cleanly without mangling surrounding content.
_LEAK_PATTERNS = [
    # Meta-directives that should never have left the prompt template.
    (re.compile(r"^.*INCLUDE THIS\s*-\s*CRITICAL.*$", re.MULTILINE | re.IGNORECASE),
     "guide-meta-directive"),
    (re.compile(r"^.*HIGHLY DETAILED\s*-\s*EDUCATIONAL.*$", re.MULTILINE | re.IGNORECASE),
     "guide-meta-directive"),

    # "Manual Way" template artifacts.
    (re.compile(r"^\s*Mine Community Forums:.*$", re.MULTILINE),
     "manual-template-bullet"),
    (re.compile(r"^\s*\*\*TOTAL MANUAL TIME INVESTMENT[^\n]*$", re.MULTILINE),
     "manual-template-bullet"),
    (re.compile(r"^\s*\*\*EXPERTISE REQUIRED:[^\n]*$", re.MULTILINE),
     "manual-template-bullet"),
    (re.compile(r"^\s*\*\*RISK LEVEL:[^\n]*$", re.MULTILINE),
     "manual-template-bullet"),
    (re.compile(r"^\s*\*\*SCALABILITY:[^\n]*$", re.MULTILINE),
     "manual-template-bullet"),
    (re.compile(r"^\s*\*\*HIDDEN COSTS:[^\n]*$", re.MULTILINE),
     "manual-template-bullet"),

    # 3-part / section-edit generation scaffolding echoed by the model
    # (leaks from ai_router._edit_section / _polish_section prompts).
    (re.compile(r"^\s*SECTION\s+\d+\s+of\s+\d+:?\s*$", re.MULTILINE | re.IGNORECASE),
     "section-marker"),
    (re.compile(r"^\s*PART\s+\d+\s+of\s+\d+:?\s*$", re.MULTILINE | re.IGNORECASE),
     "section-marker"),
    (re.compile(r"^\s*GENERATION SCOPE FOR THIS PART\b.*$", re.MULTILINE | re.IGNORECASE),
     "generation-scope-directive"),

    # Leaked research / product-fact verification notes.
    (re.compile(
        r"^[^\n]*\b(?:stated product facts|company facts|provided research|"
        r"supplied facts|verification notes|research context)\s+"
        r"(?:do not|does not|did not|cannot)\s+"
        r"(?:confirm|state|verify|mention|specify)[^\n]*$",
        re.MULTILINE | re.IGNORECASE,
    ), "leaked-research-note"),
    (re.compile(
        r"^[^\n]*\b(?:not confirmed in the (?:stated|provided) (?:product|company) facts|"
        r"according to the provided company facts)[^\n]*$",
        re.MULTILINE | re.IGNORECASE,
    ), "leaked-research-note"),
]


# Meta-bullet *labels* that leaked from the Manual Way template. Unlike the
# patterns above, these bullets usually carry genuine reader advice after the
# label ("**Pro hack:** Create a master folder structure."). Per the standing
# rule — scrub in place, never delete-and-rewrite real content — we strip only
# the leaked label and keep the sentence. The bullet is deleted outright only
# when nothing but the label was present. Group 1 is the bullet marker, group 2
# is the surviving content.
_NEUTRALIZE_PATTERNS = [
    (re.compile(
        r"^(\s*[-*]\s*)\*\*(?:Pro hack|Common issue|Time-saving tip|"
        r"Reality check|Excel hack|Watch out):\*\*\s*(.*)$",
        re.MULTILINE | re.IGNORECASE),
     "instruction-meta-bullet"),
]

_MAX_REMOVAL_RATIO = 0.10


def _repair_orphaned_markdown(body: str) -> str:
    """Repair orphaned bold/bullet stubs outside fenced code blocks and inline code spans."""
    in_fence = False
    fence_char = ""
    fence_len = 0
    out_lines: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()
        m = re.match(r"^(`{3,}|~{3,})(.*)$", stripped)
        if m:
            run = m.group(1)
            if not in_fence and (run[0] != "`" or "`" not in m.group(2)):
                in_fence = True
                fence_char = run[0]
                fence_len = len(run)
                out_lines.append(line)
                continue
            if in_fence and run[0] == fence_char and len(run) >= fence_len and not m.group(2).strip():
                in_fence = False
                out_lines.append(line)
                continue
        if in_fence:
            out_lines.append(line)
            continue
        # Protect inline code spans on prose lines
        parts = re.split(r"(`[^`]+`)", line)
        for idx in range(0, len(parts), 2):
            parts[idx] = re.sub(r"\*\*\s*\*\*", "", parts[idx])
        rejoined = "".join(parts)
        if re.match(r"^\s*\*\*\s*$", rejoined):
            continue
        if re.match(r"^\s*[-*]\s*$", rejoined):
            continue
        if re.match(r"(?i)^\s*(?:the|a|an)\s*\.\s*$", rejoined):
            continue
        out_lines.append(rejoined)
    return "\n".join(out_lines)


# A redundant parallel citation style ("[Source Name, 2026]") the research
# prompt over-produces. The canonical system is the numbered
# <a href="#cite-N" class="citation-ref">[N]</a> refs + the Sources section,
# so these bracketed tags are litter. The numeric refs ([8]) never match this
# pattern because they lack the `, YYYY` component.
_SOURCE_TAG = re.compile(
    r"\*{0,2}\[\s*([^\[\]\n]{1,80}?),\s*((?:19|20)\d{2})\s*\]\*{0,2}"
)
_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")


def _consolidate_source_tags(body: str) -> tuple[str, int]:
    """Strip/dedupe redundant ``[Name, Year]`` source tags.

    - Inside markdown table rows: remove them entirely (table cells carry
      data only, matching the site's blog formatting).
    - In prose: keep the first mention of a given source per paragraph and
      drop later duplicates, so claims stay attributed but the litter goes.
    Fenced code blocks and the numbered ``[N]`` citation refs are untouched.
    """
    removed = 0
    in_fence = False
    seen: set[str] = set()
    out_lines: list[str] = []

    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        if not stripped:
            seen.clear()  # paragraph boundary
            out_lines.append(line)
            continue

        if _TABLE_ROW.match(line):
            new_line, n = _SOURCE_TAG.subn("", line)
            if n:
                removed += n
                new_line = re.sub(r"(?<=\S)[ \t]{2,}", " ", new_line)
                new_line = re.sub(r"\|\s+\|", "| |", new_line)
            out_lines.append(new_line)
            continue

        removed_on_line = 0

        def _dedupe(m: "re.Match") -> str:
            nonlocal removed, removed_on_line
            norm_source = re.sub(r"\s+", " ", m.group(1).strip().lower())
            key = f"{norm_source},{m.group(2)}"
            if key in seen:
                removed += 1
                removed_on_line += 1
                return ""
            seen.add(key)
            return m.group(0)

        line = _SOURCE_TAG.sub(_dedupe, line)
        if removed_on_line:
            # Tidy artefacts left by removed inline tags.
            line = re.sub(r"(?<=\S)[ \t]{2,}", " ", line)
            line = re.sub(r"\s+([.,;:])", r"\1", line)
        else:
            # Collapse only interior double spaces while preserving trailing Markdown hard-break spaces.
            line = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", line)
        out_lines.append(line)

    return "\n".join(out_lines), removed


_UK_TO_US_WORDS = (
    (re.compile(r"\boptimis(e|ed|es|ing|ation|ations)\b", re.IGNORECASE), "optimiz"),
    (re.compile(r"\borganis(e|ed|es|ing|ation|ations)\b", re.IGNORECASE), "organiz"),
    (re.compile(r"\banalys(e|ed|ing)\b", re.IGNORECASE), "analyz"),
    (re.compile(r"\brecognis(e|ed|es|ing)\b", re.IGNORECASE), "recogniz"),
    (re.compile(r"\bstandardis(e|ed|es|ing)\b", re.IGNORECASE), "standardiz"),
    (re.compile(r"\bprioritis(e|ed|es|ing)\b", re.IGNORECASE), "prioritiz"),
    (re.compile(r"\bbehaviour(s|al)?\b", re.IGNORECASE), "behavior"),
    (re.compile(r"\bprogramme(s)?\b", re.IGNORECASE), "program"),
    (re.compile(r"\bcentr(ed|ing)\b", re.IGNORECASE), "center"),
    (re.compile(r"\bcentre(s)?\b", re.IGNORECASE), "center"),
    (re.compile(r"\bmodelling\b", re.IGNORECASE), "modeling"),
    (re.compile(r"\blabelling\b", re.IGNORECASE), "labeling"),
    (re.compile(r"\btravelling\b", re.IGNORECASE), "traveling"),
    (re.compile(r"\bcancelled\b", re.IGNORECASE), "canceled"),
    (re.compile(r"\bfulfil\b", re.IGNORECASE), "fulfill"),
)


def _normalize_us_spelling_and_dashes(text: str) -> str:
    """Normalize em/en dashes and UK spelling outside fenced code blocks and URLs."""
    in_fence = False
    out_lines: list[str] = []
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        # Protect markdown/HTML URLs BEFORE dash or UK -> US spelling normalization.
        urls: list[str] = []

        def _stash_url(m: "re.Match") -> str:
            urls.append(m.group(0))
            return f"__URL_SLOT_{len(urls) - 1}__"

        line = re.sub(r"(?:https?://[^\s\)\"'>]+|\(/[^\s\)]+\)|(?:href|src)=[\"'][^\"']+[\"'])", _stash_url, line)

        # Replace em/en dashes with hyphens (space-padded em dash becomes ' - ').
        line = re.sub(r"\s+—\s+", " - ", line)
        line = line.replace("—", "-").replace("–", "-")

        for pat, stem in _UK_TO_US_WORDS:
            def _repl(m: "re.Match", _stem: str = stem) -> str:
                word = m.group(0)
                suffix = m.group(1) if m.lastindex else ""
                rep = _stem + (suffix or "")
                if word.isupper():
                    return rep.upper()
                if word[0].isupper():
                    return rep[0].upper() + rep[1:]
                return rep
            line = pat.sub(_repl, line)
        for idx, u in enumerate(urls):
            line = line.replace(f"__URL_SLOT_{idx}__", u)
        out_lines.append(line)
    return "\n".join(out_lines)


@dataclass
class Violation:
    pattern_name: str
    line: str
    line_number: int


@dataclass
class ScrubResult:
    cleaned: str
    violations: List[Violation] = field(default_factory=list)
    removed_ratio: float = 0.0
    over_deletion: bool = False

    @property
    def strict_fail(self) -> bool:
        return bool(self.violations) or self.over_deletion


def _stash_code_regions(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace fenced code blocks and inline code spans with 1-to-1 line/inline placeholders."""
    stashed: list[tuple[str, str]] = []
    lines = body.splitlines(keepends=True)
    out: list[str] = []
    idx = 0

    while idx < len(lines):
        m = re.match(r"^\s*(`{3,}|~{3,})([^\n]*)$", lines[idx])
        if m:
            run = m.group(1)
            char = run[0]
            info = m.group(2)
            if char != "`" or "`" not in info:
                close_re = re.compile(rf"^\s*{re.escape(char)}{{{len(run)},}}\s*$")
                end_idx = None
                for j in range(idx + 1, len(lines)):
                    if close_re.match(lines[j]):
                        end_idx = j
                        break
                if end_idx is not None:
                    for k in range(idx, end_idx + 1):
                        raw_ln = lines[k]
                        has_nl = raw_ln.endswith("\n")
                        core_ln = raw_ln[:-1] if has_nl else raw_ln
                        token = f"__FLEO_CODE_LINE_{len(stashed)}__"
                        stashed.append((token, core_ln))
                        out.append(token + ("\n" if has_nl else ""))
                    idx = end_idx + 1
                    continue

        def _stash_inline(im: "re.Match") -> str:
            token = f"__FLEO_INLINE_CODE_{len(stashed)}__"
            stashed.append((token, im.group(0)))
            return token

        out.append(re.sub(r"(`+)([^`\n]*?)\1", _stash_inline, lines[idx]))
        idx += 1

    return "".join(out), stashed


def _restore_code_regions(body: str, stashed: list[tuple[str, str]]) -> str:
    restored = body
    for token, original in reversed(stashed):
        restored = restored.replace(token, original)
    return restored


def scrub(mdx: str, *, strict: bool = False) -> ScrubResult:
    """Strip known instruction leaks. Returns cleaned text and violations."""
    if not mdx:
        return ScrubResult(cleaned=mdx or "", violations=[])

    violations: List[Violation] = []
    cleaned = mdx

    # Consolidate redundant [Name, Year] source tags and run all cleanup passes
    # on the body ONLY so YAML frontmatter values and URLs are never mutated.
    fm_match = re.match(r"^(---\n.*?\n---\n)(.*)$", cleaned, re.DOTALL)
    if fm_match:
        head, body = fm_match.group(1), fm_match.group(2)
    else:
        head, body = "", cleaned
    head_lines = head.count("\n")
    body, stashed_code = _stash_code_regions(body)

    body, n_src = _consolidate_source_tags(body)
    if n_src:
        logger.log(
            logging.ERROR if strict else logging.WARNING,
            "Content scrubber consolidated %d redundant [Name, Year] source "
            "tag(s); attribution flows through the numbered citation system.",
            n_src,
        )

    body_len_before_lines = len(_restore_code_regions(body, stashed_code))

    # Pass 1 — neutralize content-bearing meta-bullets in place. Strip the
    # leaked label but keep the advice; delete the bullet only if it was
    # label-only. This preserves real content instead of nuking the whole line.
    for pattern, name in _NEUTRALIZE_PATTERNS:
        def _neutralize(m: "re.Match") -> str:
            marker, content = m.group(1), (m.group(2) or "").strip()
            line_no = head_lines + body.count("\n", 0, m.start()) + 1
            violations.append(Violation(
                pattern_name=name,
                line=m.group(0).strip(),
                line_number=line_no,
            ))
            # Keep the bullet + surviving content; drop entirely if label-only.
            return f"{marker}{content}" if content else ""
        body = pattern.sub(_neutralize, body)

    # Pass 2 — delete confirmed scaffolding lines (no reader value).
    for pattern, name in _LEAK_PATTERNS:
        matches = list(pattern.finditer(body))
        if not matches:
            continue
        for m in matches:
            line_no = head_lines + body.count("\n", 0, m.start()) + 1
            violations.append(Violation(
                pattern_name=name,
                line=m.group(0).strip(),
                line_number=line_no,
            ))
        body = pattern.sub("", body)

    # Pass 3 — repair orphaned markdown left behind by tag/label removal (outside code blocks/spans).
    body = _repair_orphaned_markdown(body)

    # Pass 4 — US spelling and em/en dash normalization.
    body = _normalize_us_spelling_and_dashes(body)

    body = re.sub(r"\n{3,}", "\n\n", body)
    body = _restore_code_regions(body, stashed_code)
    cleaned = head + body
    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"

    if violations:
        sample = violations[0]
        level = logging.ERROR if strict else logging.WARNING
        logger.log(
            level,
            "Content scrubber stripped %d instruction-leak line(s); first hit "
            "(%s) at line %d: %r",
            len(violations), sample.pattern_name, sample.line_number, sample.line[:120],
        )

    # Content-preservation guard: scaffolding removal should be a trim, not a
    # gutting. If we deleted more than _MAX_REMOVAL_RATIO of the text, flag it
    # loudly and trip strict_fail so a regression is caught, not shipped.
    removed_ratio = 0.0
    if body_len_before_lines:
        removed_ratio = max(0.0, (body_len_before_lines - len(body)) / body_len_before_lines)
    over_deletion = removed_ratio > _MAX_REMOVAL_RATIO
    if over_deletion:
        logger.log(
            logging.ERROR if strict else logging.WARNING,
            "Content scrubber removed %.1f%% of the content (threshold %.0f%%); "
            "possible over-deletion — review scrub patterns before publishing.",
            removed_ratio * 100, _MAX_REMOVAL_RATIO * 100,
        )

    return ScrubResult(
        cleaned=cleaned,
        violations=violations,
        removed_ratio=round(removed_ratio, 4),
        over_deletion=over_deletion,
    )


__all__ = ["scrub", "ScrubResult", "Violation"]
