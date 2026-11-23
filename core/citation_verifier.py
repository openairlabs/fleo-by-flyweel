"""Deterministic Citation Ledger Verifier & Unverified Stat Detector.

Ensures every external citation URL in generated content actually appeared in the
run's SERP / community / research-index results (or the configured BrandProfile
domain) — eliminating hallucinated URLs and orphaned `#cite-N` links even when
Jev (`FLEO_JEV_ENABLED=0`) is disabled.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_MD_LINK_RE = re.compile(r"(?<!!)(\[([^\]]+)\])\((https?://[^)\s]+)\)")
_HTML_ANCHOR_RE = re.compile(
    r"""<a\b((?:(?:"[^"]*"|'[^']*'|[^>"'])*))>(.*?)</a>""",
    re.IGNORECASE | re.DOTALL,
)
_HREF_ATTR_RE = re.compile(
    r"""(?<![\w.:-])href\s*=\s*(?:(["'])(.*?)\1|([^\s"'=<>`]+))""",
    re.IGNORECASE | re.DOTALL,
)
_CITE_REF_RE = re.compile(r'#cite-(\d+)\b')
_STAT_SENTENCE_RE = re.compile(
    r"([^.!?\n]*?(?:\b\d+(?:\.\d+)?%\s+(?:of|increase|decrease|growth|reduction|higher|lower)|"
    r"\$\d+(?:\.\d+)?\s*(?:billion|million|trillion|[BMK])\b|"
    r"\baccording to\s+[A-Z][A-Za-z0-9&\s]{2,40})[^.!?\n]*[.!?])",
    re.IGNORECASE,
)


def _norm_url(url: str) -> str:
    raw = (url or "").strip()
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    query = parsed.query
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def collect_verified_research_urls(
    research_data: Optional[Dict[str, Any]],
    brand_site_url: str = "",
) -> tuple[Set[str], Set[str]]:
    """Return `(exact_norm_urls, verified_domains)` from all research phases + brand domain."""
    urls: Set[str] = set()
    domains: Set[str] = set()

    def _add(candidate: Any) -> None:
        if not isinstance(candidate, str) or not candidate.startswith(("http://", "https://")):
            return
        try:
            norm = _norm_url(candidate)
            host = (urlsplit(candidate).hostname or "").lower().removeprefix("www.")
        except ValueError:
            logger.debug("Skipping malformed research URL candidate: %r", candidate)
            return
        urls.add(norm)
        if host:
            domains.add(host)

    if brand_site_url:
        _add(brand_site_url)

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            for m in re.findall(r"https?://[^\s)\]>\"']+", node):
                _add(m)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    if research_data:
        _walk(research_data)
    return urls, domains


def verify_and_clean_citations(
    content: str,
    research_data: Optional[Dict[str, Any]],
    *,
    brand_site_url: str = "",
) -> Dict[str, Any]:
    """Strip unverified external URLs (hallucinated links) and detect uncited market stats."""
    if not content:
        return {
            "content": content,
            "stripped_urls": [],
            "uncited_stat_sentences": [],
            "verified_url_count": 0,
        }

    verified_urls, _verified_domains = collect_verified_research_urls(research_data, brand_site_url=brand_site_url)

    brand_host = ""
    if brand_site_url:
        try:
            brand_host = (urlsplit(brand_site_url if "://" in brand_site_url else f"https://{brand_site_url}").hostname or "").lower().removeprefix("www.")
        except ValueError:
            brand_host = ""

    stripped_urls: List[str] = []
    verified_kept = 0

    def _is_verified_url(raw_url: str) -> bool:
        try:
            unescaped = html.unescape(raw_url)
            norm = _norm_url(unescaped)
            host = (urlsplit(unescaped).hostname or "").lower().removeprefix("www.")
        except ValueError:
            return False
        return bool(norm in verified_urls or (brand_host and (host == brand_host or host.endswith(f".{brand_host}"))))

    def _check_link(match: re.Match[str]) -> str:
        nonlocal verified_kept
        inner_text, url = match.group(2), match.group(3)
        if _is_verified_url(url):
            verified_kept += 1
            return match.group(0)
        stripped_urls.append(url)
        return inner_text

    def _check_html_anchor(match: re.Match[str]) -> str:
        nonlocal verified_kept
        attrs, label = match.group(1) or "", match.group(2)
        href_m = _HREF_ATTR_RE.search(attrs)
        if not href_m:
            return match.group(0)
        raw_href = (href_m.group(2) if href_m.group(1) else href_m.group(3)) or ""
        unescaped_href = html.unescape(raw_href).strip()
        if not unescaped_href.lower().startswith(("http://", "https://")):
            return match.group(0)
        if _is_verified_url(raw_href):
            verified_kept += 1
            return match.group(0)
        stripped_urls.append(unescaped_href)
        return label

    # Only scrub body outside YAML frontmatter and code fences
    fm_prefix = ""
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) == 3:
            fm_prefix = f"---{parts[1]}---"
            body = parts[2]

    segments: List[tuple[bool, List[str]]] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    uncited_stats: List[str] = []

    def _push_line(fenced_flag: bool, ln: str) -> None:
        if segments and segments[-1][0] == fenced_flag:
            segments[-1][1].append(ln)
        else:
            segments.append((fenced_flag, [ln]))

    for line in body.splitlines(keepends=True):
        fence_m = re.match(r"^ {0,3}(`{3,}|~{3,})([^\n]*)$", line.rstrip("\r\n"))
        if fence_m:
            run = fence_m.group(1)
            if not in_fence and (run[0] != "`" or "`" not in fence_m.group(2)):
                in_fence = True
                fence_char = run[0]
                fence_len = len(run)
                _push_line(True, line)
                continue
            if in_fence and run[0] == fence_char and len(run) >= fence_len and not fence_m.group(2).strip():
                in_fence = False
                fence_char = ""
                fence_len = 0
                _push_line(True, line)
                continue
        _push_line(in_fence, line)

    cleaned_chunks: List[str] = []
    for is_fenced, seg_lines in segments:
        chunk = "".join(seg_lines)
        if is_fenced:
            cleaned_chunks.append(chunk)
            continue
        cleaned_chunk = _MD_LINK_RE.sub(_check_link, chunk)
        cleaned_chunk = _HTML_ANCHOR_RE.sub(_check_html_anchor, cleaned_chunk)
        cleaned_chunks.append(cleaned_chunk)
        for cleaned_line in cleaned_chunk.splitlines():
            if not cleaned_line.lstrip().startswith(("#", "|", ">")):
                for sm in _STAT_SENTENCE_RE.finditer(cleaned_line):
                    sent = sm.group(1).strip()
                    has_cite = bool(
                        _CITE_REF_RE.search(sent)
                        or re.search(r"\[\d+\]", sent)
                        or "http://" in sent
                        or "https://" in sent
                    )
                    if not has_cite and len(uncited_stats) < 6:
                        uncited_stats.append(sent)

    if stripped_urls:
        logger.warning("Stripped %d unverified/hallucinated URL(s) not present in research ledger", len(stripped_urls))

    return {
        "content": fm_prefix + "".join(cleaned_chunks),
        "stripped_urls": stripped_urls,
        "uncited_stat_sentences": uncited_stats,
        "verified_url_count": verified_kept,
    }
