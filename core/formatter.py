"""Astro Formatter - Complete Astro blog compatibility with all features"""
import re
import json
import yaml
import logging
import unicodedata
import html
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import os

# Schema generation and validation
from core.schema_generator import SchemaGenerator
from core.fact_checker import FactChecker
from core.faq_builder import FAQEntry, build_faq_entries, extract_body_faq, find_faq_section, render_faq_body_section
from core.mdx_security import (
    MdxSecurityError,
    escape_markdown_link_text,
    markdown_code_spans,
    sanitize_content_url,
)
from core.content_scrubber import scrub as scrub_content
from core import jev

logger = logging.getLogger(__name__)

# Default signup/CTA destination (overridden per-instance from BrandProfile).
SIGNUP_URL = 'https://example.com/signup'
_SIGNUP_BUTTON_BLOCK = re.compile(
    r'\n*(?:<div class="not-prose[^"]*">\s*)?<Button\b[^>]*>.*?</Button>(?:\s*</div>)?\n*', re.DOTALL)
_BANNED_CTA_LABEL = re.compile(
    r'(<Button\b[^>]*>)\s*(?:Get\s+your\s+cards|Get\s+Started\s+Free)\s*(</Button>)',
    re.IGNORECASE,
)
_CONCLUSION_H2 = re.compile(
    r'^## (?:Conclusion|Final Thoughts|Bottom Line|The Bottom Line|Wrapping Up|Next Steps?)\b.*$',
    re.MULTILINE | re.IGNORECASE)

DEFAULT_CLUSTER_POSTS: Tuple[str, ...] = ()
_CITE_STOPWORDS = frozenset({
    "http", "https", "www", "com", "org", "net", "io", "co", "html", "articles",
    "article", "help", "docs", "blog", "post", "page", "guide", "research",
    "source", "reddit", "quora", "comments", "the", "and", "for", "with", "that",
    "this", "from", "your", "into", "about", "what", "when", "where", "how",
})


# SEO Validation Gates (Phase 4.2)
# Load limits from config with sensible defaults
def _load_seo_limits() -> Dict[str, Any]:
    """Load SEO character limits from config."""
    config_path = Path(__file__).parent.parent / 'config' / 'seo_optimization.json'
    defaults = {
        'title': {'min': 30, 'max': 70, 'ideal_min': 50, 'ideal_max': 60},
        'meta': {'min': 120, 'max': 160, 'ideal_min': 150, 'ideal_max': 160}
    }

    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                title_rules = config.get('title_rules', {})
                meta_rules = config.get('meta_description_rules', {})

                return {
                    'title': {
                        'min': title_rules.get('min_length', 30),
                        'max': title_rules.get('max_length', 70),
                        'ideal_min': title_rules.get('ideal_length', [50, 60])[0],
                        'ideal_max': title_rules.get('ideal_length', [50, 60])[1]
                    },
                    'meta': {
                        'min': meta_rules.get('min_length', 120),
                        'max': meta_rules.get('max_length', 160),
                        'ideal_min': meta_rules.get('ideal_length', [150, 160])[0],
                        'ideal_max': meta_rules.get('ideal_length', [150, 160])[1]
                    }
                }
        except Exception as e:
            logger.debug("Failed to load SEO length config; using defaults: %s", e, exc_info=True)

    return defaults


_SEO_LIMITS = _load_seo_limits()


def _truncate_keep_keyword(
    text: str, max_len: int, min_len: int, keyword: Optional[str] = None
) -> Tuple[str, bool]:
    """Word-boundary truncate to ``max_len`` while keeping the keyword if it fits.

    Plain length truncation can lop the primary keyword off the end of a title
    or meta description, silently destroying the single most important SEO
    signal. This keeps the keyword phrase whenever it physically fits within the
    limit, and only when it genuinely cannot fit returns ``dropped=True`` so the
    caller can flag it instead of shipping a keyword-less field.

    Returns ``(truncated_text, keyword_dropped)``.
    """
    if len(text) <= max_len:
        return text, False

    truncated = text[:max_len]
    last_space = truncated.rfind(' ')
    base = truncated[:last_space] if last_space > min_len else truncated

    if not keyword:
        return base, False
    sig = [w.lower() for w in keyword.split() if len(w) > 3]
    if not sig:
        return base, False

    if any(w in base.lower() for w in sig):
        return base, False  # keyword already survived

    # Keyword was dropped. Recover it if the earliest significant keyword word
    # ends within the length limit (extend the word-boundary cut to include it).
    text_lower = text.lower()
    end_pos = None
    for w in sig:
        i = text_lower.find(w)
        if i != -1:
            e = i + len(w)
            end_pos = e if end_pos is None else min(end_pos, e)
    if end_pos is not None and end_pos <= max_len:
        window = text[:max_len]
        cut = window.rfind(' ')
        if cut >= end_pos:
            return window[:cut], False
        return window, False

    # Keyword cannot fit within the limit — keep the clean truncation but flag.
    return base, True


def validate_title(title: str, keyword: Optional[str] = None) -> Tuple[bool, str, List[str]]:
    """
    Validate title against SEO rules.

    Returns:
        Tuple of (is_valid, validated_title, list of issues)

    Validation rules:
    - Length: 30-70 chars, ideal 50-60
    - Contains primary keyword (if provided)
    - No banned words
    """
    issues = []
    limits = _SEO_LIMITS['title']

    # Clean title
    clean_title = title.strip()

    # Length validation
    if len(clean_title) < limits['min']:
        issues.append(f"Title too short ({len(clean_title)} chars, min {limits['min']})")
    elif len(clean_title) > limits['max']:
        issues.append(f"Title too long ({len(clean_title)} chars, max {limits['max']})")
        # Truncate at word boundary, preserving the primary keyword when it fits.
        clean_title, kw_dropped = _truncate_keep_keyword(
            clean_title, limits['max'], limits['min'], keyword)
        if kw_dropped:
            issues.append(f"Truncation dropped primary keyword '{keyword}' from title")
            logger.warning(f"⚠️  Title truncated to {len(clean_title)} chars (keyword did not fit)")
        else:
            logger.warning(f"⚠️  Title truncated to {len(clean_title)} chars (keyword preserved)")

    if limits['ideal_min'] <= len(clean_title) <= limits['ideal_max']:
        logger.info(f"✅ Title length optimal: {len(clean_title)} chars")
    elif len(clean_title) <= limits['max']:
        logger.info(f"ℹ️  Title length acceptable: {len(clean_title)} chars (ideal: {limits['ideal_min']}-{limits['ideal_max']})")

    # Keyword check
    if keyword:
        keyword_lower = keyword.lower()
        # Check if any significant words from keyword are in title
        significant_words = [w for w in keyword_lower.split() if len(w) > 3]
        title_lower = clean_title.lower()
        keyword_match = any(word in title_lower for word in significant_words)

        if not keyword_match:
            issues.append(f"Primary keyword '{keyword}' not found in title")

    # Load banned words
    config_path = Path(__file__).parent.parent / 'config' / 'seo_optimization.json'
    banned_words = []
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                banned_words = config.get('title_rules', {}).get('banned_words', [])
        except Exception as e:
            logger.debug("Failed to load banned-words config; using empty list: %s", e, exc_info=True)

    # Check for banned words
    for banned in banned_words:
        if banned.lower() in clean_title.lower():
            issues.append(f"Contains banned word: '{banned}'")

    is_valid = len(issues) == 0
    return (is_valid, clean_title, issues)


def validate_meta_description(description: str, keyword: Optional[str] = None) -> Tuple[bool, str, List[str]]:
    """
    Validate meta description against SEO rules.

    Returns:
        Tuple of (is_valid, validated_description, list of issues)

    Validation rules:
    - Length: 120-160 chars, ideal 150-160
    - Contains primary keyword in first 50 chars (if provided)
    - Ends with CTA-like content
    """
    issues = []
    limits = _SEO_LIMITS['meta']

    # Clean description
    clean_desc = description.strip()

    # Length validation
    if len(clean_desc) < limits['min']:
        issues.append(f"Meta description too short ({len(clean_desc)} chars, min {limits['min']})")
    elif len(clean_desc) > limits['max']:
        issues.append(f"Meta description too long ({len(clean_desc)} chars, max {limits['max']})")
        # Truncate at word boundary, preserving the primary keyword when it fits.
        clean_desc, kw_dropped = _truncate_keep_keyword(
            clean_desc, limits['max'], limits['min'], keyword)
        if kw_dropped:
            issues.append(f"Truncation dropped primary keyword '{keyword}' from meta description")
            logger.warning(f"⚠️  Meta description truncated to {len(clean_desc)} chars (keyword did not fit)")
        else:
            logger.warning(f"⚠️  Meta description truncated to {len(clean_desc)} chars (keyword preserved)")

    if limits['ideal_min'] <= len(clean_desc) <= limits['ideal_max']:
        logger.info(f"✅ Meta description length optimal: {len(clean_desc)} chars")
    elif len(clean_desc) <= limits['max']:
        logger.info(f"ℹ️  Meta description length acceptable: {len(clean_desc)} chars (ideal: {limits['ideal_min']}-{limits['ideal_max']})")

    # Keyword in first 50 chars check
    if keyword:
        first_50 = clean_desc[:50].lower()
        keyword_words = [w.lower() for w in keyword.split() if len(w) > 3]
        keyword_in_first_50 = any(word in first_50 for word in keyword_words)

        if not keyword_in_first_50:
            issues.append(f"Primary keyword not found in first 50 chars of meta description")

    is_valid = len(issues) == 0
    return (is_valid, clean_desc, issues)


def validate_frontmatter_seo(frontmatter: Dict[str, Any], keyword: Optional[str] = None) -> Dict[str, Any]:
    """
    Validate and fix frontmatter for SEO compliance.

    Returns validated frontmatter with any issues logged.
    """
    validated = frontmatter.copy()
    all_issues = []

    # Validate title
    if 'title' in validated:
        is_valid, clean_title, issues = validate_title(validated['title'], keyword)
        validated['title'] = clean_title
        all_issues.extend(issues)

    # Validate description
    if 'description' in validated:
        is_valid, clean_desc, issues = validate_meta_description(validated['description'], keyword)
        validated['description'] = clean_desc
        all_issues.extend(issues)

    # Log issues
    if all_issues:
        logger.warning(f"⚠️  SEO validation issues found:")
        for issue in all_issues:
            logger.warning(f"   - {issue}")
    else:
        logger.info(f"✅ Frontmatter SEO validation passed")

    return validated

# Articles, conjunctions, prepositions and copulas that add no SEO value in a
# URL slug. Query-intent words (how, what, why, best, top, vs, guide) are
# deliberately NOT here — they are frequently the primary keyword.
SLUG_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "nor", "of", "to", "for", "in",
    "on", "at", "by", "with", "from", "as", "is", "are", "was", "were",
    "be", "been", "being", "that", "this", "these", "those", "into",
    "than", "then", "it", "its",
})

# SEO slug shape limits (Google favours short, keyword-dense slugs).
SLUG_MAX_WORDS = 8
SLUG_MAX_CHARS = 60


def build_seo_slug(
    text: str,
    *,
    max_words: int = SLUG_MAX_WORDS,
    max_chars: int = SLUG_MAX_CHARS,
) -> str:
    """Build an SEO-optimised URL slug from arbitrary text.

    Best-practice rules applied, in order:
      1. Unicode → ASCII transliteration (café → cafe), so the slug is
         portable and not percent-encoded in the address bar.
      2. Lowercase; ``&`` → ``and``; intra-word apostrophes dropped
         (don't → dont, not don-t).
      3. Every other non-alphanumeric run collapses to a single hyphen.
      4. Stop-words removed (``SLUG_STOPWORDS``) for keyword density —
         but if that would empty the slug, the unfiltered tokens are kept.
      5. Capped to ``max_words`` words and ``max_chars`` chars, always
         breaking on a word boundary (never mid-word).
      6. No leading/trailing/repeated hyphens.

    Returns ``""`` only for input that has no alphanumeric content.
    """
    if not text:
        return ""

    # 1. Transliterate accents/diacritics to ASCII.
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")

    # 2. Lowercase + symbol normalisation.
    text = text.lower().strip()
    text = text.replace("&", " and ")
    text = re.sub(r"['’]", "", text)  # straight + curly apostrophes

    # 3. Non-alphanumeric → hyphen, then tokenise.
    text = re.sub(r"[^a-z0-9]+", "-", text)
    tokens = [t for t in text.split("-") if t]
    if not tokens:
        return ""

    # 4. Drop stop-words, but never return an all-stop-word slug empty.
    filtered = [t for t in tokens if t not in SLUG_STOPWORDS]
    if not filtered:
        filtered = tokens

    # 5. Word cap.
    filtered = filtered[:max_words]

    # 6. Char cap on a word boundary.
    slug = filtered[0]
    for tok in filtered[1:]:
        if len(slug) + 1 + len(tok) > max_chars:
            break
        slug += "-" + tok
    return slug[:max_chars].strip("-")


def restamp_slug(
    mdx: str,
    old_slug: str,
    new_slug: str,
    blog_base_path: Optional[str] = None,
) -> str:
    """Re-stamp a slug inside an already-built MDX so it stays consistent.

    Used when the user edits the proposed slug after generation: rewrites the
    canonical URL (under ``blog_base_path`` or ``/blog/``) and hero-image path
    (``{old}-hero.webp``) to the new slug without touching sibling paths like
    ``/blog/{old}-comparison`` or body prose. No-op when slugs match or either
    is empty.
    """
    if not old_slug or not new_slug or old_slug == new_slug:
        return mdx

    if blog_base_path is None:
        from core.brand_profile import load_brand_profile

        blog_base_path = load_brand_profile().blog_base_path

    norm_base = f"/{blog_base_path.strip('/')}" if (blog_base_path and blog_base_path.strip("/")) else "/blog"
    prefixes = sorted({"/blog/", f"{norm_base}/"}, key=len, reverse=True)
    prefix_alt = "|".join(re.escape(p) for p in prefixes)
    esc_old = re.escape(old_slug)
    blog_pat = re.compile(rf"({prefix_alt}){esc_old}(?=(?:/?(?:[\"'\s\)\]#?]|$)))")
    hero_pat = re.compile(rf"(?<=/){esc_old}(-hero\.webp\b)|^{esc_old}(-hero\.webp\b)", re.MULTILINE)

    def _replace_hero(m: re.Match[str]) -> str:
        suffix = m.group(1) or m.group(2)
        return f"{new_slug}{suffix}"

    if mdx.startswith("---\n") and "\n---\n" in mdx[4:]:
        head_end = mdx.index("\n---\n", 4) + 5
        head = blog_pat.sub(rf"\g<1>{new_slug}", mdx[:head_end])
        head = hero_pat.sub(_replace_hero, head)
        body = mdx[head_end:]
        # Also update any <Image src=".../{old_slug}-hero.webp" /> hero tag in body without touching sibling links
        body = re.sub(
            rf'(<Image\b[^>]*\bsrc=["\'][^"\']*/){esc_old}(-hero\.webp["\'])',
            rf"\g<1>{new_slug}\g<2>",
            body,
        )
        return head + body

    mdx = blog_pat.sub(rf"\g<1>{new_slug}", mdx)
    mdx = hero_pat.sub(_replace_hero, mdx)
    return mdx


