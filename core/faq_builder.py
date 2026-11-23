"""Unified FAQ builder.

Single source of truth for FAQ questions and answers. The same FAQEntry list
feeds the frontmatter `faq:` array, the body `## Frequently Asked Questions`
section, and the FAQPage JSON-LD schema, so all three stay in sync.

Design rules (see audit plan F1/F2):
- Paragraph scoring rejects headings, frontmatter lines, imports, components,
  bullet/numbered list blocks, and paragraphs starting with imperative verbs
  (those usually indicate prompt directives that leaked into the body).
- A candidate paragraph must contain >=2 complete sentences and >=100 chars.
- When no candidate scores above `min_confidence`, the question is dropped
  rather than filled with boilerplate. Truncated FAQ lists are acceptable;
  five questions sharing identical generic text is not.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


IMPERATIVE_VERB_DENYLIST = frozenset({
    "mine", "search", "quote", "log", "navigate", "export", "download",
    "open", "standardize", "normalize", "remove", "create", "build",
    "manually", "upload", "wait", "field", "copy", "write", "email",
    "identify", "pause", "adjust", "document", "filter",
})

_QUESTION_STOPWORDS = frozenset({
    "what", "how", "why", "when", "where", "which", "is", "are", "the",
    "a", "an", "do", "does", "can", "will", "should", "to", "of", "for",
    "in", "on", "at", "by", "with", "from", "and", "or", "but", "i",
    "my", "your", "you", "we", "our", "us", "be", "been", "being",
    "have", "has", "had", "this", "that", "these", "those",
})

_SENTENCE_TERMINATOR = re.compile(r"[.!?](?:\s|$)")
_HEADING_LINE = re.compile(r"^#{1,6}\s")
_LIST_LINE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
_TABLE_LINE = re.compile(r"^\s*\|")

_FRONTMATTER_PREFIXES = (
    "title:", "description:", "publishDate:", "author:",
    "tags:", "category:", "categories:", "image:", "canonical:",
    "readingTime:", "seo:", "faq:", "jsonld:",
)


@dataclass
class FAQEntry:
    question: str
    short_answer: str
    long_answer: str
    confidence: float = 0.0

    def as_frontmatter(self) -> dict:
        return {"question": self.question, "answer": self.short_answer}


def _split_paragraphs(content: str) -> List[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]


def _paragraph_is_eligible(para: str) -> bool:
    if len(para) < 100:
        return False
    first_line = para.split("\n", 1)[0].strip()
    if not first_line:
        return False
    if _HEADING_LINE.match(first_line):
        return False
    if first_line.startswith("---") or first_line.startswith("```"):
        return False
    if first_line.startswith("import ") or first_line.startswith("<"):
        return False
    if any(first_line.startswith(prefix) for prefix in _FRONTMATTER_PREFIXES):
        return False
    lines = [ln for ln in para.split("\n") if ln.strip()]
    if lines and sum(1 for ln in lines if _LIST_LINE.match(ln) or _TABLE_LINE.match(ln)) >= max(1, len(lines) // 2):
        return False
    first_token = re.split(r"[\s,.:;]+", first_line.lstrip("*_-> "), maxsplit=1)
    if first_token and first_token[0].lower() in IMPERATIVE_VERB_DENYLIST:
        return False
    if len(_SENTENCE_TERMINATOR.findall(para)) < 2:
        return False
    return True


def _question_tokens(question: str) -> set:
    tokens = re.findall(r"\w+", question.lower())
    return {t for t in tokens if t not in _QUESTION_STOPWORDS and len(t) > 2}


def _discriminating_tokens(q_tokens: set, keyword: str) -> set:
    """Drop the article keyword's words: every body paragraph shares them, so they
    can't tell one answer from another. Falls back to all tokens for a question
    made only of keyword words ("What is ad spend financing?")."""
    rest = q_tokens - _question_tokens(keyword or "")
    return rest or q_tokens


def _score_paragraph(para: str, q_tokens: set) -> float:
    para_tokens = set(re.findall(r"\w+", para.lower()))
    overlap = len(q_tokens & para_tokens)
    if overlap == 0:
        return 0.0
    quality = 0.0
    if "**" in para:
        quality += 0.5
    if "for example" in para.lower() or "e.g." in para.lower():
        quality += 1.0
    return (overlap * 3.0) + quality


def _truncate_to_sentences(text: str, max_words: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    if len(words) <= max_words:
        return text
    # Find character cut after max_words, expanding past any Markdown link/inline-code span it lands inside
    cut_pos = len(" ".join(words[:max_words]))
    protected_spans = [
        (m.start(), m.end())
        for m in re.finditer(r"!?\[[^\]]*\]\([^)]*\)|`[^`]*`|<[^>]+>", text)
    ]
    for s_start, s_end in protected_spans:
        if s_start < cut_pos < s_end:
            cut_pos = s_end
            break
    clipped = text[:cut_pos]
    masked_clip = list(clipped)
    for s_start, s_end in protected_spans:
        if s_start < len(masked_clip):
            for idx in range(s_start, min(s_end, len(masked_clip))):
                masked_clip[idx] = "x"
    masked_str = "".join(masked_clip)
    term_matches = list(re.finditer(r"[.!?](?=\s|$)", masked_str))
    if term_matches:
        return clipped[: term_matches[-1].end()]
    # Avoid slicing mid-link when falling back to last space
    safe_space = masked_str.rfind(" ")
    if safe_space > 0:
        return clipped[:safe_space].rstrip(",;:") + "."
    return clipped.rstrip(",;:") + "."


def _strip_markdown_for_short_answer(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"\*\*([^\*]+)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_question(q: str) -> str:
    q = (q or "").strip()
    if not q:
        return ""
    if not q.endswith("?"):
        q += "?"
    return q


def build_faq_entries(
    questions: Iterable[str],
    content: str,
    *,
    min_confidence: float = 3.0,
    max_entries: int = 15,
    keyword: str = "",
    short_answer_max_words: int = 60,
    long_answer_max_words: int = 90,
    post_process_long: Optional[Callable[[str], str]] = None,
) -> List[FAQEntry]:
    """Build a deduplicated FAQ list whose answers come from the body content."""
    paragraphs = [p for p in _split_paragraphs(content) if _paragraph_is_eligible(p)]
    if not paragraphs:
        logger.warning("FAQ builder: no eligible body paragraphs found; FAQ list will be empty")
        return []

    entries: List[FAQEntry] = []
    used_paragraphs: set = set()
    seen_questions: set = set()

    for raw_q in questions:
        question = _normalize_question(raw_q)
        if not question:
            continue
        key = re.sub(r"\W+", " ", question.lower()).strip()
        if key in seen_questions:
            continue
        seen_questions.add(key)

        q_tokens = _discriminating_tokens(_question_tokens(question), keyword)
        if not q_tokens:
            continue

        best_idx = -1
        best_score = 0.0
        for idx, para in enumerate(paragraphs):
            if idx in used_paragraphs:
                continue
            score = _score_paragraph(para, q_tokens)
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx < 0 or best_score < min_confidence:
            logger.debug(
                "FAQ builder: dropping question (score=%.2f < %.2f): %s",
                best_score, min_confidence, question,
            )
            continue

        used_paragraphs.add(best_idx)
        para = paragraphs[best_idx]
        long_md = para.strip()
        if post_process_long is not None:
            try:
                long_md = post_process_long(long_md)
            except Exception:  # noqa: BLE001
                logger.debug("FAQ builder: post_process_long hook raised; using raw paragraph")
        long_md = _truncate_to_sentences(long_md, long_answer_max_words)
        short = _truncate_to_sentences(_strip_markdown_for_short_answer(para), short_answer_max_words)

        entries.append(FAQEntry(
            question=question,
            short_answer=short,
            long_answer=long_md,
            confidence=best_score,
        ))

        if len(entries) >= max_entries:
            break

    return entries


_H2_LINE = re.compile(r"^## .*$", re.MULTILINE)
_H3_QUESTION = re.compile(r"^### (.+\?)\s*$", re.MULTILINE)
_H3_ANY = re.compile(r"^### (.+?)\s*$", re.MULTILINE)
_FAQ_H2_HEADING = re.compile(
    r"^##\s+(?:Frequently Asked Questions|FAQ|Your Top Questions[^\n]*|Common Questions[^\n]*|Questions\s*(?:&|and)\s*Answers)\s*$",
    re.IGNORECASE,
)


def _bound_faq_section_end(content: str, start: int, end: int) -> int:
    """Stop FAQ section spans before <CitationsSection> or a trailing CTA Button."""
    cite_pos = content.find("<CitationsSection", start, end)
    if cite_pos != -1:
        end = cite_pos
    btn_m = re.search(
        r'\n*(?:<div class="not-prose[^"]*">\s*)?<Button\b[^>]*>.*?</Button>(?:\s*</div>)?',
        content[start:end],
        re.DOTALL,
    )
    if btn_m:
        end = start + btn_m.start()
    return end


def _mask_fenced_code_blocks(content: str) -> str:
    """Replace fenced code block interiors with same-length spaces so character offsets stay exact."""
    chars = list(content)
    lines = content.splitlines(keepends=True)
    pos = 0
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in lines:
        m = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if m:
            run = m.group(1)
            if not in_fence and (run[0] != "`" or "`" not in m.group(2)):
                in_fence = True
                fence_char = run[0]
                fence_len = len(run)
                for k in range(pos, pos + len(line)):
                    if chars[k] != "\n":
                        chars[k] = " "
                pos += len(line)
                continue
            if in_fence and run[0] == fence_char and len(run) >= fence_len and not m.group(2).strip():
                in_fence = False
                for k in range(pos, pos + len(line)):
                    if chars[k] != "\n":
                        chars[k] = " "
                pos += len(line)
                continue
        if in_fence:
            for k in range(pos, pos + len(line)):
                if chars[k] != "\n":
                    chars[k] = " "
        pos += len(line)
    return "".join(chars)


def find_faq_section(content: str, *, explicit_only: bool = False) -> Optional[tuple]:
    """(start, end) of the body FAQ: the H2 section holding >=2 `### ...?` questions,
    or an explicit FAQ heading (`## FAQ`, `## Frequently Asked Questions`, etc.).

    Bounded before `<CitationsSection>` so harvesting/removal never touches citations.
    """
    masked = _mask_fenced_code_blocks(content)
    h2_matches = list(_H2_LINE.finditer(masked))
    h2s = [m.start() for m in h2_matches] + [len(content)]
    for m_h2, start, raw_end in zip(h2_matches, h2s, h2s[1:]):
        end = _bound_faq_section_end(masked, start, raw_end)
        is_explicit = bool(_FAQ_H2_HEADING.match(m_h2.group(0).strip()))
        q_count = len(_H3_QUESTION.findall(masked, start, end))
        if is_explicit or (not explicit_only and q_count >= 2):
            return start, end
    return None


def extract_body_faq(
    content: str,
    short_answer_max_words: int = 60,
    *,
    explicit_only: bool = False,
) -> List[FAQEntry]:
    """FAQ entries read straight from the body FAQ section (question -> the text under it)."""
    span = find_faq_section(content, explicit_only=explicit_only)
    if not span:
        return []
    section = content[span[0]:span[1]]
    matches = list(_H3_QUESTION.finditer(section))
    if not matches:
        first_line = section.splitlines()[0].strip() if section.splitlines() else ""
        if _FAQ_H2_HEADING.match(first_line):
            matches = list(_H3_ANY.finditer(section))
    entries = []
    for m, nxt in zip(matches, matches[1:] + [None]):
        body = section[m.end(): nxt.start() if nxt else len(section)]
        body = re.split(r"^#{1,6}\s", body, maxsplit=1, flags=re.MULTILINE)[0].strip()
        if not body:
            continue
        entries.append(FAQEntry(
            question=_normalize_question(m.group(1).strip()),
            short_answer=_truncate_to_sentences(_strip_markdown_for_short_answer(body), short_answer_max_words),
            long_answer=body,
            confidence=1.0,
        ))
    return entries


def render_faq_body_section(entries: Sequence[FAQEntry], heading: str) -> str:
    """Render the body `## <heading>` Q&A section from unified entries."""
    if not entries:
        return ""
    parts = [f"\n\n## {heading}\n"]
    for entry in entries:
        parts.append(f"\n### {entry.question}\n")
        parts.append(f"\n{entry.long_answer}\n")
    return "".join(parts)


__all__ = [
    "FAQEntry",
    "IMPERATIVE_VERB_DENYLIST",
    "build_faq_entries",
    "extract_body_faq",
    "find_faq_section",
    "render_faq_body_section",
]