class AstroFormatter:
    """Format content for Astro blog with all required components"""

    def __init__(self, site_context=None, flyweel_mode='full', brand_profile=None):
        from core.brand_profile import load_brand_profile
        self.brand_profile = brand_profile or load_brand_profile()
        self.author_email = os.getenv('AUTHOR_EMAIL', self.brand_profile.resolve_author().email or '')
        self.blog_schema = self._load_blog_schema()
        self.content_templates = self._load_content_templates()
        self.brand_voice_config = self._load_brand_voice_config()
        self.seo_config = self._load_seo_config()
        self.site_context = site_context
        self.flyweel_mode = self.brand_profile.effective_brand_mode(flyweel_mode) if flyweel_mode == 'full' and not self.brand_profile.is_configured() and not site_context else flyweel_mode

        self.schema_generator = SchemaGenerator(brand_profile=self.brand_profile)
        self.fact_checker = FactChecker()

        self._unified_faq_entries: List[FAQEntry] = []
        self._current_article_slug = ""
        self._current_article_url = ""
        self._current_keyword = ""

        if self.flyweel_mode != 'none':
            self.link_opportunities = self._build_link_opportunity_map(site_context)
        else:
            self.link_opportunities = {}

    def _load_blog_schema(self):
        """Load optional blog content schema if available in config/."""
        config_dir = Path(__file__).parent.parent / 'config'
        schema_path = config_dir / 'blog-content-schema.json'
        if schema_path.exists():
            with open(schema_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None

    def _load_content_templates(self):
        """Load content templates for different styles."""
        config_dir = Path(__file__).parent.parent / 'config'
        templates_path = config_dir / 'content-templates.json'
        if templates_path.exists():
            with open(templates_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def _load_brand_voice_config(self):
        """Load brand voice configuration"""
        config_dir = Path(__file__).parent.parent / 'config'
        config_path = config_dir / 'brand_voice_config.json'

        if config_path.exists():
            with open(config_path, 'r') as f:
                return json.load(f)
        return {}

    def _load_seo_config(self) -> Dict[str, Any]:
        """Load SEO optimization configuration"""
        config_path = Path(__file__).parent.parent / 'config' / 'seo_optimization.json'
        if config_path.exists():
            try:
                with open(config_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.debug("Failed to load SEO config; using empty config: %s", e, exc_info=True)
        return {}

    def _generate_slug(self, keyword: str) -> str:
        """Generate an SEO-optimised URL slug from the target keyword.

        This is the canonical slug — it flows into the frontmatter ``slug``
        and the ``canonical`` URL, so it must be short, ASCII, and
        stop-word free for keyword density.
        """
        return build_seo_slug(keyword)

    def _calculate_reading_time(self, content: str) -> str:
        """Calculate reading time from content word count (265 WPM - Medium standard)"""
        # Remove code blocks and imports from word count
        import re
        clean = re.sub(r'```[\s\S]*?```', '', content)  # Remove code blocks
        clean = re.sub(r'^import.*$', '', clean, flags=re.MULTILINE)  # Remove imports
        clean = re.sub(r'<[^>]+>', '', clean)  # Remove HTML/JSX tags
        words = len(clean.split())
        minutes = max(1, round(words / 265))  # 265 WPM is Medium's standard
        return f"{minutes} min read"

    def _build_unified_faq_entries(
        self,
        paa_questions: List[str],
        content: str,
        keyword: str
    ) -> List[FAQEntry]:
        """Build the canonical FAQ list shared by body, frontmatter, and schema."""
        faq_rules = self.seo_config.get('faq_rules', {})
        min_faqs = faq_rules.get('min_questions', 12)
        max_faqs = faq_rules.get('max_questions', 15)
        word_bounds = faq_rules.get('answer_word_count', {})
        short_max = int(word_bounds.get('max', 60))
        questions = self._collect_faq_questions(
            paa_questions or [], content, keyword, min_faqs, max_faqs
        )
        # `_inject_faq_internal_links` already exists for the legacy path; reuse
        # it as the long-answer post-processor so links are consistent.
        link_hook = getattr(self, '_inject_faq_internal_links', None)
        entries = build_faq_entries(
            questions,
            content,
            min_confidence=3.0,
            max_entries=max_faqs,
            keyword=keyword,
            short_answer_max_words=short_max,
            long_answer_max_words=max(short_max + 30, 90),
            post_process_long=link_hook,
        )
        if len(entries) < min_faqs:
            logger.info(
                "FAQ builder produced %d entries (target>=%d); the FAQ section "
                "will be shorter rather than padded with boilerplate.",
                len(entries), min_faqs,
            )
        return entries

    def _build_faq_array(self, paa_questions: List[str], content: str, keyword: str = "") -> List[Dict[str, str]]:
        """Build FAQ array for YAML frontmatter from the unified entries."""
        entries = getattr(self, '_unified_faq_entries', None)
        if entries is None:
            entries = self._build_unified_faq_entries(paa_questions or [], content, keyword)
            self._unified_faq_entries = entries
        return [e.as_frontmatter() for e in entries]

    def _extract_short_answer_for_frontmatter(self, question: str, content: str) -> str:
        """Extract a 40-60 word answer for frontmatter FAQ array."""
        question_words = set(re.findall(r'\w+', question.lower()))
        question_words -= {'what', 'how', 'why', 'when', 'where', 'which', 'is', 'are', 'the', 'a', 'an', 'do', 'does', 'can', 'will', 'should'}

        paragraphs = re.split(r'\n\n+', content)
        best_para = ""
        best_score = 0

        for para in paragraphs:
            if len(para) < 50 or para.startswith('#') or para.startswith('import'):
                continue
            para_words = set(re.findall(r'\w+', para.lower()))
            score = len(question_words & para_words)
            if score > best_score:
                best_score = score
                best_para = para

        if best_para:
            answer = self._normalize_atomic_answer(best_para, question)
            return self._plain_frontmatter_text(answer)

        return self._plain_frontmatter_text(self._fallback_atomic_answer(question))

    @staticmethod
    def _clean_plain_text_field(text: str) -> str:
        """Strip markdown, HTML, footnote markers, inline citations, and em dashes from plain-text fields."""
        if not text:
            return ""
        text = re.sub(r'<sup>\s*<a[^>]*>.*?</a>\s*</sup>', '', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<a[^>]*class=["\']citation-ref["\'][^>]*>.*?</a>', '', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'\[\d+\]\(#home-fn-\d+\)', '', text)
        text = re.sub(r'[¹²³]', '', text)
        text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
        text = re.sub(r'\*\*([^\*]+)\*\*', r'\1', text)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        text = re.sub(r'<[^>]+>', '', text)
        text = re.sub(r'\s+—\s+', ' - ', text)
        text = text.replace('—', '-').replace('–', '-')
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _plain_frontmatter_text(self, text: str) -> str:
        """Strip markdown that does not belong in scalar FAQ frontmatter."""
        return self._clean_plain_text_field(text)

    def _collect_faq_questions(
        self,
        paa_questions: List[str],
        content: str,
        keyword: str,
        min_faqs: int,
        max_faqs: int
    ) -> List[str]:
        """Collect deduped FAQ questions from PAA data, existing content, and deterministic fallbacks."""
        questions = []
        seen = set()

        def add(question: str):
            question = (question or '').strip()
            if not question:
                return
            if not question.endswith('?'):
                question += '?'
            key = re.sub(r'\W+', ' ', question.lower()).strip()
            if key and key not in seen:
                seen.add(key)
                questions.append(question)

        for question in self._prioritize_long_tail_questions(list(paa_questions or [])):
            add(question)

        for question in self._extract_faq_questions_from_content(content):
            add(question)

        if len(questions) < min_faqs:
            for question in self._fallback_aeo_questions(keyword):
                add(question)
                if len(questions) >= min_faqs:
                    break

        return questions[:max_faqs]

    def _extract_faq_questions_from_content(self, content: str) -> List[str]:
        """Extract H3 FAQ-style questions already present in the content body."""
        return [
            match.group(1).strip()
            for match in re.finditer(r'^###\s+([^#\n]+\?)\s*$', content, re.MULTILINE)
        ]

    def _fallback_aeo_questions(self, keyword: str) -> List[str]:
        """Deterministic, domain-agnostic AEO fallback questions when PAA coverage is thin."""
        topic = keyword.strip() or "this topic"
        return [
            f"What is {topic} and why does it matter in practice?",
            f"How does {topic} work step by step?",
            f"What are the main benefits and trade-offs of {topic}?",
            f"When should a team prioritize {topic}?",
            f"Which roles are typically responsible for {topic}?",
            f"What inputs or prerequisites are needed for {topic}?",
            f"How long does {topic} typically take to produce measurable outcomes?",
            f"What are the most common mistakes teams make with {topic}?",
            f"How does manual {topic} compare to automated {topic}?",
            f"What metrics show whether {topic} is working?",
            f"How should buyers evaluate tools or vendors for {topic}?",
            f"How does {topic} integrate into existing workflows?",
        ]

    def _to_relative_internal_url(self, url: str) -> str:
        """Convert configured brand domain URLs to clean relative paths."""
        from core.brand_profile import load_brand_profile
        raw = (url or "").strip()
        profile = getattr(self, "brand_profile", None) or load_brand_profile()
        host = profile.domain_host()
        if host:
            m = re.match(rf"^https?://(?:www\.)?{re.escape(host)}(/.*)?$", raw, re.IGNORECASE)
            if m:
                path = (m.group(1) or "/").rstrip("/")
                return path if path else "/"
        if raw.startswith("/") and len(raw) > 1:
            return raw.rstrip("/")
        return raw

    def _normalize_link_url(self, url: str) -> str:
        """Normalize internal URLs for comparison while preserving output URLs."""
        rel = self._to_relative_internal_url(url)
        return (rel or "").strip().rstrip('/').lower() or "/"

    def _tokenize_link_text(self, text: str) -> List[str]:
        stop_words = {
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
            'of', 'with', 'by', 'from', 'up', 'about', 'into', 'through', 'during',
            'your', 'our', 'their', 'this', 'that', 'these', 'those', 'what', 'why',
            'when', 'how', 'best', 'top', 'guide', 'standard', 'comparison',
            'investment', 'managing', 'optimize', 'industry', 'platform', 'reporting',
            'tracking', 'business', 'marketing', 'sales', 'data', 'system', 'systems',
            'process', 'approach', 'strategy', 'strategies', 'learn', 'more', 'read'
        }
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9]+", text or "")
        return [t.lower() for t in tokens if len(t) >= 4 and t.lower() not in stop_words]

    def _is_generic_anchor(self, anchor: str) -> bool:
        clean = re.sub(r"\s+", " ", (anchor or "").strip()).lower()
        tokens = self._tokenize_link_text(clean)
        generic = {
            'standard', 'comparison', 'investment', 'managing', 'tracking', 'platform',
            'reporting', 'optimize', 'industry', 'tools', 'blog', 'features',
            'documentation', 'performance', 'solution', 'solutions', 'approach',
            'strategy', 'process', 'system', 'systems', 'business', 'marketing', 'sales'
        }
        if not clean or len(clean) < 4:
            return True
        if clean in {'click here', 'read more', 'learn more', 'this guide', 'this post'}:
            return True
        if len(tokens) == 1 and tokens[0] in generic:
            return True
        if len(tokens) == 1 and len(tokens[0]) < 5:
            return True
        return False

    def _destination_tokens_from_url(self, url: str) -> List[str]:
        try:
            from urllib.parse import urlparse
            path = urlparse(url or "").path or ""
        except Exception:
            path = url or ""
        return self._tokenize_link_text(path.replace('-', ' ').replace('/', ' '))

    def _candidate_destination_tokens(self, candidate: Dict[str, Any]) -> List[str]:
        pieces = [
            candidate.get('url', ''), candidate.get('title', ''),
            candidate.get('anchor_text', ''), candidate.get('description', ''),
            candidate.get('category', ''), candidate.get('type', ''),
        ]
        tags = candidate.get('tags') or candidate.get('categories')
        if isinstance(tags, list):
            pieces.extend(str(t) for t in tags)
        return list(dict.fromkeys(
            self._destination_tokens_from_url(candidate.get('url', ''))
            + self._tokenize_link_text(' '.join(str(p) for p in pieces if p))
        ))

    def _register_link_candidate(self, link_map: Dict[str, Dict], term: str, candidate: Dict[str, Any]) -> None:
        term = re.sub(r"\s+", " ", (term or "").strip())
        if not term or self._is_generic_anchor(term):
            return
        term_tokens = self._tokenize_link_text(term)
        if len(term_tokens) == 1 and candidate.get('priority', 0) < 8:
            return
        key = term.lower()
        candidate = dict(candidate)
        candidate.setdefault('anchor_text', term)
        candidate.setdefault('title', term)
        candidate['term_tokens'] = term_tokens
        candidate['destination_tokens'] = self._candidate_destination_tokens(candidate)
        existing = link_map.get(key)
        if not existing or candidate.get('priority', 0) > existing.get('priority', 0):
            link_map[key] = candidate

    def _build_link_opportunity_map(self, site_context: Optional[Dict]) -> Dict[str, Dict]:
        """Build scored link opportunities from sitemap/repo metadata."""
        link_map: Dict[str, Dict] = {}
        if not site_context:
            return link_map

        metadata_by_url = site_context.get('internal_link_metadata') or {}

        for post in site_context.get('blog_posts', []):
            if not post.get('url') or not post.get('title'):
                continue
            base = {
                'url': post['url'],
                'title': post.get('title', ''),
                'description': post.get('description', ''),
                'tags': post.get('tags') or post.get('categories') or [],
                'category': post.get('category', ''),
                'type': 'blog',
                'source_type': 'blog',
                'priority': 4,
            }
            for term in self._extract_linkable_terms(post.get('title', '')):
                self._register_link_candidate(link_map, term, base)

        for text, value in (site_context.get('internal_links', {}) or {}).items():
            if not text or not value:
                continue
            if isinstance(value, dict):
                url = value.get('url') or value.get('href')
                metadata = dict(value)
            else:
                url = str(value)
                metadata = dict(metadata_by_url.get(url) or metadata_by_url.get(url.rstrip('/')) or {})
            if not url:
                continue
            lower = str(text).lower()
            priority = 8 if any(k in lower for k in ('pricing', 'integration', 'product', 'feature', 'platform', 'doc')) else 5
            candidate = {
                'url': url,
                'title': metadata.get('title') or text,
                'description': metadata.get('description', ''),
                'tags': metadata.get('tags') or [],
                'category': metadata.get('category', ''),
                'type': metadata.get('type') or 'page',
                'source_type': metadata.get('source_type') or 'page',
                'priority': metadata.get('priority', priority),
            }
            self._register_link_candidate(link_map, str(text), candidate)

        return link_map

    def _extract_linkable_terms(self, text: str) -> List[str]:
        """Extract meaningful multi-word terms from text that could be link anchors."""
        if not text:
            return []
        raw_words = re.findall(r"[A-Za-z][A-Za-z0-9+]*", text)
        terms: List[str] = []
        for size in (4, 3, 2):
            for i in range(0, max(0, len(raw_words) - size + 1)):
                phrase = " ".join(raw_words[i:i + size]).strip()
                tokens = self._tokenize_link_text(phrase)
                if len(tokens) >= 2 and len(phrase) >= 10 and not self._is_generic_anchor(phrase):
                    terms.append(phrase)
        if 2 <= len(self._tokenize_link_text(text)) <= 7 and len(text) <= 80 and not self._is_generic_anchor(text):
            terms.insert(0, text.strip())
        return list(dict.fromkeys(terms))[:8]

    def _validate_frontmatter(self, frontmatter: Dict[str, Any], slug: str) -> List[str]:
        """Validate frontmatter against Astro requirements"""
        warnings = []

        # Required fields
        required_fields = ['title', 'publishDate', 'description', 'author', 'image', 'tags', 'category']
        for field in required_fields:
            if field not in frontmatter or not frontmatter[field]:
                warnings.append(f"Missing required field: {field}")

        # Image validation
        if 'image' in frontmatter:
            image = frontmatter['image']
            if not isinstance(image, dict):
                warnings.append("Image must be a dict with 'src' and 'alt' fields")
            else:
                if 'src' not in image or not image['src'].startswith('/src/assets/blog/'):
                    warnings.append(f"Image src must start with /src/assets/blog/, got: {image.get('src', 'missing')}")
                if 'src' in image and not image['src'].endswith('.webp'):
                    warnings.append(f"Image should be .webp format, got: {image['src']}")
                if 'alt' not in image or not image['alt']:
                    warnings.append("Image missing alt text")

        # Title length (SEO best practice)
        if 'title' in frontmatter and len(frontmatter['title']) > 60:
            warnings.append(f"Title too long for SEO ({len(frontmatter['title'])} chars, max 60)")

        # Description length (SEO best practice)
        if 'description' in frontmatter:
            desc_len = len(frontmatter['description'])
            if desc_len < 120:
                warnings.append(f"Description too short ({desc_len} chars, min 120)")
            elif desc_len > 160:
                warnings.append(f"Description too long ({desc_len} chars, max 160)")

        # Tags validation
        if 'tags' in frontmatter:
            if not isinstance(frontmatter['tags'], list):
                warnings.append("Tags must be a list")
            elif len(frontmatter['tags']) < 2:
                warnings.append("Should have at least 2 tags for SEO")
            elif len(frontmatter['tags']) > 8:
                warnings.append(f"Too many tags ({len(frontmatter['tags'])}, max 8)")

        # TLDR validation (AEO answer-first requirements)
        if 'tldr' in frontmatter:
            tldr = frontmatter['tldr']
            if not isinstance(tldr, dict):
                warnings.append("tldr must be a dictionary")
            else:
                answer = tldr.get('answer', '')
                takeaways = tldr.get('takeaways', [])
                if not answer or not isinstance(answer, str):
                    warnings.append("tldr missing or invalid answer field")
                elif len(answer) < 120 or len(answer) > 500:
                    warnings.append(f"tldr answer length is {len(answer)} (strict limit: 120-500 chars)")
                
                if not isinstance(takeaways, list):
                    warnings.append("tldr takeaways must be a list")
                elif len(takeaways) < 3 or len(takeaways) > 6:
                    warnings.append(f"tldr takeaways count is {len(takeaways)} (must be 3-6)")

        return warnings

    def format(self, content: str, keyword: str, style: str = "standard",
              research_data: Optional[Dict] = None, slug: Optional[str] = None) -> Dict[str, Any]:
        """Format content with complete Astro frontmatter and all sections.

        ``slug`` is the single canonical slug for this article — it flows into
        the returned dict, the hero-image path, the canonical URL, and JSON-LD,
        so they never diverge. A smart (LLM-proposed) slug is passed in by the
        generator; when absent we fall back to the deterministic keyword slug.
        Either way it is re-sanitised through ``build_seo_slug`` as a guardrail.
        """

        # Generate metadata (with SEO optimization)
        title = self._generate_title(content, keyword, style)
        meta_description = self._generate_meta_description(content, keyword, style=style)
        slug = build_seo_slug(slug) if slug else self._generate_slug(keyword)
        self._current_keyword = keyword
        self._current_article_slug = slug
        self._current_article_url = self.brand_profile.canonical_blog_url(slug)
        self._unified_faq_entries = []

        # Extract sections
        h2_sections = self._extract_h2_sections(content)

        # Process content based on style
        processed_content = self._process_content_by_style(content, style, keyword, research_data)

        # Build unified FAQ entries for frontmatter `faq:` (body FAQ restored by site_adapters unless layout renders frontmatter).
        serp_data = (research_data.get('serp') or {}) if research_data else {}
        paa_questions = []
        if serp_data:
            paa_questions = serp_data.get('paa_questions') or []
        if not isinstance(paa_questions, list):
            paa_questions = []
        body_faq_entries = extract_body_faq(processed_content, explicit_only=True)
        processed_content = self._strip_body_faq_section(processed_content)
        built_entries = self._build_unified_faq_entries(
            paa_questions, processed_content, keyword
        )
        seen_q = set()
        merged_entries: List[FAQEntry] = []
        for entry in list(body_faq_entries) + list(built_entries):
            q_key = re.sub(r'\W+', ' ', entry.question.lower()).strip()
            if q_key and q_key not in seen_q:
                seen_q.add(q_key)
                merged_entries.append(FAQEntry(
                    question=self._clean_plain_text_field(entry.question),
                    short_answer=self._clean_plain_text_field(entry.short_answer),
                    long_answer=self._clean_plain_text_field(entry.long_answer),
                    confidence=entry.confidence,
                ))
        self._unified_faq_entries = merged_entries[:12]

        # Add comparison table for comparison style
        if style == "comparison":
            processed_content = self._add_comparison_table(processed_content, keyword)

        # End-of-article signup CTA (every article unless --nfw)
        processed_content = self._add_signup_cta(processed_content)

        # Add internal links (relative paths)
        processed_content = self._add_internal_links(processed_content)
        processed_content = self.normalize_internal_links(processed_content, brand_profile=self.brand_profile)

        # Add citations from research
        if research_data:
            processed_content = self._add_citations(processed_content, research_data, style)

        # Process external links (add nofollow for SEO)
        processed_content = self._process_external_links(processed_content)

        # Apply optional brand compliance footnotes (no-op unless configured in BrandProfile)
        if self.flyweel_mode != 'none':
            processed_content = self.apply_compliance_footnotes(processed_content, brand_profile=self.brand_profile)

        category, categories = self._determine_category_and_list(keyword, content)
        tags = self._generate_tags(keyword, content, style)
        product, funnel_stage, topic = self._compute_page_taxonomy(keyword, style, brand_profile=self.brand_profile)
        related_posts = self._select_related_posts(keyword, slug, topic, category, tags)

        # Build complete frontmatter (strictly astro blogSchema compliant — no unknown keys like readingTime)
        frontmatter = {
            'title': title,
            'publishDate': datetime.now().strftime('%Y-%m-%d'),
            'updatedDate': datetime.now().strftime('%Y-%m-%d'),
            'description': meta_description,
            'excerpt': self._generate_excerpt(processed_content, meta_description),
            'author': self._resolve_author(content),
            'image': {
                'src': f'/src/assets/blog/{slug}-hero.webp',
                'alt': self._generate_hero_alt(title, keyword, style, content)
            },
            'tags': tags,
            'category': category,
            'categories': categories,
            'product': product,
            'funnel_stage': funnel_stage,
            'topic': topic,
            'featured': False,
            'draft': False,
            'relatedPosts': related_posts,
            'tldr': self._extract_or_build_tldr(content, processed_content, title, keyword),
        }

        # SEO validation gates - validate and fix title/meta (Phase 4.2)
        frontmatter = validate_frontmatter_seo(frontmatter, keyword)
        title = frontmatter['title']  # May have been truncated
        meta_description = frontmatter['description']  # May have been truncated

        # Validate frontmatter against Astro requirements
        validation_warnings = self._validate_frontmatter(frontmatter, slug)
        if validation_warnings:
            for warning in validation_warnings:
                logger.warning(f"⚠️  Frontmatter validation: {warning}")

        # Build complete MDX with all components
        mdx_content = self._build_complete_mdx(frontmatter, processed_content, keyword, h2_sections, research_data, style, slug=slug)

        return {
            'title': title,
            'meta_description': meta_description,
            'slug': slug,
            'content': mdx_content,
            'h2_sections': h2_sections,
            'tags': frontmatter['tags'],
            'category': frontmatter['category'],
            'style': style
        }
    
    def _process_content_by_style(self, content: str, style: str, keyword: str,
                                 research_data: Optional[Dict] = None) -> str:
        """Process content based on style template"""

        # Style-specific processing
        if style == "comparison":
            content = self._enhance_comparison_content(content, keyword)
        elif style == "guide":
            content = self._enhance_guide_content(content, keyword)
        elif style == "research":
            content = self._enhance_research_content(content, research_data)
        elif style == "news":
            content = self._enhance_news_content(content)
        elif style == "category":
            content = self._enhance_category_content(content, keyword)
        elif style == "top-compare":
            content = self._enhance_top_compare_content(content, keyword)

        return content
    
    def _enhance_comparison_content(self, content: str, keyword: str) -> str:
        """Enhance comparison style content"""
        # Add comparison markers if not present
        if "## Quick Comparison" not in content:
            # Find a good spot to insert comparison
            first_h2_pos = content.find("## ")
            if first_h2_pos > 0:
                content = (content[:first_h2_pos] + 
                          "## Quick Comparison\n\n" +
                          "[Comparison table will be inserted here]\n\n" +
                          content[first_h2_pos:])
        return content
    
    def _enhance_guide_content(self, content: str, keyword: str) -> str:
        """Enhance guide style content"""
        # Ensure step-by-step structure
        if "## Step" not in content and "## How to" not in content:
            # Add step markers
            h2_sections = re.findall(r'^## (.+)$', content, re.MULTILINE)
            for i, section in enumerate(h2_sections[:5], 1):
                if not section.startswith("Step"):
                    content = content.replace(f"## {section}", f"## Step {i}: {section}")
        return content
    
    def _enhance_research_content(self, content: str, research_data: Optional[Dict]) -> str:
        """Enhance research style content with citations"""
        if research_data:
            # Add research methodology section if missing (professional, no metadata exposure)
            if "## Research Methodology" not in content and "## Our Research" not in content:
                methodology = "\n\n## Research Methodology\n\n"
                methodology += "This comprehensive analysis draws from:\n\n"

                # Only mention SERP analysis - no Reddit/Quora platform exposure
                serp_results = (research_data.get('serp') or {}).get('search_results', [])
                if serp_results:
                    methodology += f"- In-depth analysis of {len(serp_results)} authoritative industry sources\n"

                methodology += "- Current market data and industry benchmarks\n"
                methodology += "- Expert insights from leading practitioners\n"
                methodology += "- Systematic evaluation using established research frameworks\n"

                # Insert after introduction
                first_h2 = content.find("## ")
                if first_h2 > 0:
                    content = content[:first_h2] + methodology + "\n" + content[first_h2:]

        return content
    
    def _enhance_news_content(self, content: str) -> str:
        """Enhance news style content"""
        # Add publication date context
        today = datetime.now().strftime("%B %d, %Y")
        if today not in content:
            # Add date after title
            content = re.sub(r'^(#[^#\n]+)\n', f'\\1\n\n*Published on {today}*\n\n', content)
        return content
    
    def _enhance_category_content(self, content: str, keyword: str) -> str:
        """Enhance category overview content"""
        # Ensure category structure
        if "## Overview" not in content:
            first_h2 = content.find("## ")
            if first_h2 > 0:
                content = content[:first_h2] + f"## {keyword.title()} Overview\n\n" + content[first_h2:]
        return content

    def _enhance_top_compare_content(self, content: str, keyword: str) -> str:
        """Enhance top-compare style content with proper structure

        This method ensures the top-compare article has:
        - Quick comparison summary table
        - Numbered solution sections
        - Proper heading hierarchy
        """
        # Ensure Quick Comparison section exists at the top
        if "## Quick Comparison" not in content:
            # Find first H2 and insert Quick Comparison before it
            first_h2_pos = content.find("## ")
            if first_h2_pos > 0:
                quick_compare_placeholder = """## Quick Comparison

| # | Solution | Best For | Pricing |
|---|----------|----------|---------|
| 1 | [Solution details will be extracted from content below] | | |

"""
                content = content[:first_h2_pos] + quick_compare_placeholder + content[first_h2_pos:]

        # Ensure "How to Choose" section exists
        if "## How to Choose" not in content:
            # Find FAQ section or end of solutions
            faq_pos = content.find("## Frequently Asked Questions")
            if faq_pos < 0:
                faq_pos = content.find("## FAQ")
            if faq_pos < 0:
                faq_pos = len(content)

            how_to_choose = """

## How to Choose the Right Solution

When evaluating your options, consider these key factors:

- **Budget:** Match pricing models to your investment capacity
- **Team Expertise:** Consider internal capabilities and learning curve
- **Integration Needs:** Ensure compatibility with existing tech stack
- **Timeline:** Align expected results with your business goals
- **Industry Fit:** Look for solutions with relevant experience

"""
            # Insert before FAQ
            content = content[:faq_pos] + how_to_choose + content[faq_pos:]

        # Ensure Conclusion section exists
        if "## Conclusion" not in content:
            faq_pos = content.find("## Frequently Asked Questions")
            if faq_pos < 0:
                faq_pos = content.find("## FAQ")
            if faq_pos > 0:
                conclusion = f"""

## Conclusion

Each solution on this list brings unique strengths to {keyword}. The right choice depends on your specific needs, budget, and growth trajectory. Consider scheduling demos with your top 2-3 picks to see which platform best fits your workflow.

"""
                content = content[:faq_pos] + conclusion + content[faq_pos:]

        return content

    def _prioritize_long_tail_questions(self, questions: List[str]) -> List[str]:
        """Prioritize long-tail questions for FAQ section"""
        if not questions:
            return []

        scored_questions = []
        for q in questions:
            score = 0
            word_count = len(q.split())

            # Long-tail indicators (higher score = more specific/long-tail)
            if word_count >= 7:  # Longer questions = more specific
                score += 3
            elif word_count >= 5:
                score += 2

            # Specific question words indicate long-tail intent
            long_tail_phrases = [
                'specific', 'best way', 'difference between', 'how long', 'how much',
                'what happens when', 'can i', 'should i', 'do i need', 'vs', 'versus',
                'compare', 'comparison', 'alternative', 'instead of', 'better than',
                'step by step', 'easiest way', 'fastest way', 'cheapest', 'most expensive',
                'which is better', 'pros and cons', 'advantages', 'disadvantages',
                'worth it', 'best time', 'when should', 'how often'
            ]
            q_lower = q.lower()
            for phrase in long_tail_phrases:
                if phrase in q_lower:
                    score += 2
                    break

            # Avoid generic short questions
            generic_patterns = ['what is', 'define', 'meaning of', 'what are']
            if any(g in q_lower for g in generic_patterns) and word_count < 6:
                score -= 2

            scored_questions.append((score, word_count, q))

        # Sort by score (highest first), then by length (longer first)
        scored_questions.sort(key=lambda x: (x[0], x[1]), reverse=True)

        return [q for _, _, q in scored_questions]

    def _add_qa_section(self, content: str, paa_questions: List[str], keyword: str = "") -> str:
        """Render the body FAQ section from the unified FAQ entries.

        The unified entries are computed once per article in `format()` and
        stored on `self._unified_faq_entries`; this method only renders them
        so the body, frontmatter, and JSON-LD schema stay in sync.
        """
        # Skip if any FAQ-shaped section already exists (model may have written one).
        faq_patterns = [
            r'##\s*Frequently Asked Questions',
            r'##\s*FAQ',
            r'##\s*Your Top Questions',
            r'##\s*Common Questions',
            r'##\s*Questions (?:and Answers|& Answers)',
            r'##\s*Q&A',
            r'###\s*[^#\n]+\?',
        ]
        for pattern in faq_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                logger.debug("FAQ section already present in body; skipping duplicate render")
                return content

        entries = getattr(self, '_unified_faq_entries', None)
        if not entries:
            return content

        faq_config = self.brand_voice_config.get('faq_heading_format', {})
        faq_style = faq_config.get('style', 'contextual')
        faq_options = faq_config.get('options', {
            'contextual': 'Your Top Questions About {keyword}',
            'standard': 'Frequently Asked Questions',
        })
        if faq_style == 'contextual' and keyword:
            heading = faq_options.get('contextual', 'Your Top Questions About {keyword}').format(keyword=keyword.title())
        else:
            heading = faq_options.get('standard', 'Frequently Asked Questions')

        qa_section = render_faq_body_section(entries, heading)
        if not qa_section:
            return content

        conclusion_pos = content.rfind("## Conclusion")
        if conclusion_pos > 0:
            return content[:conclusion_pos] + qa_section + "\n\n" + content[conclusion_pos:]
        return content + qa_section

    def _extract_answer_for_question(self, content: str, question: str) -> str:
        """Extract a relevant 40-60 word answer for a question with internal linking."""
        # Extract question keywords
        keywords = re.findall(r'\w+', question.lower())
        keywords = [kw for kw in keywords if len(kw) > 3]  # Filter short words

        paragraphs = content.split('\n\n')
        best_para = ""
        best_score = 0
        best_position = 999

        for idx, para in enumerate(paragraphs):
            # Filter out invalid paragraphs
            if len(para) < 100:  # Minimum content length
                continue
            if para.startswith('#'):  # Skip headings
                continue
            if para.startswith('---'):  # Skip frontmatter
                continue
            if 'import ' in para or '<' in para[:10]:  # Skip imports and components
                continue
            if para.startswith('title:') or para.startswith('description:'):  # Skip frontmatter fields
                continue

            # Score based on keyword matches
            keyword_score = sum(1 for kw in keywords if kw in para.lower())

            # Bonus for quality indicators
            quality_score = 0
            if '**' in para:  # Has bold text
                quality_score += 1
            if any(marker in para for marker in ['*   ', '- ', '1. ', '2. ']):  # Has bullets/lists
                quality_score += 1
            if 'for example' in para.lower() or 'e.g.' in para.lower():  # Has examples
                quality_score += 2

            # Combined score with position weight (earlier = better)
            total_score = (keyword_score * 3) + quality_score - (idx * 0.1)

            if total_score > best_score:
                best_score = total_score
                best_para = para
                best_position = idx

        if best_para and best_score > 2:
            # Clean but preserve structure
            answer = best_para.strip()

            # Add intelligent internal links
            answer = self._inject_faq_internal_links(answer)

            return self._normalize_atomic_answer(answer, question)

        fallback = f"{question.replace('?', '')} requires a structured workflow with clear inputs, ownership, and measurable checkpoints. "
        brand_name = self.brand_profile.company_name
        if brand_name and self.site_context and 'internal_links' in self.site_context:
            brand_url = self.site_context['internal_links'].get(brand_name, self.brand_profile.site_url or '/')
            fallback += f"Solutions like [{brand_name}]({brand_url}) streamline this workflow with built-in automation and clear visibility."
        else:
            fallback += "Modern tooling streamlines this workflow by automating repetitive checks and surfacing real-time results."

        return self._normalize_atomic_answer(fallback, question)

    def _fallback_atomic_answer(self, question: str) -> str:
        """Generate a deterministic 40-60 word topic-neutral fallback answer."""
        subject = question.replace('?', '').strip() or "This approach"
        domain_phrase = (
            f"within {self.brand_profile.category}"
            if self.brand_profile.is_configured() and self.brand_profile.category
            else "across real production workflows"
        )
        answer = (
            f"{subject} works best when teams align clear inputs, measurable success criteria, and ownership {domain_phrase}. "
            "Most teams run into friction when critical steps stay fragmented across disconnected tools, delaying feedback and masking trade-offs. "
            "Start by baselining current performance, documenting constraints, and reviewing measurable outcomes on a recurring cadence."
        )
        return self._normalize_atomic_answer(answer, question)

    def _normalize_atomic_answer(self, answer: str, question: str = "") -> str:
        """Normalize FAQ answers toward concise, snippet-friendly coverage."""
        min_words = self.seo_config.get('faq_rules', {}).get('answer_word_count', {}).get('min', 40)
        max_words = self.seo_config.get('faq_rules', {}).get('answer_word_count', {}).get('max', 60)

        answer = re.sub(r'\s+', ' ', answer).strip()
        answer = re.sub(r'^[-*]\s+', '', answer)
        answer = re.sub(r'^#{1,6}\s+', '', answer)

        words = answer.split()
        if len(words) > max_words:
            clipped = ' '.join(words[:max_words])
            sentence_end = max(clipped.rfind('.'), clipped.rfind('!'), clipped.rfind('?'))
            if sentence_end > 0 and len(clipped[:sentence_end + 1].split()) >= min_words:
                answer = clipped[:sentence_end + 1]
            else:
                answer = clipped.rstrip('.,;:') + '.'

        words = answer.split()
        if len(words) < min_words:
            supplemental = self._fallback_atomic_answer(question) if question else (
                "This matters because answer engines prefer concise, sourced explanations that state the answer first and add clear business context."
            )
            supplemental_words = supplemental.split()
            if answer and supplemental.startswith(answer):
                answer = supplemental
            else:
                answer = f"{answer.rstrip('.')}." if answer else ""
                needed = max_words - len(answer.split())
                answer = f"{answer} {' '.join(supplemental_words[:needed])}".strip()

        words = answer.split()
        if len(words) > max_words:
            answer = ' '.join(words[:max_words]).rstrip('.,;:') + '.'

        return answer.strip()

    def _generate_aeo_questions(self, keyword: str, existing_questions: List[str], count_needed: int, content: str) -> List[str]:
        """
        Generate AEO-optimized questions when PAA data is insufficient.
        Uses AI to create natural language questions based on content and keyword intent.

        2026 AEO Best Practices:
        - Long-tail questions (7+ words)
        - Natural language format
        - Cover all question types: what, how, why, when, where, which
        - Business outcome focused for ICP (sales-led, pipeline-led businesses)
        """
        from .ai_router import SmartAIRouter

        router = SmartAIRouter()

        # Analyze existing questions to identify gaps
        existing_types = {
            'what': sum(1 for q in existing_questions if q.lower().startswith('what')),
            'how': sum(1 for q in existing_questions if q.lower().startswith('how')),
            'why': sum(1 for q in existing_questions if q.lower().startswith('why')),
            'when': sum(1 for q in existing_questions if q.lower().startswith('when')),
            'where': sum(1 for q in existing_questions if q.lower().startswith('where')),
            'which': sum(1 for q in existing_questions if q.lower().startswith('which')),
            'can': sum(1 for q in existing_questions if q.lower().startswith('can')),
            'should': sum(1 for q in existing_questions if q.lower().startswith('should')),
            'is': sum(1 for q in existing_questions if q.lower().startswith('is')),
            'are': sum(1 for q in existing_questions if q.lower().startswith('are')),
            'does': sum(1 for q in existing_questions if q.lower().startswith('does')),
            'do': sum(1 for q in existing_questions if q.lower().startswith('do'))
        }

        # Priority order for missing question types (AEO optimization)
        priority_types = [k for k, v in sorted(existing_types.items(), key=lambda x: x[1])][:5]

        prompt = f"""Generate {count_needed} natural language FAQ questions about "{keyword}" that would appear in Google's "People Also Ask" section.

EXISTING QUESTIONS (avoid duplication):
{chr(10).join(f'- {q}' for q in existing_questions)}

REQUIREMENTS (Google-aligned helpful FAQ coverage):
1. **Natural questions** (7-12 words when useful) that real buyers would ask
2. **Question type diversity** - prioritize underrepresented types: {', '.join(priority_types)}
3. **Outcome focused** - relevant to:
   - {self.brand_profile.icp_reader if self.brand_profile.is_configured() and self.brand_profile.icp_reader else 'Practitioners, engineers, and decision-makers evaluating real implementation trade-offs'}
   - Practical workflows, setup constraints, cost, and measurable outcomes
4. **Natural language** - how real people search ("How do I..." not "Methods for...")
5. **Semantically distinct** - don't overlap with existing questions
6. **Answer-worthy** - each question should have a clear, substantive answer in the content

OUTPUT FORMAT (one per line, no numbering):
- Question 1 here?
- Question 2 here?
- Question 3 here?
"""

        try:
            response = router.gemini.generate_content(prompt)
            raw_text = response.text.strip()

            # Parse questions from response
            questions = []
            for line in raw_text.split('\n'):
                line = line.strip()
                if line.startswith('-') or line.startswith('•') or line.startswith('*'):
                    question = line.lstrip('-•*').strip()
                    # Validate: must end with ?, be 7+ words, not duplicate
                    if question and question.endswith('?') and len(question.split()) >= 7:
                        # Check not too similar to existing
                        if not any(question.lower()[:30] in eq.lower() or eq.lower()[:30] in question.lower()
                                 for eq in existing_questions):
                            questions.append(question)

            logger.info(f"✅ AI-generated {len(questions)} AEO-optimized questions (requested: {count_needed})")
            return questions[:count_needed]

        except Exception as e:
            logger.error(f"❌ Failed to generate AEO questions: {e}")
            fallback_questions = self._fallback_aeo_questions(keyword)
            logger.info(f"⚠️  Using {min(count_needed, len(fallback_questions))} fallback non-prescriptive questions")
            return fallback_questions[:count_needed]

    def _inject_faq_internal_links(self, text: str) -> str:
        """Sparsely add one useful internal link to a FAQ answer when context fits."""
        if self.flyweel_mode == 'none' or not text or '[' in text or '](' in text:
            return text

        if self.link_opportunities:
            return self._add_contextual_links(
                text,
                self.link_opportunities,
                max_total_links=1,
                max_links_per_destination=1,
                require_keyword_overlap=False,
            )

        internal_links = (self.site_context.get('internal_links') or {}) if self.site_context else {}
        brand_name = self.brand_profile.company_name
        brand_url = internal_links.get(brand_name) if brand_name else None
        if isinstance(brand_url, dict):
            brand_url = brand_url.get('url')
        if brand_name and brand_url and brand_name in text and re.search(r'\b(platform|tool|software|product)\b', text, re.IGNORECASE):
            return text.replace(brand_name, f'[{brand_name}]({brand_url})', 1)
        return text

    def _add_comparison_table(self, content: str, keyword: str) -> str:
        """Add comparison table component for comparison content"""
        if "[Comparison table will be inserted here]" in content:
            # Generate comparison table component
            table = self._generate_comparison_component(keyword)
            content = content.replace("[Comparison table will be inserted here]", table)

        return content
    
    def _generate_comparison_component(self, keyword: str) -> str:
        """Generate a comparison table strictly from verified BrandProfile / site_context facts, else empty."""
        if self.flyweel_mode == 'none' or not self.brand_profile.is_configured():
            return ""

        rows: List[Tuple[str, str, str]] = []
        if self.brand_profile.category:
            rows.append(("Primary Category", self.brand_profile.category, "Varies by vendor"))

        primary_product = self.brand_profile.products[0] if self.brand_profile.products else None
        if primary_product and primary_product.pricing_facts:
            rows.append(("Stated Pricing", primary_product.pricing_facts, "See vendor pricing"))

        if self.site_context and 'integrations' in self.site_context:
            total = 0
            if isinstance(self.site_context['integrations'], dict):
                total = sum(len(items) for items in self.site_context['integrations'].values())
            elif isinstance(self.site_context['integrations'], list):
                total = len(self.site_context['integrations'])
            if total > 0:
                rows.append(("Verified Integrations", f"{total} documented", "Varies by vendor"))

        if primary_product and primary_product.key_features:
            rows.append(("Verified Capabilities", ", ".join(primary_product.key_features[:3]), "See vendor documentation"))

        if not rows:
            return ""

        competitor_name = "Alternative"
        if " vs " in keyword.lower():
            items = keyword.lower().split(" vs ")
            competitor_name = items[1].strip().title() if len(items) > 1 else "Alternative"
        elif " versus " in keyword.lower():
            items = keyword.lower().split(" versus ")
            competitor_name = items[1].strip().title() if len(items) > 1 else "Alternative"
        elif " or " in keyword.lower():
            items = keyword.lower().split(" or ")
            competitor_name = items[1].strip().title() if len(items) > 1 else "Alternative"

        brand_col = self.brand_profile.company_name
        lines = [
            "",
            f"| Dimension | {brand_col} | {competitor_name} |",
            "|---|---|---|",
            *(f"| {dim} | {ours} | {theirs} |" for dim, ours, theirs in rows),
            "",
        ]
        return "\n".join(lines)

    def _cta_href_and_label(self) -> Tuple[str, str]:
        href = self.brand_profile.signup_or_cta_url or self.brand_profile.site_url or SIGNUP_URL
        label = self.brand_profile.cta_label or "Start Free"
        return href, label

    def _add_signup_cta(self, content: str) -> str:
        """Insert optional CTA Button before Conclusion/FAQ/Citations (skipped in 'none' mode)."""
        if self.flyweel_mode == 'none':
            return content
        href, label = self._cta_href_and_label()
        content = _BANNED_CTA_LABEL.sub(rf'\1{label}\2', content)
        if re.search(r'<Button\b[^>]*' + re.escape(href), content):
            return content

        cta_section = f"""

<div class="not-prose mt-4">
  <Button variant="figma-v1" href="{href}" icon="pixelarticons:trending-up">{label}</Button>
</div>

"""

        # Before the Conclusion, else before the FAQ section, else before CitationsSection, else at the end.
        conclusion = list(_CONCLUSION_H2.finditer(content))
        faq = find_faq_section(content)
        citations_pos = content.find("<CitationsSection")
        at = (conclusion[-1].start() if conclusion
              else (faq[0] if faq else (citations_pos if citations_pos > 0 else None)))
        if at:
            content = content[:at] + cta_section + content[at:]
        else:
            content += cta_section

        return content

    def ensure_signup_cta(self, mdx_content: str) -> str:
        """Re-assert the signup Button + its import on the finished MDX.

        The CTA is inserted before polish, so the later LLM passes (polish,
        heading/title SEO) can drop, mangle or strand it (a heading renamed after
        placement leaves it at the end). Idempotent: strip, then re-place.
        """
        content = _BANNED_CTA_LABEL.sub(r'\1Start Free\2', mdx_content)
        content = _SIGNUP_BUTTON_BLOCK.sub('\n\n', content)
        content = self._add_signup_cta(content)
        content = re.sub(r'\n{3,}(<div class="not-prose)', r'\n\n\1', content)
        content = re.sub(r'(</Button>\s*</div>)\n{3,}', r'\1\n\n', content)
        button_import = "import Button from '@/components/ui/Button.astro';"
        if '<Button' in content and button_import not in content:
            fm = re.match(r'---\n.*?\n---\n', content, re.DOTALL)
            at = fm.end() if fm else 0
            content = content[:at] + button_import + '\n' + content[at:]
        return content

    @staticmethod
    def _compute_page_taxonomy(
        keyword: str,
        style: str,
        brand_profile: Optional["BrandProfile"] = None,
    ) -> Tuple[str, str, str]:
        """Compute generic `(product, funnel_stage, topic)` metadata."""
        from core.brand_profile import load_brand_profile
        profile = brand_profile if brand_profile is not None else load_brand_profile()
        kw = (keyword or '').lower()
        product = 'core'
        for p in profile.products:
            if any(t.lower() in kw for t in ([p.name] + list(p.trigger_keywords))):
                product = p.id
                break
        funnel = ('awareness' if style == 'news'
                  else 'decision' if style in ('comparison', 'top-compare', 'feature') else 'consideration')
        topic = re.sub(r'[^a-z0-9]+', '-', (profile.category or 'general').lower()).strip('-') or 'general'
        return product, funnel, topic

    @staticmethod
    def ensure_page_taxonomy(
        mdx_content: str,
        keyword: str,
        style: str,
        brand_profile: Optional["BrandProfile"] = None,
    ) -> str:
        """Populate optional `product`/`funnel_stage`/`topic` frontmatter keys if missing."""
        fm = re.match(r'---\n(.*?\n)---\n', mdx_content, re.DOTALL)
        if not fm:
            return mdx_content
        product, funnel, topic = AstroFormatter._compute_page_taxonomy(keyword, style, brand_profile=brand_profile)
        head = fm.group(1)
        missing = ''.join(f'{k}: "{v}"\n' for k, v in
                          (('product', product), ('funnel_stage', funnel), ('topic', topic))
                          if not re.search(rf'^{k}:', head, re.MULTILINE))
        return '---\n' + head + missing + '---\n' + mdx_content[fm.end():] if missing else mdx_content

    @staticmethod
    def _strip_body_faq_section(body: str) -> str:
        """Remove duplicate explicit body FAQ section during frontmatter harvesting."""
        if not body:
            return body
        cleaned = body
        for _ in range(3):
            span = find_faq_section(cleaned, explicit_only=True)
            if not span:
                break
            cleaned = (cleaned[:span[0]].rstrip() + "\n\n" + cleaned[span[1]:].lstrip()).strip() + "\n"
        return cleaned

    @staticmethod
    def sync_faq_frontmatter(mdx_content: str) -> str:
        """Harvest any body FAQ section into frontmatter ``faq:`` and deduplicate body FAQ."""
        fm = re.match(r'---\n(.*?\n)---\n', mdx_content, re.DOTALL)
        if not fm:
            return mdx_content
        body = mdx_content[fm.end():]
        entries = extract_body_faq(body, explicit_only=True)
        head = fm.group(1)
        if entries:
            cleaned_items = [
                {
                    "question": AstroFormatter._clean_plain_text_field(e.question),
                    "answer": AstroFormatter._clean_plain_text_field(e.short_answer),
                }
                for e in entries
                if AstroFormatter._clean_plain_text_field(e.question)
                and AstroFormatter._clean_plain_text_field(e.short_answer)
            ]
            if cleaned_items:
                items = yaml.safe_dump(cleaned_items, sort_keys=False, allow_unicode=True, width=10**6)
                block = 'faq:\n' + ''.join('  ' + ln for ln in items.splitlines(True))
                existing = re.search(r'^faq:[^\n]*\n(?:(?:[ \t-].*|[ \t]*)\n)*', head, re.MULTILINE)
                head = head[:existing.start()] + block + head[existing.end():] if existing else head + block
        stripped_body = AstroFormatter._strip_body_faq_section(body)
        return '---\n' + head + '---\n\n' + stripped_body.lstrip('\n')

    def _add_internal_links(self, content: str) -> str:
        """Add sparse, relevant internal links using editorial candidate scoring."""
        if self.flyweel_mode == 'none':
            return content

        raw_internal_links = (self.site_context or {}).get('internal_links') if self.site_context else None
        if not raw_internal_links:
            raw_internal_links = {}
            if self.brand_profile.company_name:
                raw_internal_links[self.brand_profile.company_name] = "/"
            for prod in self.brand_profile.products:
                if prod.name and prod.url:
                    raw_internal_links[prod.name] = prod.url

        internal_links: Dict[str, str] = {}
        for term, value in (raw_internal_links or {}).items():
            url = value.get('url') if isinstance(value, dict) else str(value)
            if term and url:
                internal_links[str(term)] = self._to_relative_internal_url(url)

        if self.link_opportunities:
            content = self._add_contextual_links(content, self.link_opportunities)

        for term, url in internal_links.items():
            if self._is_generic_anchor(term):
                continue
            high_prio_terms = {'pricing', 'integrations', 'documentation', 'docs', 'features', 'product'}
            if self.brand_profile.company_name:
                high_prio_terms.add(self.brand_profile.company_name.lower())
            for p in self.brand_profile.products:
                if p.name:
                    high_prio_terms.add(p.name.lower())
            candidate = {
                'url': url,
                'title': term,
                'anchor_text': term,
                'type': 'page',
                'priority': 8 if term.lower() in high_prio_terms else 5,
            }
            candidate['destination_tokens'] = self._candidate_destination_tokens(candidate)
            content = self._add_contextual_links(
                content,
                {term.lower(): candidate},
                max_total_links=1,
                max_links_per_destination=1,
                require_keyword_overlap=False,
            )

        return content

    def _line_bounds_for_index(self, content: str, index: int) -> Tuple[int, int]:
        line_start = content.rfind('\n', 0, index) + 1
        line_end = content.find('\n', index)
        if line_end == -1:
            line_end = len(content)
        return line_start, line_end

    def _section_heading_before(self, content: str, index: int) -> str:
        headings = re.findall(r'(?m)^#{2,4}\s+(.+)$', content[:index])
        return headings[-1] if headings else ''

    def _surrounding_sentence(self, content: str, start: int, end: int, radius: int = 180) -> str:
        left = max(0, start - radius)
        right = min(len(content), end + radius)
        return re.sub(r'\s+', ' ', content[left:right]).strip()

    @staticmethod
    def _code_and_import_spans(content: str) -> List[Tuple[int, int]]:
        """Return character spans for frontmatter, MDX import/export lines, fenced code, and inline code."""
        from .mdx_security import markdown_code_spans

        fm = re.match(r'^---\r?\n.*?\r?\n---\r?\n', content, re.DOTALL)
        pos = fm.end() if fm else 0
        spans: List[Tuple[int, int]] = [(0, pos)] if pos else []
        spans.extend(markdown_code_spans(content, start=pos))
        for m in re.finditer(r'(?m)^\s*(?:import|export)\s+[^\r\n]*', content[pos:]):
            spans.append((pos + m.start(), pos + m.end()))
        return spans

    def _inside_existing_link(self, content: str, start: int, end: int) -> bool:
        line_start, line_end = self._line_bounds_for_index(content, start)
        line = content[line_start:line_end]
        rel_start = start - line_start
        before = line[:rel_start]
        after = line[end - line_start:]
        in_tag = before.rfind('<') > before.rfind('>')  # e.g. inside an href="..." attribute
        in_url = before.rfind('](') > before.rfind(')')  # inside a markdown link destination
        in_bold = before.count('**') % 2 == 1  # would split a **bold term**
        return in_tag or in_url or in_bold or (before.rfind('[') > before.rfind('](')) or ('](' in after and '[' in before) or ('<a ' in before.lower() and '</a>' in after.lower())

    def _is_self_link_url(self, url: str) -> bool:
        current = self._normalize_link_url(getattr(self, '_current_article_url', ''))
        return bool(current and self._normalize_link_url(url) == current)

    def _score_link_candidate(self, anchor: str, candidate: Dict[str, Any], context: str, heading: str) -> int:
        if self._is_generic_anchor(anchor):
            return -10
        if self._is_self_link_url(candidate.get('url', '')):
            return -10

        anchor_tokens = set(self._tokenize_link_text(anchor))
        context_tokens = set(self._tokenize_link_text(f"{heading} {context} {self._current_keyword}"))
        destination_tokens = set(candidate.get('destination_tokens') or self._candidate_destination_tokens(candidate))
        term_tokens = set(candidate.get('term_tokens') or self._tokenize_link_text(candidate.get('anchor_text', anchor)))

        score = int(candidate.get('priority', 0))
        score += min(8, len(anchor_tokens) * 2)
        score += len(anchor_tokens & destination_tokens) * 4
        score += len(context_tokens & destination_tokens) * 3
        score += len(context_tokens & term_tokens) * 2

        high_intent = {'pricing', 'integration', 'integrations', 'product', 'features', 'platform', 'docs'}
        if anchor_tokens & high_intent:
            score += 6
        if len(anchor_tokens) == 1 and not (anchor_tokens & high_intent):
            score -= 7
        if not ((anchor_tokens | context_tokens) & destination_tokens) and not (anchor_tokens & high_intent):
            score -= 8
        return score

    _JEV_LINK_QUESTIONS = {
        # Word sense: the verb "close" is not the Close CRM (0.54 vs 0.92 live).
        "same_thing": {"type": "noul", "instructions": (
            "In the passage, the anchor text refers to the same thing the destination page is about "
            "(not just the same word used with a different meaning).")},
        "useful": {"type": "noul", "instructions": (
            "A reader of the passage would find the destination page a useful next step "
            "for the specific idea named by the anchor text.")},
    }

    def _jev_filter_link_candidates(self, candidates: List[Tuple], states: List[Dict[str, str]]) -> List[Tuple]:
        """Keep candidates Jev rates same-meaning >= 0.7 and useful >= 0.6,
        ordered by the weaker of the two.

        0.6, not higher: link metadata is thin (title often just repeats the
        anchor), and on a live run good links scored 0.6-0.8 while junk
        ("close" -> the Close CRM integration, "dashboard" -> pricing) scored
        0.15-0.57.

        Unjudged candidates fall back to the plain ``score >= 9`` rule, so a
        Jev outage reproduces the pre-Jev link set.
        """
        cache = self.__dict__.setdefault('_jev_link_cache', {})
        keys = [json.dumps(st, sort_keys=True) for st in states]
        todo = [i for i, k in enumerate(keys) if k not in cache]
        if todo:
            answers = jev.ask_many_sync([states[i] for i in todo], self._JEV_LINK_QUESTIONS)
            for i, a in zip(todo, answers):
                same, useful = jev.noul(a, "same_thing"), jev.noul(a, "useful")
                cache[keys[i]] = (None if same is None or useful is None
                                  else (min(same, useful) if same >= 0.7 and useful >= 0.6 else 0.0))
        kept = []
        for cand, key in zip(candidates, keys):
            p = cache.get(key)
            if (p is not None and p > 0) or (p is None and cand[0] >= 9):
                kept.append((p if p is not None else 0.0, cand))
        kept.sort(key=lambda item: (-item[0], -item[1][0], -item[1][1], item[1][2]))
        return [cand for _p, cand in kept]

    def _add_contextual_links(
        self,
        content: str,
        link_map: Dict[str, Dict],
        *,
        max_total_links: Optional[int] = None,
        max_links_per_destination: int = 2,
        require_keyword_overlap: bool = True,
    ) -> str:
        """Add internal links only when anchor, context, and destination fit."""
        if not content or not link_map:
            return content

        if max_total_links is None:
            words = max(1, len(content.split()))
            max_total_links = max(4, min(12, words // 450 + 4))

        brand_host = self.brand_profile.domain_host()
        internal_pat = (
            rf'\]\((?:https?://(?:www\.|[a-z0-9-]+\.)?{re.escape(brand_host)}|/[a-zA-Z0-9_/-]*)'
            if brand_host
            else r'\]\(/[a-zA-Z0-9_/-]*'
        )
        existing_internal = len(re.findall(internal_pat, content))
        remaining = max(0, max_total_links - existing_internal)
        if remaining <= 0:
            return content

        destination_counts: Dict[str, int] = {}
        for url in re.findall(r'(?<!!)\[[^\]]+\]\(((?:https?://[^\s\)]+|/[^\s\)]+))\)', content):
            norm = self._normalize_link_url(url)
            destination_counts[norm] = destination_counts.get(norm, 0) + 1

        # With Jev on, the token-overlap score is only a loose prefilter (>= 5)
        # and Jev decides relevance; otherwise the score alone decides (>= 9).
        use_jev = jev.enabled()
        excluded_spans = self._code_and_import_spans(content)
        candidates: List[Tuple[int, int, int, str, Dict[str, Any], Any]] = []
        link_states: List[Dict[str, str]] = []
        for term, link_data in link_map.items():
            if not term or not isinstance(link_data, dict):
                continue
            pattern = re.compile(r'\b' + re.escape(term) + r'\b', re.IGNORECASE)
            for match in pattern.finditer(content):
                if any(s < match.end() and match.start() < e for s, e in excluded_spans):
                    continue
                line_start, _ = self._line_bounds_for_index(content, match.start())
                if content[line_start:match.start()].strip().startswith('#'):
                    continue
                if self._inside_existing_link(content, match.start(), match.end()):
                    continue
                anchor = match.group(0)
                # Integration names are proper nouns: a lowercase one-word match
                # is the common word ("take time to close" is not Close CRM).
                if ' ' not in anchor and anchor.islower() and '/integrations/' in str(link_data.get('url', '')):
                    continue
                context = self._surrounding_sentence(content, match.start(), match.end())
                heading = self._section_heading_before(content, match.start())
                score = self._score_link_candidate(anchor, link_data, context, heading)
                keyword_tokens = set(self._tokenize_link_text(self._current_keyword))
                local_tokens = set(self._tokenize_link_text(f"{heading} {context}"))
                if require_keyword_overlap and keyword_tokens and not (keyword_tokens & local_tokens):
                    score -= 3
                if score >= (5 if use_jev else 9):
                    candidates.append((score, len(anchor), match.start(), anchor, link_data, match))
                    link_states.append({
                        "passage": f"{heading}\n{context}".strip(),
                        "anchor": anchor,
                        "destination": " - ".join(dict.fromkeys(filter(None, (
                            link_data.get('title'), link_data.get('description'), link_data.get('url'))))),
                    })

        if use_jev and candidates:
            candidates = self._jev_filter_link_candidates(candidates, link_states)
        else:
            candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))

        replacements: List[Tuple[int, int, str, str]] = []
        occupied: List[Tuple[int, int]] = []
        used_keys = set()
        for _score, _length, _pos, anchor, link_data, match in candidates:
            if len(replacements) >= remaining:
                break
            url = link_data.get('url')
            if not url:
                continue
            norm_url = self._normalize_link_url(url)
            if destination_counts.get(norm_url, 0) >= max_links_per_destination:
                continue
            span = (match.start(), match.end())
            if any(not (span[1] <= taken[0] or span[0] >= taken[1]) for taken in occupied):
                continue
            key = (norm_url, anchor.lower())
            if key in used_keys:
                continue
            try:
                rel_url = self._to_relative_internal_url(str(url))
                safe_url = sanitize_content_url(rel_url)
            except MdxSecurityError:
                continue
            replacements.append((
                span[0], span[1],
                f"[{escape_markdown_link_text(anchor)}]({safe_url})",
                norm_url,
            ))
            occupied.append(span)
            used_keys.add(key)
            destination_counts[norm_url] = destination_counts.get(norm_url, 0) + 1

        for start, end, replacement, _url in sorted(replacements, key=lambda item: item[0], reverse=True):
            content = content[:start] + replacement + content[end:]

        return content

    @classmethod
    def normalize_internal_links(
        cls,
        mdx_content: str,
        *,
        max_per_destination: int = 2,
        brand_profile: Optional[Any] = None,
    ) -> str:
        """Convert absolute configured-brand links in the body to relative paths and cap repeats at 2."""
        if not mdx_content:
            return mdx_content
        from core.brand_profile import load_brand_profile
        profile = brand_profile or load_brand_profile()
        host = profile.domain_host()
        fm = re.match(r'^(---\n.*?\n---\n)(.*)$', mdx_content, re.DOTALL)
        head, body = (fm.group(1), fm.group(2)) if fm else ("", mdx_content)

        if host:
            esc_host = re.escape(host)

            def _html_to_rel(m: "re.Match") -> str:
                path = (m.group(1) or "/").rstrip("/") or "/"
                inner = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                return f"[{inner}]({path})" if inner else ""

            body = re.sub(
                rf'<a\s+[^>]*href=["\']https?://(?:www\.)?{esc_host}(/[^"\']*)?["\'][^>]*>(.*?)</a>',
                _html_to_rel,
                body,
                flags=re.IGNORECASE | re.DOTALL,
            )

            def _md_to_rel(m: "re.Match") -> str:
                anchor = m.group(1)
                path = (m.group(2) or "/").rstrip("/") or "/"
                return f"[{anchor}]({path})"

            body = re.sub(
                rf'(?<!!)\[([^\]]+)\]\(https?://(?:www\.)?{esc_host}(/[^\s\)]*)?\)',
                _md_to_rel,
                body,
                flags=re.IGNORECASE,
            )

        counts: Dict[str, int] = {}

        def _cap_repeats(m: "re.Match") -> str:
            anchor, path = m.group(1), m.group(2)
            norm = (path.rstrip("/") or "/").lower()
            counts[norm] = counts.get(norm, 0) + 1
            if counts[norm] > max_per_destination:
                return anchor
            return f"[{anchor}]({path.rstrip('/') or '/'})"

        body = re.sub(r'(?<!!)\[([^\]]+)\]\((/[a-zA-Z0-9_/-]*)\)', _cap_repeats, body)
        return head + body

    @classmethod
    def apply_compliance_footnotes(cls, mdx_content: str, brand_profile: Optional["BrandProfile"] = None) -> str:
        """Apply optional BrandProfile compliance_footnotes (no-op when none are configured)."""
        if not mdx_content:
            return mdx_content
        from core.brand_profile import load_brand_profile
        profile = brand_profile if brand_profile is not None else load_brand_profile()
        footnotes = profile.compliance_footnotes
        if not footnotes:
            return mdx_content
        fm = re.match(r'^(---\n.*?\n---\n)(.*)$', mdx_content, re.DOTALL)
        head, body = (fm.group(1), fm.group(2)) if fm else ("", mdx_content)
        for fn in footnotes:
            if fn.trigger_pattern and fn.marker_html and fn.marker_html not in body:
                body = re.sub(
                    fn.trigger_pattern,
                    lambda m, marker=fn.marker_html: m.group(0) + marker,
                    body,
                    count=1,
                    flags=re.IGNORECASE,
                )
        return head + body

    def _extract_topic_from_url(self, url: str) -> str:
        """Extract readable topic from Reddit/Quora URL for better citation context"""
        # Extract from Reddit: /r/PPC/comments/abc/topic-name/ → "r/PPC: Topic Name"
        reddit_match = re.search(r'/r/(\w+)/comments/\w+/([^/]+)', url)
        if reddit_match:
            subreddit = reddit_match.group(1)
            topic = reddit_match.group(2).replace('-', ' ').replace('_', ' ').title()
            return f"r/{subreddit}: {topic[:60]}"

        # Extract from Quora: /What-is-the-best-way → "What is the best way..."
        quora_match = re.search(r'/([A-Z][^/]+)$', url)
        if quora_match:
            topic = quora_match.group(1).replace('-', ' ')[:70]
            return topic

        return ""

    def _extract_source_title(self, citation: dict) -> str:
        """Extract descriptive title for citation with E-E-A-T signals

        Priorities:
        1. Use existing title from research data (Perplexity provides these)
        2. Extract meaningful title from URL patterns
        3. Fallback to domain/platform name
        """
        url = citation.get('url', '')

        # Priority 1: Use existing title if available
        if citation.get('title') and len(citation['title']) > 10:
            title = citation['title']
            # Clean up overly long titles
            if len(title) > 80:
                title = title[:77] + "..."
            return title

        # Priority 2: Extract from URL patterns

        # Reddit: r/SubredditName/comments/id/descriptive-slug
        reddit_match = re.search(r'/r/(\w+)/comments/\w+/([^/]+)', url)
        if reddit_match:
            subreddit = reddit_match.group(1)
            slug = reddit_match.group(2).replace('_', ' ').replace('-', ' ')
            # Capitalize words for readability
            words = slug.split()
            title = ' '.join(word.capitalize() for word in words if len(word) > 2)
            if len(title) > 60:
                title = title[:57] + "..."
            return f"{title} - r/{subreddit}"

        # Quora: /What-is-the-best-way-to-do-X
        quora_match = re.search(r'/([A-Z][^/]+)$', url)
        if quora_match:
            title = quora_match.group(1).replace('-', ' ')
            if len(title) > 70:
                title = title[:67] + "..."
            return title

        # Authority sites: Extract from domain
        domain_match = re.search(r'//(?:www\.)?([^/]+)', url)
        if domain_match:
            domain = domain_match.group(1)

            # Known authority domains with better labels
            authority_domains = {
                'hubspot.com': 'HubSpot Marketing Resources',
                'searchengineland.com': 'Search Engine Land Industry Report',
                'moz.com': 'Moz SEO Best Practices',
                'marketingland.com': 'Marketing Land Industry Analysis',
                'adweek.com': 'Adweek Marketing Insights',
                'forbes.com': 'Forbes Business Analysis',
                'entrepreneur.com': 'Entrepreneur Industry Report',
                'inc.com': 'Inc. Business Insights',
                'gartner.com': 'Gartner Research Report',
                'forrester.com': 'Forrester Industry Study'
            }

            for domain_key, label in authority_domains.items():
                if domain_key in domain:
                    return label

            # Extract readable name from domain
            clean_domain = domain.replace('www.', '').replace('.com', '').replace('.io', '').replace('.ai', '')
            return clean_domain.title() + ' Industry Resource'

        # Priority 3: Fallback to platform name
        platform = citation.get('platform', 'Industry Source')
        if len(platform) > 70:
            platform = platform[:67] + "..."
        return platform

    def _get_source_type(self, citation: dict) -> str:
        """Get descriptive source type for E-E-A-T credibility signals"""

        url = citation.get('url', '')

        # Check citation type first
        if citation.get('type') == 'serp':
            # Research/authority sources get higher E-E-A-T labels
            if any(domain in url for domain in ['.edu', '.gov', '.org']):
                return "Educational/Government Resource"
            elif any(keyword in url.lower() for keyword in ['research', 'study', 'white-paper', 'whitepaper']):
                return "Research Study"
            elif any(keyword in url.lower() for keyword in ['report', 'benchmark', 'survey', 'data']):
                return "Industry Report"
            elif any(keyword in url.lower() for keyword in ['guide', 'best-practices', 'how-to']):
                return "Expert Guide"
            else:
                return "Industry Analysis"

        # Community sources
        elif 'reddit.com' in url:
            return "Community Discussion"

        elif 'quora.com' in url:
            return "Expert Q&A"

        # Default for unknown types
        else:
            return "Industry Resource"

    def _add_citations(self, content: str, research_data: Dict[str, Any], style: str = "standard") -> str:
        """Add contextual citations with descriptive anchor text

        For research style: Only uses authoritative SERP citations (no community sources)
        For other styles: Uses all citation types (SERP, Reddit, Quora)
        """
        # Collect all citations from research
        all_citations = []

        # Research style: prefer the pre-tiered, de-duplicated research_sources
        # pool (Perplexity SERP + Interfaze, Tier A/B only). When present, skip
        # raw SERP citations below to avoid double-listing weaker duplicates.
        research_pool = (research_data.get('research_sources') or [])
        use_research_pool = style == "research" and bool(research_pool)
        if use_research_pool:
            for s in research_pool:
                url = s.get('url')
                if url and str(url).startswith('http'):
                    all_citations.append({
                        'url': url,
                        'platform': s.get('title') or s.get('source_type', 'Research Source'),
                        'title': s.get('title', ''),
                        'type': 'serp',
                    })

        # Only suppress raw SERP citations when the pool ACTUALLY produced
        # citations. The pool already ingests SERP citations + search_results
        # (Tier-C dropped), so suppression is safe when it is healthy — but an
        # empty / url-less pool must fall back to raw SERP, never ship uncited.
        pool_yielded = use_research_pool and bool(all_citations)

        # For research style, skip community citations - only use authoritative sources
        community_data = (research_data.get('community') or {})
        if style != "research":
            # Extract Reddit citations (only for non-research styles)
            reddit_data = (community_data.get('reddit') or {})
            reddit_citations = reddit_data.get('citations', [])
            for citation in reddit_citations:
                if isinstance(citation, dict) and citation.get('url'):
                    all_citations.append({
                        'url': citation['url'],
                        'platform': citation.get('platform', 'Reddit'),
                        'type': 'community'
                    })

            # Extract Quora citations (only for non-research styles)
            quora_data = (community_data.get('quora') or {})
            quora_citations = quora_data.get('citations', [])
            for citation in quora_citations:
                if isinstance(citation, dict) and citation.get('url'):
                    all_citations.append({
                        'url': citation['url'],
                        'platform': citation.get('platform', 'Quora'),
                        'type': 'community'
                    })

        # Extract SERP/Perplexity citations (from sonar-deep-research/sonar-reasoning-pro)
        # These are highest priority - authoritative research sources
        serp_data = (research_data.get('serp') or {})
        serp_analysis = (serp_data.get('serp_analysis') or {})
        serp_citations = [] if pool_yielded else serp_analysis.get('citations', [])

        for citation in serp_citations:
            # Perplexity citations can be strings (URLs) or dicts
            if isinstance(citation, str):
                all_citations.append({
                    'url': citation,
                    'platform': 'Research Source',
                    'type': 'serp'
                })
            elif isinstance(citation, dict):
                # Extract URL from dict (Perplexity format varies)
                url = citation.get('url') or citation.get('link') or str(citation)
                if url and url.startswith('http'):
                    title = citation.get('title', citation.get('name', 'Research Source'))
                    all_citations.append({
                        'url': url,
                        'platform': title,
                        'type': 'serp'
                    })

        safe_citations = []
        for citation in all_citations:
            try:
                safe_url = sanitize_content_url(str(citation.get("url", "")))
            except MdxSecurityError:
                logger.warning("Dropping citation with an unsafe URL")
                continue
            safe_citations.append({**citation, "url": safe_url})
        all_citations = safe_citations

        if not all_citations:
            return content

        # Find sentences that make claims needing citations
        # Comprehensive patterns optimized for research-style content
        all_citation_patterns = [
            # === RESEARCH TERMINOLOGY (Primary for research style) ===
            r'((?:our|the) analysis (?:reveals|shows|demonstrates|indicates)[^.]+\.)',
            r'(data (?:demonstrates|shows|reveals|indicates|suggests)[^.]+\.)',
            r'((?:research|industry) (?:data|findings|analysis) (?:shows|indicates|reveals|confirms)[^.]+\.)',
            r'(market analysis (?:suggests|shows|reveals|indicates)[^.]+\.)',
            r'((?:quantitative|empirical) (?:assessment|evidence|analysis) (?:indicates|supports|shows)[^.]+\.)',
            r'(comparative evaluation (?:shows|reveals|indicates)[^.]+\.)',

            # === STATISTICAL CLAIMS (High priority for all styles) ===
            r'((?:averaging|average|median) \$?\d+(?:[.,]\d+)?(?:%| (?:per|each|in))?[^.]+\.)',
            r'((?:average|median) [^.]*?\$\d+(?:[.,]\d+)?[^.]+\.)',  # Flexible: average ... $70.11 ... .
            r'(\d+(?:[.,]\d+)?% (?:increase|decrease|reduction|improvement|growth|of|higher|lower)[^.]+\.)',
            r'((?:from |range of )?\$\d+(?:[.,]\d+)? (?:to|-)?\s?\$?\d+(?:[.,]\d+)?[^.]+\.)',
            r'(\d+–\d+% [^.]+\.)',

            # === INDUSTRY/EXPERT ATTRIBUTION ===
            r'(according to [^.]+\.)',
            r'((?:industry|leading|performance) (?:experts|marketers|analysts|professionals) (?:argue|suggest|recommend|agree)[^.]+\.)',
            r'(experts (?:recommend|suggest|agree|argue)[^.]+\.)',

            # === STUDY/RESEARCH REFERENCES ===
            r'(studies (?:show|indicate|reveal|suggest|demonstrate)[^.]+\.)',
            r'(research (?:shows|indicates|reveals|suggests|demonstrates)[^.]+\.)',
            r'((?:a|the) (?:study|survey|report|analysis) (?:found|showed|revealed|indicated)[^.]+\.)',

            # === CLAIM LANGUAGE ===
            r'((?:businesses|companies|advertisers|organizations) (?:report|experience|achieve|see)[^.]+\d+[^.]*\.)',
            r'(this (?:shows|demonstrates|reveals|indicates)[^.]+\.)',
            r'((?:data|evidence|analysis) (?:confirms|validates|supports)[^.]+\.)',

            # === LEGACY PATTERNS (for backward compatibility) ===
            r'(users report[^.]+\.)',
            r'(many (?:users|businesses|companies)[^.]+\.)',
        ]

        # Filter patterns based on style
        if style == "research":
            # Research style uses ALL patterns for maximum coverage
            citation_patterns = all_citation_patterns
        else:
            # Other styles skip research-specific terminology patterns
            citation_patterns = [p for p in all_citation_patterns
                               if not p.startswith(r'((?:our|the) analysis')
                               and not p.startswith(r'((?:quantitative|empirical)')]

        used_citations = []  # Track which citations were actually used
        used_source_idxs: set[int] = set()

        def _pick_supporting_citation(sentence: str) -> Optional[int]:
            sent_tokens = {
                t for t in re.findall(r"[a-z0-9]{3,}", sentence.lower())
                if t not in _CITE_STOPWORDS
            }
            best_idx: Optional[int] = None
            best_score = 0
            for idx, cit in enumerate(all_citations):
                if idx in used_source_idxs:
                    continue
                body_text = f"{cit.get('description', '')} {cit.get('content', '')}".strip()
                full_text = f"{cit.get('title', '')} {body_text} {cit.get('url', '')}".lower()
                cit_tokens = {
                    t for t in re.findall(r"[a-z0-9]{3,}", full_text)
                    if t not in _CITE_STOPWORDS
                }
                overlap = len(sent_tokens & cit_tokens)
                if overlap > best_score:
                    best_score = overlap
                    best_idx = idx
            if best_idx is not None and best_score >= 1:
                return best_idx
            # Only fall back to sequential pool assignment when sources lack snippet bodies
            # (bare Perplexity citation URLs verified downstream by _verify_citations).
            for idx, cit in enumerate(all_citations):
                if idx in used_source_idxs:
                    continue
                has_snippet = bool(f"{cit.get('description', '')} {cit.get('content', '')}".strip())
                if not has_snippet:
                    return idx
            return None

        for pattern in citation_patterns:
            matches = list(re.finditer(pattern, content, re.IGNORECASE))
            for match in matches:
                claim_seg = match.group(1)
                # Preserve existing inline Markdown/HTML links without adding a second citation
                if "](" in claim_seg or "<a " in claim_seg or "citation-ref" in claim_seg:
                    continue
                chosen_idx = _pick_supporting_citation(claim_seg)
                if chosen_idx is not None:
                    used_source_idxs.add(chosen_idx)
                    citation = all_citations[chosen_idx]
                    used_citations.append(citation)
                    citation_number = len(used_citations)

                    # Add numbered inline citation as linked superscript: [1], [2], etc.
                    # Links to corresponding citation at bottom with #cite-N anchor
                    replacement = claim_seg.rstrip('.') + f' <a href="#cite-{citation_number}" class="citation-ref">[{citation_number}]</a>.'
                    content = content.replace(claim_seg, replacement, 1)

        # Fallback for non-research styles when no strict pattern matched: attach [1]
        # only when a source in all_citations (preferring SERP sources) shares meaningful
        # tokens with a claim sentence in the body.
        if not used_citations and all_citations and style != "research":
            prioritized_citations = [c for c in all_citations if c.get("type") == "serp"] + [
                c for c in all_citations if c.get("type") != "serp"
            ]
            candidate_sents = [
                m.group(1)
                for m in re.finditer(r'(?m)^(?![#|>*-]|import\s|<)([A-Z][^.!?\n]{25,}\.)', content)
            ]
            best_pair: Optional[Tuple[str, Dict[str, Any], int]] = None
            for sent in candidate_sents:
                sent_tokens = {
                    t for t in re.findall(r"[a-z0-9]{3,}", sent.lower()) if t not in _CITE_STOPWORDS
                }
                if not sent_tokens:
                    continue
                for cit in prioritized_citations:
                    cit_text = f"{cit.get('title', '')} {cit.get('platform', '')} {cit.get('url', '')}".lower()
                    cit_tokens = {
                        t for t in re.findall(r"[a-z0-9]{3,}", cit_text) if t not in _CITE_STOPWORDS
                    }
                    overlap = len(sent_tokens & cit_tokens)
                    if overlap >= 1:
                        score = overlap * (2 if cit.get("type") == "serp" else 1)
                        if best_pair is None or score > best_pair[2]:
                            best_pair = (sent, cit, score)
            if best_pair is not None:
                old_s, citation, _ = best_pair
                new_s = old_s.rstrip('.') + ' <a href="#cite-1" class="citation-ref">[1]</a>.'
                content = content.replace(old_s, new_s, 1)
                used_citations.append(citation)

        # Add references section if citations were used. For research articles,
        # keep the verified source pool visible even when table-heavy content
        # does not match sentence citation patterns.
        citations_for_references = used_citations
        if use_research_pool and not citations_for_references:
            citations_for_references = all_citations[:20]

        needs_numbered_targets = any(
            f'id="cite-{i}"' not in content and f"id='cite-{i}'" not in content
            for i in range(1, len(used_citations) + 1)
        )
        should_emit_references = bool(citations_for_references) and (
            needs_numbered_targets or ("## Sources" not in content and "## References" not in content)
        )

        if should_emit_references:
            rows_md = ""
            for i, citation in enumerate(citations_for_references, 1):
                if f'id="cite-{i}"' in content or f"id='cite-{i}'" in content:
                    continue
                title = self._extract_source_title(citation)
                safe_title = html.escape(title, quote=False)
                safe_url = html.escape(citation["url"], quote=True)
                source_type = self._get_source_type(citation)
                rows_md += (
                    f'<span id="cite-{i}">[{i}]</span> '
                    f'<a href="{safe_url}" target="_blank" rel="nofollow">{safe_title}</a> '
                    f'- {source_type}  \n'
                )

            if rows_md:
                if "</CitationsSection>" in content:
                    content = content.replace("</CitationsSection>", f"{rows_md}\n</CitationsSection>", 1)
                else:
                    references = (
                        "\n\n<CitationsSection>\n\n"
                        "> This article cites the following authoritative sources:\n\n"
                        f"{rows_md}\n</CitationsSection>\n"
                    )
                    content += references

        return content

    @classmethod
    def reconcile_citations(cls, mdx_content: str) -> str:
        """Ensure <CitationsSection> is at the end of the body and every #cite-N has a matching target."""
        if not mdx_content or '<CitationsSection>' not in mdx_content:
            return mdx_content

        fm = re.match(r'^(---\n.*?\n---\n)(.*)$', mdx_content, re.DOTALL)
        head, body = (fm.group(1), fm.group(2)) if fm else ("", mdx_content)

        cite_block_re = re.compile(r'\n*<CitationsSection>([\s\S]*?)</CitationsSection>\n*', re.IGNORECASE)
        m_block = cite_block_re.search(body)
        if not m_block:
            return mdx_content

        raw_block = m_block.group(1)
        body_without_block = body[:m_block.start()].rstrip() + "\n\n" + body[m_block.end():].lstrip()
        body_without_block = body_without_block.rstrip()

        # Parse existing <span id="cite-N">[N]</span> <a href="...">title</a> - type entries
        span_re = re.compile(
            r'<span\b[^>]*\bid=["\']cite-(\d+)["\'][^>]*>\[\d+\]</span>\s*<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>\s*-\s*([^\n<]+)',
            re.IGNORECASE,
        )
        entries_by_num: Dict[int, Tuple[str, str, str]] = {}
        ordered_entries: List[Tuple[str, str, str]] = []
        for sm in span_re.finditer(raw_block):
            num = int(sm.group(1))
            entry = (sm.group(2).strip(), sm.group(3).strip(), sm.group(4).strip())
            entries_by_num[num] = entry
            ordered_entries.append(entry)

        # Find inline #cite-N refs in body in order of first appearance (attribute-order independent)
        ref_re = re.compile(r'<a\b[^>]*\bhref=["\']#cite-(\d+)["\'][^>]*>\[\d+\]</a>', re.IGNORECASE)
        inline_nums = [int(m.group(1)) for m in ref_re.finditer(body_without_block)]

        # If inline refs reference numbers not in entries_by_num, drop orphan inline refs or map to available entries
        valid_old_nums: List[int] = []
        for n in inline_nums:
            if n in entries_by_num and n not in valid_old_nums:
                valid_old_nums.append(n)

        # If body had no inline refs but CitationsSection has entries, attach [1] only when
        # an entry shares meaningful tokens with a prose sentence in the body.
        if not valid_old_nums and ordered_entries:
            for sent_m in re.finditer(r'(?m)^(?![#|>*-]|import\s|<)([A-Z][^.!?\n]{30,}\.)', body_without_block):
                old_s = sent_m.group(1)
                s_tokens = {t for t in re.findall(r"[a-z0-9]{3,}", old_s.lower()) if t not in _CITE_STOPWORDS}
                matched_entry = next(
                    (
                        ent for ent in ordered_entries
                        if s_tokens & {
                            t for t in re.findall(r"[a-z0-9]{3,}", f"{ent[0]} {ent[1]}".lower())
                            if t not in _CITE_STOPWORDS
                        }
                    ),
                    None,
                )
                if matched_entry is not None:
                    new_s = old_s.rstrip('.') + ' <a href="#cite-1" class="citation-ref">[1]</a>.'
                    body_without_block = body_without_block.replace(old_s, new_s, 1)
                    valid_old_nums = [1]
                    entries_by_num[1] = matched_entry
                    break

        # Build contiguous 1..K mapping
        remap = {old_n: new_i for new_i, old_n in enumerate(valid_old_nums, 1)}

        def _rewrite_ref(m: "re.Match") -> str:
            old_n = int(m.group(1))
            new_n = remap.get(old_n)
            if new_n is None:
                return ""
            return f'<a href="#cite-{new_n}" class="citation-ref">[{new_n}]</a>'

        body_without_block = ref_re.sub(_rewrite_ref, body_without_block)

        final_entries = [entries_by_num[old_n] for old_n in valid_old_nums if old_n in entries_by_num]
        if not final_entries and ordered_entries:
            final_entries = ordered_entries

        new_block = "\n\n<CitationsSection>\n\n> This article cites the following authoritative sources:\n\n"
        for i, (url, title, stype) in enumerate(final_entries, 1):
            new_block += f'<span id="cite-{i}">[{i}]</span> <a href="{url}" target="_blank" rel="nofollow">{title}</a> - {stype}  \n'
        new_block += "\n</CitationsSection>\n"

        cite_import = "import CitationsSection from '@/components/content/CitationsSection.astro';"
        if cite_import not in body_without_block:
            body_without_block = cite_import + "\n\n" + body_without_block.lstrip()

        return head + "\n" + body_without_block.lstrip("\n") + new_block

    def _determine_category_and_list(self, keyword: str, content: str) -> Tuple[str, List[str]]:
        """Determine primary category and categories list from BrandProfile and topic keywords."""
        primary = self.brand_profile.category or "Guides"
        defaults = list(self.brand_profile.default_categories) or [primary]
        combined = f"{keyword} {content[:1200]}".lower()
        if re.search(r'\b(chatgpt|claude|llm|ai agents?|machine learning)\b', combined):
            return "AI", list(dict.fromkeys(["AI", primary]))
        if re.search(r'\b(automat\w*|workflow|cli)\b', combined):
            return "Automation", list(dict.fromkeys(["Automation", primary]))
        if re.search(r'\b(integrat\w*|webhook|api)\b', combined):
            return "Integrations", list(dict.fromkeys(["Integrations", primary]))
        if re.search(r'\b(analytics|benchmark\w*|reporting|dashboard|metrics|roi)\b', combined):
            return "Analytics", list(dict.fromkeys(["Analytics", primary]))
        return primary, defaults

    def _determine_category(self, keyword: str, content: str) -> str:
        """Determine content category"""
        return self._determine_category_and_list(keyword, content)[0]
    
    def _calculate_read_time(self, content: str) -> int:
        """Calculate read time in minutes"""
        words = len(content.split())
        return max(1, round(words / 200))  # 200 words per minute

    def _load_proper_nouns(self) -> Dict[str, str]:
        """Load the proper-noun casing map (cached on the instance)."""
        cached = getattr(self, '_proper_nouns_map', None)
        if cached is not None:
            return cached
        path = Path(__file__).parent.parent / 'config' / 'proper_nouns.json'
        mapping: Dict[str, str] = {}
        if path.exists():
            try:
                with open(path, 'r') as fh:
                    data = json.load(fh)
                overrides = data.get('overrides', {}) if isinstance(data, dict) else {}
                if isinstance(overrides, dict):
                    mapping = {str(k).lower(): str(v) for k, v in overrides.items()}
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load proper_nouns.json: %s", exc)
        self._proper_nouns_map = mapping
        return mapping

    def _normalize_proper_nouns(self, text: str) -> str:
        """Replace title-cased tokens with their canonical casing where known.

        Operates on whole-word boundaries; safe to call on already-cased text.
        """
        mapping = self._load_proper_nouns()
        if not mapping or not text:
            return text

        def _sub(match: 're.Match[str]') -> str:
            token = match.group(0)
            canonical = mapping.get(token.lower())
            return canonical if canonical is not None else token

        return re.sub(r"[A-Za-z][A-Za-z0-9]*", _sub, text)

    def _looks_sentence_like_keyword(self, keyword: str) -> bool:
        """Return True when the keyword reads like a sentence/claim, not a noun phrase."""
        tokens = re.findall(r"[A-Za-z0-9]+", keyword or "")
        if len(tokens) >= 7:
            return True
        return bool(re.search(r"\b(when|while|because|before|after|fastest|dead|lies|drop|drops|leaks|hidden|breaks)\b", (keyword or "").lower()))

    def _normalized_keyword_title(self, keyword: str) -> str:
        """Return keyword text with canonical casing, avoiding blind title-casing of sentence queries."""
        if not keyword:
            return ""
        cleaned = re.sub(r"\s+", " ", keyword).strip()
        if self._looks_sentence_like_keyword(cleaned):
            return self._normalize_proper_nouns(cleaned)
        return self._normalize_proper_nouns(cleaned.title())

    def _shorten_title_phrase(self, title: str, max_len: int = 60) -> str:
        """Shorten at phrase boundaries without leaving dangling fragments."""
        title = re.sub(r"\s+", " ", title or "").strip()
        if len(title) <= max_len:
            return title

        dangling = r"\b(a|an|and|are|for|is|of|or|the|to|with|without)$"

        # A trailing parenthetical modifier is the cheapest thing to give up.
        no_parens = re.sub(r"\s*\([^)]*\)\s*$", "", title).rstrip(',:;-–—')
        if 30 <= len(no_parens) <= max_len:
            return no_parens

        for sep in (": ", " - ", " – ", " — ", "; "):
            if sep in title:
                left, right = title.split(sep, 1)
                # Trim the qualifier word-by-word before discarding it — a
                # deliberate "Primary: Qualifier" shape should survive
                # shortening whenever a trimmed qualifier still fits.
                right_words = right.split(' ')
                while len(right_words) > 1:
                    right_words.pop()
                    candidate = f"{left}{sep}{' '.join(right_words)}".rstrip(',:;-–—')
                    if len(candidate) < 30:
                        break
                    if len(candidate) <= max_len and not re.search(dangling, candidate.lower()):
                        return candidate
                if 30 <= len(left) <= max_len and not re.search(dangling, left.lower()):
                    return left.rstrip(',:;-–—')
                candidate = f"{left}{sep}{right}"[:max_len].rsplit(' ', 1)[0].rstrip(',:;-–—')
                if len(candidate) >= 30 and not re.search(dangling, candidate.lower()):
                    return candidate

        cut = title[:max_len].rsplit(' ', 1)[0].rstrip(',:;-–—')
        while re.search(r"\b(a|an|and|are|for|is|of|or|the|to|with|without)$", cut.lower()):
            next_cut = cut.rsplit(' ', 1)[0].rstrip(',:;-–—')
            if len(next_cut) < 30:
                break
            cut = next_cut
        return cut if len(cut) >= 30 else title[:max_len].rstrip(',:;-–—')

    def _generate_title(self, content: str, keyword: str, style: str) -> str:
        """Generate an SEO/AEO-safe fallback title across article types."""
        kw_title = self._normalized_keyword_title(keyword)

        # The draft's frontmatter title wins, then an H1, then a style template.
        title_match = (
            re.match(r'---\n(?:.*\n)*?title:\s*["\']?(.+?)["\']?\s*\n(?:.*\n)*?---', content)
            or re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        )
        if title_match:
            title = self._normalize_proper_nouns(title_match.group(1).strip())
        else:
            year = datetime.now().year
            sentence_like = self._looks_sentence_like_keyword(keyword)
            if sentence_like:
                # Sentence-like topics are often already the editorial angle; avoid
                # bolting on "What is"/"Guide to" or title-casing them into word salad.
                title = kw_title.rstrip('.!?')
            elif style in {"comparison", "top-compare"}:
                title = f"Best {kw_title} Tools ({year} Reviewed)"
            elif style == "category":
                title = f"Best {kw_title} Tools and Options ({year})"
            elif style == "guide":
                title = f"How to Use {kw_title} Without Guesswork"
            elif style == "research":
                title = f"{kw_title} Benchmarks and Findings ({year})"
            elif style == "news":
                title = f"{kw_title}: What Changed and Why It Matters"
            else:
                title = f"{kw_title} for Better Operator Decisions"

        # Ensure topical coverage without prefix-stacking the full keyword.
        keyword_tokens = [w for w in re.findall(r"\w+", (keyword or "").lower()) if len(w) > 3]
        title_lower = title.lower()
        if keyword_tokens and not any(w in title_lower for w in keyword_tokens):
            anchor = self._normalized_keyword_title(' '.join(keyword_tokens[:3]))
            title = f"{anchor}: {title}"

        return self._shorten_title_phrase(self._normalize_proper_nouns(title), 60)

    def build_seo_title(self, title: str, keyword: str, style: str = "standard") -> str:
        """Build an SEO title distinct from H1 `title` sized within validate_blog_contract's 35-68 rendered budget."""
        suffix = self.brand_profile.effective_title_suffix()
        suffix_len = len(suffix)
        max_base = min(60, max(16, 66 - suffix_len))
        min_base = min(max(10, max_base - 6), max(12, (45 if suffix_len == 0 else 45 - suffix_len)))

        clean_h1 = self._clean_plain_text_field(title)
        if suffix and suffix.strip() in clean_h1:
            clean_h1 = clean_h1.replace(suffix.strip(), "").strip()
        kw_clean = self._clean_plain_text_field(keyword).strip()
        kw_title = self._normalized_keyword_title(kw_clean or clean_h1).rstrip('.!?')
        kw_cap = min(32, max(12, max_base - 6))
        if len(kw_title) > kw_cap:
            kw_title = self._shorten_title_phrase(kw_title, kw_cap)
        if not kw_title:
            kw_title = self._shorten_title_phrase(self.brand_profile.category or "Practical Guide", kw_cap)

        # Case 1: H1 is "How to <Verb> <Keyword>" -> "<Keyword>: How to <Verb> Them / It"
        m_how = re.match(r'^How\s+to\s+([A-Za-z]+)\s+(.+)$', clean_h1, re.IGNORECASE)
        candidates: List[str] = []
        if m_how:
            verb = m_how.group(1).title()
            candidates.append(f"{kw_title}: How to {verb} Them")
            candidates.append(f"{kw_title}: How to {verb} It")

        # Case 2: Extract non-keyword words from H1 for a colon qualifier
        kw_words = {w.lower() for w in re.findall(r'\w+', kw_title)}
        h1_extra = [w for w in clean_h1.split() if re.sub(r'\W+', '', w).lower() not in kw_words]
        if h1_extra:
            extra_phrase = ' '.join(h1_extra[:5]).strip(' ,:;-')
            if extra_phrase:
                candidates.append(f"{kw_title}: {extra_phrase}")

        # Case 3: Style-driven qualifiers
        year = datetime.now().year
        qualifiers = [
            f"How It Works in {year}",
            "Rules, Costs and Setup",
            "How to Set It Up Right",
            "What Operators Should Know",
            f"{year} Operator Breakdown",
            "Key Rules and Trade-Offs",
            "Practical Setup and Rules",
        ]
        for q in qualifiers:
            candidates.append(f"{kw_title}: {q}")

        for cand in candidates:
            cand = self._normalize_proper_nouns(self._clean_plain_text_field(cand))
            if len(cand) > max_base:
                cand = self._shorten_title_phrase(cand, max_base)
            rendered_len = len(cand) + (suffix_len if suffix and suffix.strip() not in cand else 0)
            if min_base <= len(cand) <= max_base and 35 <= rendered_len <= 68 and cand.lower() != clean_h1.lower():
                return cand

        # Deterministic fit into [min_base, max_base] chars, distinct from clean_h1
        base = f"{kw_title}: Rules, Costs and Setup for Teams"
        base = self._normalize_proper_nouns(self._clean_plain_text_field(base))
        if len(base) > max_base:
            base = self._shorten_title_phrase(base, max_base)
        pad_words = ["in", str(year), "for", "Teams", "Guide", "Now"]
        idx = 0
        while len(base) < min_base and idx < len(pad_words):
            trial = f"{base} {pad_words[idx]}"
            if len(trial) <= max_base:
                base = trial
            idx += 1
        if base.lower() == clean_h1.lower():
            alt = f"{kw_title}: Practical Rules & Setup"
            base = self._shorten_title_phrase(alt, max_base) if len(alt) > max_base else alt
        return base[:max_base].rstrip(' ,:;-')

    @classmethod
    def _fit_description_150_160(cls, text: str, keyword: str = "") -> str:
        """Ensure a meta description is plain text, has no em dashes/markdown, and lands in 150-160 chars."""
        desc = cls._clean_plain_text_field(text)
        if not desc:
            kw = cls._clean_plain_text_field(keyword) or "this workflow"
            desc = f"Learn how {kw} works in practice, which trade-offs matter most, and the concrete steps teams use to get measurable results."
        if not desc.endswith(('.', '!', '?')):
            desc = desc.rstrip(',;:') + '.'

        fillers = [
            "Here is what works now.",
            "See the rules and trade-offs.",
            "Built for practical execution.",
            "See how it works in practice.",
            "Compare your options today.",
            "Learn the key steps now.",
        ]
        for filler in fillers:
            if 150 <= len(desc) <= 160:
                break
            if len(desc) + 1 + len(filler) <= 160:
                desc = f"{desc} {filler}"

        # If still < 150 chars, add words up to 150-160 chars
        if len(desc) < 150:
            tail = " Get clear benchmarks, decision criteria, and practical next steps for your workflow."
            needed_room = 159 - len(desc)
            chunk = tail[:needed_room].rsplit(' ', 1)[0].rstrip('.,;:') + '.'
            if len(desc) + len(chunk) >= 150:
                desc = desc + chunk
            else:
                desc = (desc[:-1] + " with clear trade-offs and actionable next steps.").strip()
                while len(desc) < 150:
                    desc = desc[:-1] + " today."

        if len(desc) > 160:
            cut = desc[:160]
            last_term = max(cut.rfind('.'), cut.rfind('!'), cut.rfind('?'))
            if last_term >= 150:
                desc = cut[:last_term + 1]
            else:
                last_space = cut.rfind(' ', 0, 159)
                desc = (cut[:last_space if last_space >= 149 else 159]).rstrip('.,;:') + '.'
        return desc
    
    def _generate_meta_description(
        self,
        content: str,
        keyword: str,
        style: str = "standard"
    ) -> str:
        """
        Generate SEO-optimized meta description based on SEO best practices.

        Rules:
        1. Length: 150-160 chars (never empty, never >160)
        2. Primary keyword in first 50 chars
        3. Compelling hook addressing user pain
        4. End with soft CTA

        Args:
            content: The article content
            keyword: Primary keyword
            style: Content style for context
        """
        # The draft's own frontmatter description wins when it passes the gate.
        fm_desc = re.match(
            r'---\n(?:.*\n)*?description:\s*["\']?(.+?)["\']?\s*\n(?:.*\n)*?---', content)
        if fm_desc:
            is_valid, clean, _ = validate_meta_description(fm_desc.group(1).strip(), keyword)
            if is_valid:
                return self._fit_description_150_160(clean, keyword)

        # Load SEO config for CTA examples
        config_path = Path(__file__).parent.parent / 'config' / 'seo_optimization.json'
        cta_examples = ["Get started free", "See how", "Learn more", "Compare now"]
        hooks = ["Compare the best", "Find out which", "Discover why", "See how to"]

        if config_path.exists():
            try:
                with open(config_path, 'r') as f:
                    seo_config = json.load(f)
                    meta_rules = seo_config.get('meta_description_rules', {})
                    cta_examples = meta_rules.get('cta_examples', cta_examples)
                    hooks = meta_rules.get('hooks', hooks)
            except Exception as e:
                logger.debug("Failed to load meta CTA/hook config; using defaults: %s", e, exc_info=True)

        # Determine content style for hook selection
        is_comparison = style in ['comparison', 'top-compare', 'category']
        is_guide = style in ['guide']

        # Extract first meaningful paragraph (skip headings, frontmatter)
        first_para_match = re.search(r'^(?!#|\*\*|---)[A-Z][^#\n]{50,}', content.strip(), re.MULTILINE)

        if first_para_match:
            raw_description = first_para_match.group(0).strip()
            # Clean up markdown
            raw_description = re.sub(r'\*\*|__|\[|\]|\(|\)|`', '', raw_description)
            raw_description = re.sub(r'\s+', ' ', raw_description).strip()
        else:
            raw_description = ""

        # Build description around a natural intent phrase instead of a chopped keyword fragment.
        keyword_title = self._shorten_title_phrase(self._normalized_keyword_title(keyword), 58)

        # Select an article-type-specific hook without repeating generic filler.
        if is_comparison:
            hook = f"Compare {keyword_title} options with practical selection criteria."
        elif is_guide:
            hook = f"Learn how {keyword_title} works in practice and what to do next."
        elif style == 'research':
            hook = f"See the data, benchmarks, and trade-offs behind {keyword_title}."
        elif style == 'news':
            hook = f"Understand what changed with {keyword_title} and why it matters now."
        else:
            hook = f"Understand {keyword_title} with practical operator context."

        description = hook

        # Append a value-proposition snippet ONLY if it forms a complete
        # sentence (audit F6). The previous implementation truncated the
        # body mid-sentence and then bolted a CTA onto the fragment.
        if raw_description and len(raw_description) > 30:
            # Take the first sentence of the body (up to 90 chars).
            sentence_match = re.match(r'(.{30,90}?[.!?])(?:\s|$)', raw_description)
            if sentence_match:
                snippet = sentence_match.group(1).strip()
                combined = f"{description} {snippet}".strip()
                if len(combined) <= 158:
                    description = combined
                elif len(snippet) <= 158 - len(description) - 1:
                    description = f"{description} {snippet}"

        # CTA: append only if the description currently ends on a complete
        # sentence terminator and there is room. Never glue a CTA onto a
        # truncated fragment (audit F6).
        if is_comparison:
            cta = "Compare now."
        elif is_guide:
            cta = "Get started free."
        else:
            cta = "See how."
        if description.endswith(('.', '!', '?')) and len(description) + len(cta) + 1 <= 160:
            description = f"{description} {cta}"

        # If under the soft minimum, extend with concrete value instead of repeated generic filler.
        if len(description) < 120:
            filler = "Get the decision points, warning signs, and next steps in one place."
            candidate = description.rstrip()
            if not candidate.endswith(('.', '!', '?')):
                candidate += '.'
            candidate = f"{candidate} {filler}".strip()
            if len(candidate) <= 160:
                description = candidate

        # Trim at a sentence boundary if over the hard maximum.
        if len(description) > 160:
            cut = description[:160]
            last_term = max(cut.rfind('.'), cut.rfind('!'), cut.rfind('?'))
            if last_term >= 120:
                description = cut[: last_term + 1]
            else:
                last_space = cut.rfind(' ', 0, 158)
                description = (cut[: last_space if last_space > 0 else 157]).rstrip(',;:') + '.'

        # Final safety net: never empty, never < 50 chars, always grammatical, and fit to 150-160 chars.
        if not description or len(description) < 50:
            description = f"Understand {keyword_title} with practical context, trade-offs, and next steps."

        return self._fit_description_150_160(description, keyword)
    
    def _process_external_links(self, content: str) -> str:
        """
        Process external links for SEO based on best practices.

        Rules:
        1. For NEW content (this formatter is only used for new content):
           - Add rel="nofollow" to external links by default
           - Exception: Official partners, authority sites (academic, government)
        2. For EXISTING content (not handled here):
           - NEVER modify existing link attributes (handled at update time)

        This ensures we control link equity and protect backlink partnerships.
        """
        # Load exception domains from SEO config
        config_path = Path(__file__).parent.parent / 'config' / 'seo_optimization.json'
        dofollow_domains = [
            'github.com',
            'docs.google.com',
            'wikipedia.org',
            'anthropic.com',
        ]
        if self.brand_profile.domain_host():
            dofollow_domains.insert(0, self.brand_profile.domain_host())

        if config_path.exists():
            try:
                with open(config_path, 'r') as f:
                    seo_config = json.load(f)
                    # Add any configured partner domains
                    ext_rules = seo_config.get('external_link_rules', {})
                    new_rules = ext_rules.get('new_articles', {})
                    # Could add partner domains from config here
            except Exception as e:
                logger.debug("Failed to load external-link config; using defaults: %s", e, exc_info=True)

        # Pattern to match markdown links: [text](url)
        # This catches external links (http/https) but not internal (/)
        link_pattern = r'(?<!!)\[([^\]]+)\]\((https?://[^)]+)\)'
        code_spans = markdown_code_spans(content)

        def add_nofollow(match):
            if any(s <= match.start() < e for s, e in code_spans):
                return match.group(0)
            text = match.group(1)
            url = match.group(2)

            try:
                safe_url = sanitize_content_url(url)
            except MdxSecurityError:
                return html.escape(text, quote=False)

            from urllib.parse import urlparse
            host = (urlparse(safe_url).hostname or "").lower().rstrip(".")

            # Check if this domain should keep dofollow
            for domain in dofollow_domains:
                if host == domain or host.endswith("." + domain):
                    return f"[{escape_markdown_link_text(text)}]({safe_url})"

            # For all other external links, we note they should have nofollow
            # In Astro MDX, we use HTML anchor tags for nofollow
            # Return as HTML: <a href="url" target="_blank" rel="nofollow">text</a>
            return (
                f'<a href="{html.escape(safe_url, quote=True)}" target="_blank" '
                f'rel="nofollow">{html.escape(text, quote=False)}</a>'
            )

        # Process external links
        processed = re.sub(link_pattern, add_nofollow, content)

        return processed

    def _generate_feature_byline(self, keyword: str, content: str) -> str:
        """Generate a compelling byline for feature pages (Phase 6.3).

        Bylines are short, benefit-focused statements that appear below the title.
        Examples:
        - "Consolidate all your ad data in one dashboard"
        - "Stop wasting budget on underperforming ads"
        """
        # Extract first benefit-style statement from content
        benefit_patterns = [
            r"helps? (?:you |businesses? )?(.*?)[.\n]",
            r"enables? (?:you |businesses? )?(.*?)[.\n]",
            r"(?:Stop|Start|Finally) (.*?)[.\n]",
            r"Save (?:time|money|hours) (?:on |by )?(.*?)[.\n]"
        ]

        content_first_500 = content[:500].lower()
        for pattern in benefit_patterns:
            match = re.search(pattern, content_first_500, re.IGNORECASE)
            if match:
                byline = match.group(0).strip().rstrip('.')
                if 20 < len(byline) < 100:
                    return byline.capitalize()

        # Fallback to keyword-based byline
        keyword_words = keyword.split()
        if len(keyword_words) >= 2:
            return f"Master {keyword} with powerful automation"

        return f"Powerful {keyword} tools for growing businesses"

    def _extract_feature_list(self, content: str) -> List[Dict[str, str]]:
        """Extract feature list from content for frontmatter (Phase 6.3).

        Returns list of features with name and brief description.
        Used by Astro components for feature grids/cards.
        """
        features = []

        # Look for H3 feature sections
        h3_pattern = r"###\s*(?:Feature \d+:?\s*)?(.+?)[\n]"
        h3_matches = re.findall(h3_pattern, content)

        for match in h3_matches[:6]:  # Max 6 features
            feature_name = match.strip()
            # Skip if it's a generic section name
            if any(skip in feature_name.lower() for skip in ['faq', 'conclusion', 'get started', 'results']):
                continue

            # Try to find description after the heading
            desc_pattern = rf"###\s*(?:Feature \d+:?\s*)?{re.escape(feature_name)}[\n]+\*\*What it does:\*\*\s*(.+?)[\n]"
            desc_match = re.search(desc_pattern, content, re.IGNORECASE)

            description = ""
            if desc_match:
                description = desc_match.group(1).strip()
            else:
                # Fallback: get first sentence after heading
                section_pattern = rf"###\s*(?:Feature \d+:?\s*)?{re.escape(feature_name)}[\n]+([^#\n].+?)[.\n]"
                section_match = re.search(section_pattern, content)
                if section_match:
                    description = section_match.group(1).strip()

            if feature_name and len(feature_name) > 3:
                features.append({
                    'name': feature_name,
                    'description': description[:100] if description else ''
                })

        # If no H3 features found, extract from Key Features section
        if not features:
            key_features_match = re.search(
                r"##\s*Key Features.*?(?=##|$)",
                content,
                re.DOTALL | re.IGNORECASE
            )
            if key_features_match:
                section = key_features_match.group(0)
                bullet_pattern = r"[-*]\s*\*\*(.+?):\*\*\s*(.+?)[\n]"
                bullets = re.findall(bullet_pattern, section)
                for name, desc in bullets[:6]:
                    features.append({
                        'name': name.strip(),
                        'description': desc.strip()[:100]
                    })

        return features

    def _generate_tags(self, keyword: str, content: str, style: str) -> List[str]:
        """Generate SEO-optimized topical tags (max 6, Title Case)."""
        tag_vocab = {
            'automation': 'Automation',
            'integration': 'Integrations',
            'ai': 'AI',
            'analytics': 'Analytics',
            'strategy': 'Strategy',
            'product': 'Product',
            'engineering': 'Engineering',
            'security': 'Security',
            'operations': 'Operations',
            'benchmarks': 'Benchmarks',
        }
        for topic_seed in self.brand_profile.icp_priority_topics[:10]:
            t_clean = topic_seed.strip()
            if t_clean:
                tag_vocab[t_clean.lower()] = self._normalize_proper_nouns(t_clean.title())

        keyword_lower = keyword.lower()
        content_lower = content.lower()[:2000]
        tags: List[str] = []
        if self.brand_profile.category and self.brand_profile.category not in tags:
            tags.append(self.brand_profile.category)

        for key, tag in tag_vocab.items():
            if tag not in tags and re.search(rf'\b{re.escape(key)}\b', keyword_lower):
                tags.append(tag)
                if len(tags) >= 5:
                    break

        if len(tags) < 5:
            relevance_scores = {}
            for key, tag in tag_vocab.items():
                if tag not in tags:
                    count = len(re.findall(rf'\b{re.escape(key)}\b', content_lower))
                    if count > 2:
                        relevance_scores[tag] = count
            for tag in sorted(relevance_scores, key=relevance_scores.get, reverse=True):
                if tag not in tags:
                    tags.append(tag)
                    if len(tags) >= 5:
                        break

        kw_clean = re.sub(
            r'\b(the|a|an|and|or|for|to|in|on|with|by|of|how|what|why|when|where|which|is|are|can|do|does|best|top|guide|definitive|ultimate|complete|everything|need|know|202\d)\b',
            ' ',
            keyword_lower,
        )
        kw_phrases = [p.strip() for p in re.split(r'[,:;/-]+|\s{2,}', kw_clean) if len(p.strip()) >= 4]
        for phrase in kw_phrases:
            words = [w for w in phrase.split() if len(w) >= 3][:3]
            if not words:
                continue
            cand_tag = self._normalize_proper_nouns(' '.join(words).title())
            if cand_tag and cand_tag not in tags and len(cand_tag) <= 28:
                tags.append(cand_tag)
                if len(tags) >= 6:
                    break

        style_tags = {
            'guide': 'Guide',
            'comparison': 'Comparison',
            'research': 'Research',
            'news': 'Industry News',
            'feature': 'Product',
        }
        if len(tags) < 6 and style in style_tags and style_tags[style] not in tags:
            tags.append(style_tags[style])

        for fallback in ('Strategy', 'Best Practices', 'Operations'):
            if len(tags) >= 3:
                break
            if fallback not in tags:
                tags.append(fallback)

        return tags[:6]

    def _resolve_author(self, content: str) -> str:
        """Resolve author from BrandProfile (or frontmatter fallback)."""
        m = re.match(r'---\n(?:.*\n)*?author:\s*["\']?(.+?)["\']?\s*\n(?:.*\n)*?---', content)
        raw = (m.group(1).strip() if m else "") or os.getenv("DEFAULT_AUTHOR", "")
        return self.brand_profile.resolve_author(raw).name

    def _generate_hero_alt(self, title: str, keyword: str, style: str, content: str = "") -> str:
        """Build descriptive hero alt text (never 'Hero image for X')."""
        m = re.search(r'alt:\s*["\']([^"\']{15,})["\']', content[:1200] if content else "")
        if m:
            existing = self._clean_plain_text_field(m.group(1))
            if existing and not existing.lower().startswith("hero image"):
                return existing
        clean_title = self._clean_plain_text_field(title)
        return (
            f"Serif headline reading {clean_title} over a dark green gradient "
            f"with a faint pixel icon pattern"
        )

    def _generate_excerpt(self, body: str, fallback_description: str) -> str:
        """Build a 1-2 sentence plain-text excerpt (<=160 chars) from the article opening."""
        clean_body = self._strip_existing_frontmatter(body)
        for para in re.split(r'\n\s*\n', clean_body):
            s = para.strip()
            if not s or s.startswith(('#', '|', '<', 'import ', 'export ', '```', '-', '*')):
                continue
            plain = self._clean_plain_text_field(s)
            if len(plain) >= 60:
                if len(plain) <= 160:
                    return plain
                cut = plain[:160]
                last_term = max(cut.rfind('.'), cut.rfind('!'), cut.rfind('?'))
                if last_term >= 70:
                    return cut[:last_term + 1]
                last_space = cut.rfind(' ', 0, 157)
                return (cut[:last_space if last_space > 60 else 156]).rstrip('.,;:') + '.'
        return self._clean_plain_text_field(fallback_description)[:160]

    def _select_related_posts(
        self,
        keyword: str,
        current_slug: str,
        topic: str,
        category: str,
        tags: List[str],
        count: int = 3,
    ) -> List[str]:
        """Select up to `count` related live blog slugs from connected site context."""
        from core.paths import blog_content_path
        posts = list((self.site_context or {}).get('blog_posts') or [])
        if len(posts) < count:
            blog_dir = blog_content_path()
            if blog_dir.exists():
                for mdx_file in sorted(blog_dir.glob("*.md*")):
                    if mdx_file.name.startswith("_"):
                        continue
                    try:
                        raw = mdx_file.read_text(encoding="utf-8")
                        parts = raw.split("---", 2)
                        fm = yaml.safe_load(parts[1]) if len(parts) >= 3 else {}
                        if not isinstance(fm, dict) or fm.get("draft", False):
                            continue
                        posts.append({
                            "slug": mdx_file.stem,
                            "title": fm.get("title", ""),
                            "description": fm.get("description", ""),
                            "tags": fm.get("tags", []),
                            "category": fm.get("category", ""),
                            "topic": fm.get("topic", ""),
                        })
                    except Exception:
                        continue

        kw_tokens = set(self._tokenize_link_text(keyword))
        tag_set = {t.lower() for t in (tags or [])}
        scored: List[Tuple[int, str]] = []
        seen = {current_slug}

        for p in posts:
            slug = p.get("slug") or Path(str(p.get("url", ""))).name
            if not slug or slug in seen:
                continue
            seen.add(slug)
            score = 0
            if p.get("topic") and p.get("topic") == topic:
                score += 6
            if p.get("category") and str(p.get("category")).lower() == (category or "").lower():
                score += 4
            p_tags = {str(t).lower() for t in (p.get("tags") or [])}
            score += len(tag_set & p_tags) * 2
            p_tokens = set(self._tokenize_link_text(f"{slug.replace('-', ' ')} {p.get('title', '')} {p.get('description', '')}"))
            score += len(kw_tokens & p_tokens) * 3
            scored.append((score, slug))

        scored.sort(key=lambda item: (-item[0], item[1]))
        chosen = [slug for _, slug in scored[:count]]
        for fallback in DEFAULT_CLUSTER_POSTS:
            if len(chosen) >= count:
                break
            if fallback != current_slug and fallback not in chosen:
                chosen.append(fallback)
        return chosen[:count]

    def _extract_or_build_tldr(
        self,
        raw_content: str,
        processed_content: str,
        title: str,
        keyword: str,
    ) -> Dict[str, Any]:
        """Extract valid tldr from draft frontmatter or build a deterministic 120-500 char answer + 3-6 takeaways."""
        if raw_content and raw_content.strip().startswith("---"):
            try:
                parts = raw_content.split("---", 2)
                if len(parts) >= 3:
                    fm = yaml.safe_load(parts[1]) or {}
                    tldr = fm.get("tldr") if isinstance(fm, dict) else None
                    if isinstance(tldr, dict):
                        ans = self._clean_plain_text_field(str(tldr.get("answer", "")))
                        tks = [self._clean_plain_text_field(str(t)) for t in (tldr.get("takeaways") or []) if str(t).strip()]
                        if 120 <= len(ans) <= 500 and 3 <= len(tks) <= 6:
                            return {"answer": ans, "takeaways": tks[:6]}
            except Exception:
                pass

        clean_body = self._strip_existing_frontmatter(processed_content)
        prose_paras: List[str] = []
        for block in re.split(r'\n\s*\n', clean_body):
            s = block.strip()
            if not s or s.startswith(('#', '|', '<', 'import ', 'export ', '```', '-', '*', '>')):
                continue
            plain = self._clean_plain_text_field(s)
            if len(plain) >= 60:
                prose_paras.append(plain)

        kw_clean = self._clean_plain_text_field(keyword) or self._clean_plain_text_field(title) or "this topic"
        answer = prose_paras[0] if prose_paras else (
            f"Getting reliable outcomes with {kw_clean} requires clear evaluation criteria, "
            f"repeatable implementation steps, and measurable checkpoints aligned to real constraints."
        )
        if len(answer) < 120:
            extra = prose_paras[1] if len(prose_paras) > 1 else (
                "Compare proven approaches against your team's requirements, validate assumptions early, "
                "and track concrete performance signals before scaling rollout."
            )
            answer = f"{answer.rstrip('. ')}. {extra}"
        if len(answer) > 480:
            cut = answer[:480]
            last_term = max(cut.rfind('.'), cut.rfind('!'), cut.rfind('?'))
            answer = cut[:last_term + 1] if last_term >= 120 else cut.rsplit(' ', 1)[0].rstrip('.,;:') + '.'

        # Extract bullet points or sentence takeaways from body
        bullets = [
            self._clean_plain_text_field(m.group(1))
            for m in re.finditer(r'(?m)^\s*[-*]\s+([^\n]{25,180})$', clean_body)
        ]
        takeaways: List[str] = []
        for b in bullets:
            if not b or b.lower().startswith(("question", "http", "import")):
                continue
            if not b.endswith(('.', '!', '?')):
                b = b.rstrip('.,;:') + '.'
            if 20 <= len(b) <= 180 and b not in takeaways:
                takeaways.append(b)
            if len(takeaways) >= 5:
                break

        for h2 in re.findall(r'(?m)^##\s+([^\n]+)$', clean_body):
            if len(takeaways) >= 4:
                break
            clean_h = self._clean_plain_text_field(h2).rstrip('?.! ')
            if clean_h and clean_h.lower() not in {"conclusion", "sources", "references", "frequently asked questions", "faq"}:
                cand = f"Evaluate {clean_h[0].lower() + clean_h[1:]} when planning {kw_clean}."
                if 20 <= len(cand) <= 180 and cand not in takeaways:
                    takeaways.append(cand)

        fallback_takeaways = [
            f"Define clear evaluation criteria and baseline metrics before rolling out {kw_clean}.",
            "Compare implementation trade-offs, maintenance overhead, and integration fit side by side.",
            "Validate assumptions on a focused pilot workflow before expanding across production.",
            "Review measurable outcomes on a regular cadence so decisions follow real data.",
        ]
        for fb in fallback_takeaways:
            if len(takeaways) >= 4:
                break
            if fb not in takeaways:
                takeaways.append(fb)

        return {"answer": answer, "takeaways": takeaways[:5]}
    
    def _extract_h2_sections(self, content: str) -> List[str]:
        """Extract H2 headings"""
        h2_matches = re.findall(r'^##\s+(.+)$', content, re.MULTILINE)
        return [h.strip() for h in h2_matches]
    
    def _strip_existing_frontmatter(self, content: str) -> str:
        """Strip any existing YAML frontmatter and imports from content.

        Handles the case where 3-part generation produces multiple leading
        frontmatter blocks (Part 2/3 occasionally emit their own). Without
        this, a residual `---` block ends up rendered as visible body text
        (audit F7).
        """
        content = content.strip()

        # Strip ALL leading frontmatter blocks (3-part generation can produce
        # more than one).
        for _ in range(3):
            if not content.startswith('---'):
                break
            lines = content.split('\n')
            frontmatter_end = 0
            in_block = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped == '---':
                    if not in_block:
                        in_block = True
                    else:
                        frontmatter_end = i + 1
                        break
            if frontmatter_end > 0:
                content = '\n'.join(lines[frontmatter_end:]).lstrip('\n')
                logger.debug("Stripped leading frontmatter block (%d lines)", frontmatter_end)
            else:
                # Opened but never closed — bail out rather than corrupt the body.
                logger.warning("Unterminated frontmatter detected; leaving content as-is")
                break

        # Strip document-level MDX import statements while preserving imports inside fenced code blocks.
        lines = content.split('\n')
        content_lines = []
        in_fence = False
        fence_char = ""
        fence_len = 0
        for line in lines:
            stripped = line.strip()
            fence_m = re.match(r'^(`{3,}|~{3,})(.*)$', stripped)
            if fence_m:
                run = fence_m.group(1)
                if not in_fence and (run[0] != "`" or "`" not in fence_m.group(2)):
                    in_fence = True
                    fence_char = run[0]
                    fence_len = len(run)
                    content_lines.append(line)
                    continue
                if in_fence and run[0] == fence_char and len(run) >= fence_len and not fence_m.group(2).strip():
                    in_fence = False
                    fence_char = ""
                    fence_len = 0
                    content_lines.append(line)
                    continue
            if not in_fence:
                if stripped.startswith('import ') or stripped.startswith('import{'):
                    continue
                if 'import {' in stripped and stripped.count('import') > 1:
                    continue
            content_lines.append(line)

        cleaned = '\n'.join(content_lines).lstrip('\n')
        # Final invariant: body must not start with the fence char.
        if cleaned.startswith('---'):
            logger.warning("Frontmatter strip left a leading '---'; trimming")
            cleaned = cleaned.lstrip('-').lstrip('\n')
        return cleaned

    def _build_complete_mdx(self, frontmatter: Dict[str, Any], content: str,
                        keyword: str, h2_sections: List[str],
                        research_data: Optional[Dict] = None, style: str = "standard",
                        slug: Optional[str] = None) -> str:
        """Build complete canonical MDX file with structured frontmatter and components.

        ``slug`` is the single canonical slug computed in ``format()``; it must
        match the returned slug and hero-image path. Falls back to the
        deterministic keyword slug only if a caller invokes this directly.
        """

        # Strip any existing frontmatter from content (Gemini sometimes includes it)
        content = self._strip_existing_frontmatter(content)

        # Use the single canonical slug from format(); deterministic fallback.
        slug = slug or self._generate_slug(keyword)
        reading_time = self._calculate_reading_time(content)

        # Build FAQ array from PAA questions. SERP data is nested under research_data['serp'].
        paa_questions = []
        if research_data:
            serp_data = research_data.get('serp') or {}
            paa_questions = research_data.get('paa_questions') or serp_data.get('paa_questions', [])
        faq_items = self._build_faq_array(paa_questions, content, keyword)

        # Emit an import only when its component tag is actually used in the body:
        # plain-markdown posts import nothing, and FAQ renders from the `faq:`
        # frontmatter via the shared layout (no <FAQSection> is ever written).
        base_imports: List[str] = []
        _import_map = [
            ("<Image", "import { Image } from 'astro:assets';"),
            ("<Button", "import Button from '@/components/ui/Button.astro';"),
            ("<Icon", "import { Icon } from 'astro-icon/components';"),
            ("<ComparisonTable", "import ComparisonTable from '@/components/sections/ComparisonTable.astro';"),
            ("<CitationsSection", "import CitationsSection from '@/components/content/CitationsSection.astro';"),
        ]
        for tag, imp in _import_map:
            if tag in content:
                base_imports.append(imp)

        # @/components/mdx — import only the named exports actually used.
        _mdx_used = [
            name for name in ("SolutionCard", "ComparisonSummary", "ServicesList", "MetadataBadges")
            if f"<{name}" in content
        ]
        if _mdx_used:
            base_imports.append(
                "import { " + ", ".join(_mdx_used) + " } from '@/components/mdx';"
            )

        clean_title = self._clean_plain_text_field(frontmatter['title'])
        clean_desc = self._fit_description_150_160(frontmatter['description'], keyword)
        excerpt = self._clean_plain_text_field(frontmatter.get('excerpt') or self._generate_excerpt(content, clean_desc))
        seo_title = self.build_seo_title(clean_title, keyword, style)
        hero_src = f"/src/assets/blog/{slug}-hero.webp"
        hero_alt = self._clean_plain_text_field(
            (frontmatter.get('image') or {}).get('alt') or self._generate_hero_alt(clean_title, keyword, style, content)
        )
        if hero_alt.lower().startswith("hero image"):
            hero_alt = self._generate_hero_alt(clean_title, keyword, style, "")

        canonical_url = self.brand_profile.canonical_blog_url(slug)
        product, funnel_stage, topic = self._compute_page_taxonomy(keyword, style, brand_profile=self.brand_profile)
        product = frontmatter.get('product') or product
        funnel_stage = frontmatter.get('funnel_stage') or funnel_stage
        topic = frontmatter.get('topic') or topic
        category = frontmatter.get('category') or self.brand_profile.category or "Guides"
        categories = frontmatter.get('categories') or [category]
        tags = frontmatter.get('tags') or self._generate_tags(keyword, content, style)
        related_posts = frontmatter.get('relatedPosts') or self._select_related_posts(keyword, slug, topic, category, tags)
        tldr = frontmatter.get('tldr') or self._extract_or_build_tldr("", content, clean_title, keyword)
        author = self.brand_profile.resolve_author(str(frontmatter.get('author') or "")).name

        frontmatter['canonical'] = canonical_url
        frontmatter['faq'] = faq_items
        frontmatter['title'] = clean_title
        frontmatter['description'] = clean_desc
        frontmatter['excerpt'] = excerpt
        frontmatter['author'] = author
        frontmatter['image'] = {'src': hero_src, 'alt': hero_alt}
        frontmatter['categories'] = categories
        frontmatter['product'] = product
        frontmatter['funnel_stage'] = funnel_stage
        frontmatter['topic'] = topic
        frontmatter['relatedPosts'] = related_posts
        frontmatter['tldr'] = tldr

        focus_kw = self._clean_plain_text_field(keyword) or clean_title.lower()
        _dq = lambda s: json.dumps(str(s or ""))

        # Build canonical frontmatter YAML (adapted per target framework in site_adapters.py).
        mdx_parts = [
            "---",
            f"title: {_dq(clean_title)}",
            f"description: {_dq(clean_desc)}",
            f"excerpt: {_dq(excerpt)}",
            f"publishDate: {_dq(frontmatter['publishDate'])}",
            f"updatedDate: {_dq(frontmatter['updatedDate'])}",
            f"author: {_dq(author)}",
            "image:",
            f"  src: {_dq(hero_src)}",
            f"  alt: {_dq(hero_alt)}",
            f"tags: {json.dumps(tags)}",
            f"category: {_dq(category)}",
            f"categories: {json.dumps(categories)}",
            f"product: {_dq(product)}",
            f"funnel_stage: {_dq(funnel_stage)}",
            f"topic: {_dq(topic)}",
            "featured: false",
            "draft: false",
            f"canonical: {_dq(canonical_url)}",
            f"relatedPosts: {json.dumps(related_posts)}",
            "tldr:",
            f"  answer: {_dq(self._clean_plain_text_field(tldr.get('answer', '')))}",
            "  takeaways:",
        ]
        for tk in (tldr.get('takeaways') or [])[:6]:
            mdx_parts.append(f"    - {_dq(self._clean_plain_text_field(tk))}")

        mdx_parts.extend([
            "seo:",
            f"  title: {_dq(seo_title)}",
            f"  description: {_dq(clean_desc)}",
            f"  ogTitle: {_dq(clean_title)}",
            f"  ogDescription: {_dq(excerpt if 100 <= len(excerpt) <= 160 else clean_desc)}",
            f"  ogImage: {_dq(hero_src)}",
            '  twitterCard: "summary_large_image"',
            f"  focusKeyword: {_dq(focus_kw)}",
            '  schemaType: "Article"',
            "  noindex: false",
            "  nofollow: false",
        ])

        # Always emit faq: (empty array when no valid FAQ pairs qualify) to satisfy blog contract.
        serialized_faq: List[str] = []
        for item in (faq_items or [])[:15]:
            q_clean = self._clean_plain_text_field(item.get('question', ''))
            a_clean = self._clean_plain_text_field(item.get('answer', ''))
            if q_clean and a_clean:
                serialized_faq.append(f"  - question: {_dq(q_clean)}")
                serialized_faq.append(f"    answer: {_dq(a_clean)}")
        if serialized_faq:
            mdx_parts.append("faq:")
            mdx_parts.extend(serialized_faq)
        else:
            mdx_parts.append("faq: []")

        mdx_parts.append("---")
        mdx_parts.append("")
        if base_imports:
            mdx_parts.extend(base_imports)
            mdx_parts.append("")
        mdx_parts.append(content.strip())
        mdx_parts.append("")

        mdx_output = "\n".join(mdx_parts)

        # Defense-in-depth scrubber catches instruction leaks that escape the
        # prompt-side cleanups in `_get_guide_prompt` (audit F3 / F8).
        strict_scrub = os.getenv('CONTENT_SCRUB_STRICT', '').lower() in ('1', 'true', 'yes')
        scrub_result = scrub_content(mdx_output, strict=strict_scrub)
        if scrub_result.strict_fail and strict_scrub:
            raise RuntimeError(
                f"Content scrubber blocked publish: {len(scrub_result.violations)} "
                f"instruction-leak violation(s). First: {scrub_result.violations[0].line[:160]}"
            )
        return scrub_result.cleaned

    def _extract_verified_claims_for_schema(
        self,
        content: str,
        research_data: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Extract verified claims for optional ClaimReview schema."""
        if not research_data:
            return []

        try:
            claims = self.fact_checker.extract_claims(content)
            verified_claims = self.fact_checker.verify_against_research(claims, research_data)
            return self.fact_checker.get_verified_claims_for_schema(verified_claims)
        except Exception as e:
            logger.warning(f"⚠️  ClaimReview extraction skipped: {e}")
            return []

    def refresh_schema_jsonld(self, mdx_content: str) -> str:
        """Strip any legacy inline `seo.jsonld` frontmatter scalar (`schema.json` + `@graph` is emitted by `site_adapters.py`)."""
        if not mdx_content or not mdx_content.strip().startswith('---'):
            return mdx_content

        match = re.match(r'^---\n(.*?)\n---\n(.*)$', mdx_content, re.DOTALL)
        if not match:
            return mdx_content

        frontmatter_text, body = match.group(1), match.group(2)
        if 'jsonld:' not in frontmatter_text:
            return mdx_content

        # Drop the whole-line `  jsonld: '...'` or `readingTime:` scalar.
        cleaned_fm = re.sub(
            r'^[ \t]*(?:jsonld|readingTime):.*$\n?', '', frontmatter_text, flags=re.MULTILINE
        )
        return f"---\n{cleaned_fm}\n---\n{body}"
    
    def _extract_howto_steps(self, content: str) -> List[Dict[str, Any]]:
        """Extract HowTo steps from guide content"""
        steps = []
        
        # Find step patterns
        step_pattern = r'^##\s*Step\s*(\d+)[:\s]+(.+)$'
        matches = re.finditer(step_pattern, content, re.MULTILINE)
        
        for match in matches:
            step_num = match.group(1)
            step_name = match.group(2).strip()
            
            # Get step content
            start = match.end()
            next_heading = re.search(r'^#{1,6}\s', content[start:], re.MULTILINE)
            
            if next_heading:
                step_content = content[start:start + next_heading.start()].strip()
            else:
                step_content = content[start:start + 500].strip()
            
            steps.append({
                "@type": "HowToStep",
                "name": step_name,
                "text": step_content[:200] + "..." if len(step_content) > 200 else step_content,
                "position": int(step_num)
            })
        
        return steps[:10]  # Limit to 10 steps
    
    def _extract_faq_items_for_schema(self, content: str) -> List[Dict[str, Any]]:
        """Extract FAQ items from content for schema"""
        faq_items = []
        
        # Look for FAQ section
        faq_section_match = re.search(r'^##\s*(?:FAQ|Frequently Asked Questions)\s*$', 
                                     content, re.MULTILINE | re.IGNORECASE)
        
        if faq_section_match:
            faq_start = faq_section_match.end()
            # Find next H2 or end of content
            next_h2 = re.search(r'^##\s+(?!#)', content[faq_start:], re.MULTILINE)
            faq_end = faq_start + next_h2.start() if next_h2 else len(content)
            
            faq_content = content[faq_start:faq_end]
            
            # Extract questions (H3 headings)
            question_pattern = r'^###\s*([^#\n]+\?)\s*$'
            questions = re.finditer(question_pattern, faq_content, re.MULTILINE)
            
            for match in questions:
                question = match.group(1).strip()
                
                # Find the answer
                start = match.end()
                next_heading = re.search(r'^#{1,6}\s', faq_content[start:], re.MULTILINE)
                
                if next_heading:
                    answer = faq_content[start:start + next_heading.start()].strip()
                else:
                    answer = faq_content[start:].strip()
                
                if answer:
                    faq_items.append({
                        "@type": "Question",
                        "name": question,
                        "acceptedAnswer": {
                            "@type": "Answer",
                            "text": answer[:500] + "..." if len(answer) > 500 else answer
                        }
                    })
        
        return faq_items[:15]  # Limit to 15 FAQ items

    def upsert_tldr_frontmatter(self, mdx_content: str, tldr: Dict[str, Any]) -> str:
        """Safely insert or update the tldr: section in MDX frontmatter.
        
        Args:
            mdx_content: Raw MDX content with frontmatter
            tldr: TLDR dictionary with 'answer' and 'takeaways'
            
        Returns:
            MDX content with updated tldr: section in frontmatter
        """
        mdx_content = mdx_content.strip()
        if not mdx_content.startswith('---'):
            logger.warning("MDX content does not start with frontmatter; returning as-is")
            return mdx_content
            
        parts = mdx_content.split('---', 2)
        if len(parts) < 3:
            logger.warning("Unterminated or invalid frontmatter block; returning as-is")
            return mdx_content
            
        frontmatter_text = parts[1]
        body = parts[2]
        
        # Remove any existing tldr: block from frontmatter_text
        lines = frontmatter_text.split('\n')
        filtered_lines = []
        skip_mode = False
        
        for line in lines:
            stripped = line.strip()
            # If we find tldr:, enter skip_mode and skip until next top-level non-indented key
            if stripped.startswith('tldr:') or stripped == 'tldr':
                skip_mode = True
                continue
            if skip_mode:
                # A top-level key is a non-empty line that does not start with whitespace and doesn't start with '#' or '-'
                if line.strip() and not line.startswith((' ', '\t', '-', '#')):
                    skip_mode = False  # Exit skip mode
                else:
                    continue  # Keep skipping
            filtered_lines.append(line)
            
        # Reconstruct frontmatter and add our new tldr block before 'seo:' if present
        # If 'seo:' isn't present, add it at the very end of frontmatter
        clean_ans = self._clean_plain_text_field(tldr.get('answer', ''))
        ans_escaped = clean_ans.replace("'", "''")
        takeaways_lines = []
        for point in tldr.get('takeaways', []):
            point_escaped = self._clean_plain_text_field(point).replace("'", "''")
            if point_escaped:
                takeaways_lines.append(f"    - '{point_escaped}'")
            
        tldr_block = [
            "tldr:",
            f"  answer: '{ans_escaped}'",
            "  takeaways:"
        ] + takeaways_lines
        tldr_str = "\n".join(tldr_block)
        
        # Find where to insert
        cleaned_fm = "\n".join(filtered_lines).rstrip('\n')
        
        # Find top-level seo: key rather than matching substrings inside quoted values
        seo_key_match = re.search(r'(?m)^seo:\s*$', cleaned_fm)
        if seo_key_match:
            idx = seo_key_match.start()
            new_fm = cleaned_fm[:idx] + tldr_str + "\n" + cleaned_fm[idx:]
        else:
            new_fm = cleaned_fm + "\n" + tldr_str
            
        # Return complete MDX
        return f"---\n{new_fm.strip()}\n---\n{body}"

    def finalize_astro_mdx(self, mdx_content: str, keyword: str = "", style: str = "standard", slug: str = "") -> str:
        """Final post-polish normalization of canonical MDX."""
        content = self.ensure_signup_cta(mdx_content)
        content = self.sync_faq_frontmatter(content)
        content = self.ensure_page_taxonomy(content, keyword, style, brand_profile=self.brand_profile)
        content = self.refresh_schema_jsonld(content)
        content = self.normalize_internal_links(content, brand_profile=self.brand_profile)
        if self.flyweel_mode != 'none':
            content = self.apply_compliance_footnotes(content, brand_profile=self.brand_profile)
        content = self.reconcile_citations(content)

        # Ensure seo.title is distinct from H1 title and 40-50 chars (50-60 with ' | Flyweel')
        fm_match = re.match(r'^---\n(.*?)\n---\n(.*)$', content, re.DOTALL)
        if fm_match:
            fm_text, body_text = fm_match.group(1), fm_match.group(2)
            try:
                fm_data = yaml.safe_load(fm_text)
            except yaml.YAMLError:
                fm_data = None
            if isinstance(fm_data, dict) and isinstance(fm_data.get("seo"), dict):
                h1_title = str(fm_data.get("title") or "")
                cur_seo_title = str(fm_data["seo"].get("title") or "")
                if h1_title and cur_seo_title:
                    suffix = self.brand_profile.effective_title_suffix()
                    full_len = len(cur_seo_title) + (len(suffix) if suffix and suffix.strip() not in cur_seo_title else 0)
                    if cur_seo_title.lower() == h1_title.lower() or not (38 <= full_len <= 68):
                        new_seo = self.build_seo_title(h1_title, keyword or h1_title, style)
                        fm_text = re.sub(
                            r'(^seo:\s*\n(?:^[ \t]+[^\n]*\n)*?^[ \t]+title:\s*).+$',
                            lambda m: f"{m.group(1)}{json.dumps(new_seo)}",
                            fm_text,
                            flags=re.MULTILINE,
                        )
                    fm_text = re.sub(
                        r'(^seo:\s*\n(?:^[ \t]+[^\n]*\n)*?^[ \t]+ogTitle:\s*).+$',
                        lambda m: f"{m.group(1)}{json.dumps(h1_title)}",
                        fm_text,
                        flags=re.MULTILINE,
                    )
            content = f"---\n{fm_text}\n---\n{body_text}"

        strict_scrub = os.getenv('CONTENT_SCRUB_STRICT', '').lower() in ('1', 'true', 'yes')
        scrub_result = scrub_content(content, strict=strict_scrub)
        if scrub_result.strict_fail and strict_scrub:
            first_detail = (
                scrub_result.violations[0].line[:160]
                if scrub_result.violations
                else f"over-deletion ({scrub_result.removed_ratio:.0%})"
            )
            raise RuntimeError(
                f"Content scrubber blocked publish: {len(scrub_result.violations)} "
                f"instruction-leak violation(s). First: {first_detail}"
            )
        return scrub_result.cleaned
