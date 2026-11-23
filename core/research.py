"""Lean Research Module - Just what we need for quality content"""
import asyncio
import os
import re
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime, timedelta
from urllib.parse import urlparse
import aiohttp
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from googleapiclient.discovery import build
from .ai_router import SmartAIRouter
from .agent_credentials import post_sonar_chat, resolve_role_credential
from .brand_profile import load_brand_profile
from .gsc_analyzer import calculate_traffic_potential, CTR_BY_POSITION, GSCAnalyzer
from .paths import load_repo_env
from . import jev

# Load environment variables from the repository without replacing process values.
load_repo_env()

# Setup logging
logger = logging.getLogger(__name__)

# Backstop timeout for raw aiohttp sessions so a hung HTTP call cannot block the
# pipeline forever. >= the longest legitimate call (SERP analysis, 1200s); the
# per-call ``timeout=`` arguments on Perplexity/Interfaze calls still override this.
_AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=1200, connect=30)

# Research depth profiles: what --depth actually changes in the research phase.
# reddit_limit/quora_limit of None means the mining loops' own caps apply
# (10 successful Reddit queries / 5 Quora). wall_clock is a per-research-task
# pathology backstop, NOT a perf tuning knob — a wait_for cancel loses the
# mining loop's partial insights, so it is sized to be unreachable by any
# healthy run (quick worst case ≈ 3 Reddit calls × 150s per-call timeout +
# margin). The actual quick speedup comes from the limits + per-call timeouts.
DEPTH_PROFILES: Dict[str, Dict[str, Any]] = {
    "quick":         {"reddit_limit": 3,    "quora_limit": 1,    "topical_insights": False, "serp_timeout": 180, "wall_clock": 600},
    "standard":      {"reddit_limit": None, "quora_limit": None, "topical_insights": True,  "serp_timeout": 300, "wall_clock": 1800},
    # Comprehensive research == standard: its meaning lives in solution_count
    # (top-compare listicle size), not in extra research volume.
    "comprehensive": {"reddit_limit": None, "quora_limit": None, "topical_insights": True,  "serp_timeout": 300, "wall_clock": 1800},
}


def resolve_research_plan(style: str, depth: str, skip_community: bool, limit_community: bool) -> Dict[str, Any]:
    """Collapse depth + legacy community flags into one research plan dict.

    Pure and unit-testable. Precedence:
      1. skip_community (--nr) always wins — no mining at all.
      2. limit_community (--nrl) wins over depth — 3 Reddit / 1 Quora.
      3. style == "research" keeps its own semantics (30 Reddit queries,
         sonar-deep-research SERP at 1200s) regardless of depth.
      4. Otherwise the depth profile supplies the limits.
    Unknown depth falls back to standard with a warning.
    """
    profile = DEPTH_PROFILES.get(depth)
    if profile is None:
        logger.warning(f"Unknown research depth '{depth}' - falling back to standard")
        profile = DEPTH_PROFILES["standard"]
    plan = dict(profile)
    plan["depth"] = depth if depth in DEPTH_PROFILES else "standard"

    if style == "research":
        # Research style owns its research volume; depth never dilutes it.
        plan["reddit_limit"] = 30 if not limit_community else 3
        plan["quora_limit"] = 1 if limit_community else None
        plan["topical_insights"] = False  # research style uses sonar-deep-research instead
        plan["serp_timeout"] = 1200
        plan["wall_clock"] = 2400
    elif limit_community:
        plan["reddit_limit"] = 3
        plan["quora_limit"] = 1

    if skip_community:
        plan["reddit_limit"] = 0
        plan["quora_limit"] = 0

    return plan


def load_icp_context_for_research() -> str:
    """Load dynamic ICP context from BrandProfile for research relevance filtering."""
    return load_brand_profile().build_research_icp_filter()


# Backwards-compatible module symbol; call load_icp_context_for_research() per run for fresh profile state.
_ICP_RESEARCH_CONTEXT = load_icp_context_for_research()


# Intent classification patterns (from SEO best practices)
INTENT_PATTERNS = {
    'informational': {
        'patterns': ['what is', 'how to', 'how do', 'why', 'guide', 'tutorial', 'learn', 'explain', 'understand', 'definition'],
        'recommended_style': 'guide',
        'structure_priority': 'what_is_section_first',
        'content_focus': 'educational_depth'
    },
    'commercial': {
        'patterns': ['best', 'top', 'vs', 'versus', 'comparison', 'compare', 'review', 'alternative', 'alternatives to', 'like'],
        'recommended_style': 'comparison',
        'structure_priority': 'comparison_table_first',
        'content_focus': 'feature_comparison'
    },
    'transactional': {
        'patterns': ['buy', 'price', 'pricing', 'discount', 'free trial', 'sign up', 'get started', 'download', 'deal'],
        'recommended_style': 'feature',
        'structure_priority': 'cta_heavy',
        'content_focus': 'conversion_focused'
    },
    'navigational': {
        'patterns': ['login', 'official', 'site', 'website', 'app', 'dashboard'],
        'recommended_style': 'standard',
        'structure_priority': 'direct_answer',
        'content_focus': 'quick_navigation'
    }
}


def classify_keyword_intent(keyword: str) -> Dict[str, Any]:
    """
    Classify keyword search intent and recommend content structure.

    Based on SEO best practices:
    - Informational: "what is X", "how to Y" → guide style, educational depth
    - Commercial: "best X", "X vs Y" → comparison style, feature tables first
    - Transactional: "buy X", "X pricing" → feature style, CTA-heavy
    - Navigational: "X login", "X app" → standard style, direct answer

    Args:
        keyword: The target keyword to classify

    Returns:
        Dict with intent type, recommended style, and structure guidance
    """
    keyword_lower = keyword.lower()

    # Score each intent type based on pattern matches
    intent_scores = {}
    for intent_type, config in INTENT_PATTERNS.items():
        score = 0
        matched_patterns = []
        for pattern in config['patterns']:
            if re.search(r'\b' + re.escape(pattern) + r'\b', keyword_lower):
                # Weight longer patterns more heavily
                weight = len(pattern.split())
                score += weight
                matched_patterns.append(pattern)
        intent_scores[intent_type] = {
            'score': score,
            'matched_patterns': matched_patterns
        }

    # Find highest scoring intent
    best_intent = max(intent_scores.items(), key=lambda x: x[1]['score'])
    intent_type = best_intent[0]
    intent_data = best_intent[1]

    # Default to informational if no patterns matched
    if intent_data['score'] == 0:
        intent_type = 'informational'

    config = INTENT_PATTERNS[intent_type]

    result = {
        'intent_type': intent_type,
        'confidence': min(intent_data['score'] / 3.0, 1.0),  # Normalize to 0-1
        'matched_patterns': intent_data.get('matched_patterns', []),
        'recommended_style': config['recommended_style'],
        'structure_priority': config['structure_priority'],
        'content_focus': config['content_focus'],
        'guidance': _get_structure_guidance(intent_type, config)
    }

    logger.info(f"🎯 Intent Classification: {intent_type} (confidence: {result['confidence']:.0%})")
    logger.info(f"   SEO-suggested style: {config['recommended_style']} (informational only, does not override user selection)")
    logger.info(f"   Structure priority: {config['structure_priority']}")

    return result


_JEV_INTENT_CRITERIA = {
    'informational': "Wants to learn what something is, how it works, or how to do it.",
    'commercial': "Comparing or evaluating products or services before choosing one.",
    'transactional': "Wants a price, plan, free trial, or sign-up page for a product.",
    'navigational': "Wants to reach one specific brand's website, login, or app.",
}


async def classify_keyword_intent_jev(keyword: str) -> Dict[str, Any]:
    """``classify_keyword_intent`` with a Jev Choice layered on top.

    Explicit patterns ("best", "vs", "pricing") are strong signals, so Jev only
    overrides a pattern match at >= 0.95 confidence; when no pattern matched
    (the heuristic's blind default of informational) Jev decides at >= 0.6.
    Live check: Jev reads "acme pricing" as informational at 0.87, which
    the pattern rule correctly keeps transactional."""
    result = classify_keyword_intent(keyword)
    answers = await jev.ask({"keyword": keyword}, {"intent": {
        "type": "choice",
        "instructions": "Which search intent does a person typing this keyword have?",
        "criteria": _JEV_INTENT_CRITERIA,
    }})
    answer = (answers or {}).get("intent") or {}
    intent_type, confidence = answer.get("choice"), answer.get("confidence")
    bar = 0.95 if result['matched_patterns'] else 0.6
    if intent_type in INTENT_PATTERNS and isinstance(confidence, (int, float)) and confidence >= bar:
        config = INTENT_PATTERNS[intent_type]
        result.update(
            intent_type=intent_type,
            confidence=float(confidence),
            recommended_style=config['recommended_style'],
            structure_priority=config['structure_priority'],
            content_focus=config['content_focus'],
            guidance=_get_structure_guidance(intent_type, config),
            source=jev.JEV_MODEL,
        )
        logger.info(f"🎯 Jev intent: {intent_type} (confidence: {confidence:.0%})")
    return result


def _get_structure_guidance(intent_type: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Get specific content structure guidance based on intent."""
    guidance = {
        'informational': {
            'opening': 'Start with "What is [topic]?" definition section',
            'h2_priority': ['Definition', 'How It Works', 'Benefits', 'Implementation', 'Best Practices', 'FAQ'],
            'cta_placement': 'end_of_sections',
            'comparison_table': False,
            'faq_required': True,
            'min_word_count': 2000
        },
        'commercial': {
            'opening': 'Lead with quick comparison table showing top options',
            'h2_priority': ['Quick Comparison', 'Top Solutions', 'Feature Comparison', 'Pricing', 'Best For', 'FAQ'],
            'cta_placement': 'after_comparison_table',
            'comparison_table': True,
            'faq_required': True,
            'min_word_count': 2500
        },
        'transactional': {
            'opening': 'Hook → Solution → Benefits → CTA in first 200 words',
            'h2_priority': ['Key Features', 'Pricing', 'How to Get Started', 'Benefits', 'FAQ'],
            'cta_placement': 'prominent_throughout',
            'comparison_table': False,
            'faq_required': True,
            'min_word_count': 1500
        },
        'navigational': {
            'opening': 'Direct answer in first paragraph',
            'h2_priority': ['Overview', 'Key Features', 'Getting Started', 'FAQ'],
            'cta_placement': 'single_prominent',
            'comparison_table': False,
            'faq_required': False,
            'min_word_count': 1000
        }
    }
    return guidance.get(intent_type, guidance['informational'])


# ─────────────────────────────────────────────────────────────────────────────
# Research-grade source credibility (research style only)
# ─────────────────────────────────────────────────────────────────────────────

# Tier A — primary / peer-reviewed / analyst & market-research firms.
_TIER_A_DOMAINS = (
    ".edu", ".gov", ".ac.uk", ".gov.uk", ".edu.au", ".gov.au", ".gc.ca", ".gov.ca",
    ".go.jp", ".ac.jp", ".edu.sg", ".gov.sg", ".gov.in",
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "arxiv.org",
    "sec.gov", "who.int", "oecd.org", "worldbank.org", "imf.org", "doi.org",
    "nature.com", "science.org", "sciencedirect.com", "jstor.org", "ssrn.com",
    "nber.org", "ieee.org", "acm.org", "iso.org", "w3.org", "nist.gov", "ietf.org",
    "gartner.com", "forrester.com", "mckinsey.com", "bcg.com", "bain.com",
    "deloitte.com", "pwc.com", "kpmg.com", "ey.com", "statista.com",
    "pewresearch.org", "nielsen.com", "idc.com",
)

# Tier B — established publications / official docs (must also carry a data signal).
_TIER_B_DOMAINS = (
    "hbr.org", "economist.com", "ft.com", "wsj.com", "bloomberg.com",
    "reuters.com", "nytimes.com", "techcrunch.com", "theverge.com",
    "wired.com", "arstechnica.com",
)
_TIER_B_PATH_HINTS = (
    "/research", "/reports", "/report", "/whitepaper", "/white-paper",
    "/docs/", "/blog/research", "/insights",
)
_DATA_SIGNAL_RE = re.compile(
    r"(\d+(?:[.,]\d+)?\s?%|\$\s?\d|\bsurvey\b|\bstud(?:y|ies)\b|\breport\b|"
    r"\bbenchmark\b|\bstatistics\b|\brespondents\b|\bsample size\b|"
    r"\bmethodolog|\bdataset\b|\bn\s?=\s?\d)",
    re.IGNORECASE,
)
# Hosts that must never be cited as a research source.
_TIER_C_BLOCK = (
    "pinterest.", "quora.com", "reddit.com", "medium.com", "facebook.com",
    "x.com", "twitter.com", "linkedin.com", "youtube.com", "tiktok.com",
)


def canonicalize_url(url: str) -> str:
    """Canonical form of a URL for cross-provider de-duplication.

    Lowercases the host, drops scheme, ``www.``, query string, fragment and any
    trailing slash so the same resource from different providers collapses to
    one key.
    """
    if not url or not isinstance(url, str):
        return ""
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return url.strip().lower().rstrip("/")
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/")
    return f"{host}{path}" if host else url.strip().lower().rstrip("/")


def classify_source_credibility(url: str, title: str = "", content: str = "") -> str:
    """Classify a source into credibility tier ``'A'``, ``'B'`` or ``'C'``.

    A = primary / peer-reviewed / analyst sources; B = established publications
    or official docs that also carry a concrete data signal; C = everything
    else (thin blogs, affiliate roundups, uncited vendor pages, social).
    """
    raw = (url or "").strip()
    if not raw:
        return "C"
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = (parsed.path or "").lower()
    except ValueError:
        return "C"
    if not host:
        return "C"

    def host_matches(pattern: str) -> bool:
        raw_pat = pattern.strip().lower()
        if not raw_pat:
            return False
        # TLD / suffix rule (e.g. ".edu", ".gov", ".ac.uk")
        if raw_pat.startswith(".") and not raw_pat.endswith("."):
            suffix = raw_pat.lstrip(".")
            return bool(suffix) and (host == suffix or host.endswith("." + suffix))
        # Second-level wildcard block rule (e.g. "pinterest." -> pinterest.com, pinterest.co.uk)
        if raw_pat.endswith(".") and not raw_pat.startswith("."):
            label = raw_pat.rstrip(".")
            parts = host.split(".")
            return len(parts) >= 2 and label in parts[:-1]
        domain = raw_pat.strip(".")
        if not domain or "." not in domain:
            return False
        return host == domain or host.endswith("." + domain)

    if any(host_matches(blocked) for blocked in _TIER_C_BLOCK):
        return "C"
    if any(host_matches(domain) for domain in _TIER_A_DOMAINS):
        return "A"

    has_data = bool(_DATA_SIGNAL_RE.search(f"{title} {content}"))
    is_established = any(host_matches(domain) for domain in _TIER_B_DOMAINS)
    # A report-looking path on an arbitrary host is not a credibility signal.
    # Tier B is limited to the hostname allowlist; path hints are labels only.
    if is_established and has_data:
        return "B"
    return "C"


def _source_type_label(url: str) -> str:
    """Local E-E-A-T-style label for a research source (no formatter import)."""
    u = (url or "").lower()
    if any(d in u for d in (".edu", ".gov", ".org")):
        return "Educational/Government Resource"
    if any(k in u for k in ("research", "study", "whitepaper", "white-paper")):
        return "Research Study"
    if any(k in u for k in ("report", "benchmark", "survey", "data", "statistics")):
        return "Industry Report"
    if any(k in u for k in ("guide", "best-practices", "how-to")):
        return "Expert Guide"
    return "Industry Analysis"


def _normalize_source(raw: Any, provider: str) -> Optional[Dict[str, Any]]:
    """Coerce a Perplexity citation/search-result or Interfaze result into the
    shared research-source shape. Returns ``None`` when there is no usable URL.
    """
    if isinstance(raw, str):
        url, title, description, content = raw, "", "", ""
    elif isinstance(raw, dict):
        url = raw.get("url") or raw.get("link") or ""
        title = raw.get("title") or raw.get("name") or ""
        description = raw.get("description") or raw.get("snippet") or ""
        content = raw.get("content") or ""
    else:
        return None
    if not url or not str(url).startswith("http"):
        return None
    tier = classify_source_credibility(
        str(url), str(title), f"{description} {content}"
    )
    return {
        "title": str(title).strip(),
        "url": str(url).strip(),
        "description": str(description).strip(),
        "content": str(content).strip(),
        "provider": provider,
        "source_type": _source_type_label(str(url)),
        "credibility_tier": tier,
    }


def merge_research_sources(
    serp_citations: Optional[List[Any]] = None,
    serp_search_results: Optional[List[Any]] = None,
    interfaze_sources: Optional[List[Any]] = None,
) -> List[Dict[str, Any]]:
    """Merge, normalize, tier-filter and de-duplicate research sources.

    Drops Tier C entirely; returns Tier A before Tier B, de-duplicated by
    :func:`canonicalize_url` (higher tier / richer record wins on collision).
    """
    by_key: Dict[str, Dict[str, Any]] = {}

    def _ingest(items: Optional[List[Any]], provider: str) -> None:
        for raw in items or []:
            norm = _normalize_source(raw, provider)
            if not norm or norm["credibility_tier"] == "C":
                continue
            key = canonicalize_url(norm["url"])
            if not key:
                continue
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = norm
                continue
            better_tier = norm["credibility_tier"] < existing["credibility_tier"]
            same_tier = norm["credibility_tier"] == existing["credibility_tier"]
            richer = (len(norm["content"]) + len(norm["title"])) > (
                len(existing["content"]) + len(existing["title"])
            )
            if better_tier or (same_tier and richer):
                by_key[key] = norm

    _ingest(serp_search_results, "perplexity-serp")
    _ingest(serp_citations, "perplexity-serp")
    _ingest(interfaze_sources, "interfaze")

    merged = list(by_key.values())
    merged.sort(key=lambda s: s["credibility_tier"])
    return merged


def _topic_context_block(keyword_context: str) -> str:
    """Authoritative human-confirmed topic framing, prepended to research prompts.

    Ensures SERP/PAA/topical/community queries all interpret the keyword through
    the single confirmed lens instead of each phase re-guessing intent.
    """
    if not keyword_context:
        return ""
    return (
        "TOPIC CONTEXT (authoritative, human-confirmed — interpret the keyword "
        f"strictly through this lens):\n{keyword_context}\n\n"
    )


class WebResearcher:
    """Handles SERP analysis and GSC data"""
    
    def __init__(self):
        self.perplexity_key = resolve_role_credential("RESEARCH").api_key
        # Removed Google Custom Search dependencies
        self.session = None
        self._gsc_service = None
        self._gsc_credentials = None
        self._init_gsc()
        self.ai = SmartAIRouter()
    
    def _init_gsc(self):
        """Initialize Google Search Console"""
        creds_path = os.getenv('GOOGLE_SERVICE_ACCOUNT_PATH')
        if creds_path and os.path.exists(creds_path):
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    creds_path,
                    scopes=['https://www.googleapis.com/auth/webmasters.readonly']
                )
                self._gsc_credentials = credentials
                default_site = load_brand_profile().site_url or 'https://example.com'
                self.site_url = os.getenv('GSC_PROPERTY_URL', os.getenv('GSC_SITE_URL', default_site))
            except Exception as e:
                logger.warning(f"GSC init failed: {e}")
                self._gsc_credentials = None

    async def _ensure_gsc_service(self) -> None:
        if self._gsc_service is not None or self._gsc_credentials is None:
            return
        try:
            self._gsc_service = await asyncio.wait_for(
                asyncio.to_thread(
                    build, 'searchconsole', 'v1',
                    credentials=self._gsc_credentials,
                    cache_discovery=False,
                ),
                timeout=30,
            )
        except Exception as exc:
            logger.warning(f"GSC service build failed: {exc}")
            self._gsc_service = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=_AIOHTTP_TIMEOUT)
        await self._ensure_gsc_service()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
            self.session = None
        GSCAnalyzer._close_http_connections(getattr(self._gsc_service, "_http", None))
        await self.ai.__aexit__(exc_type, exc_val, exc_tb)
    
    async def analyze_serp(self, keyword: str, augment_serp: bool = False, style: str = "standard", keyword_context: str = "", depth: str = "standard") -> Dict[str, Any]:
        """Get comprehensive SERP data using Perplexity and GSC (no Google Custom Search dependency)"""
        self._keyword_context = keyword_context or getattr(self, '_keyword_context', '')
        # First, generate a shorter, broader keyword for SERP analysis
        serp_keyword = await self._generate_serp_keyword(keyword, keyword_context=self._keyword_context)
        logger.info(f"Using SERP keyword: '{serp_keyword}' (from original: '{keyword}')")

        # Create ordered tasks with fixed indices
        task_map = {
            'paa': None,
            'gsc': None,
            'serp_analysis': None
        }
        tasks = []

        # Get People Also Ask (index 0) - use the broader SERP keyword
        task_map['paa'] = len(tasks)
        tasks.append(self._extract_paa(serp_keyword))

        # Get GSC data for existing keywords (index 1) - use the broader SERP keyword
        if self._gsc_service and augment_serp:
            task_map['gsc'] = len(tasks)
            tasks.append(self._get_gsc_data(serp_keyword))
        else:
            task_map['gsc'] = len(tasks)
            tasks.append(asyncio.create_task(asyncio.sleep(0, result=None)))  # Empty placeholder

        # Add comprehensive SERP analysis using Perplexity (index 2) - use the broader SERP keyword
        if self.perplexity_key and augment_serp:
            task_map['serp_analysis'] = len(tasks)
            tasks.append(self._analyze_serp_landscape(serp_keyword, style=style, depth=depth))
        else:
            task_map['serp_analysis'] = len(tasks)
            tasks.append(asyncio.create_task(asyncio.sleep(0, result=None)))  # Empty placeholder

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Extract results by their known indices
        paa_result = results[task_map['paa']]
        gsc_result = results[task_map['gsc']]
        serp_analysis_result = results[task_map['serp_analysis']]

        # Process PAA results
        if isinstance(paa_result, Exception):
            logger.warning(f"PAA extraction failed: {paa_result}")
            paa_questions = []
        else:
            paa_questions = paa_result if paa_result else []

        # Classify keyword intent for content structure guidance
        intent_classification = await classify_keyword_intent_jev(keyword)

        # Build SERP data from available sources
        serp_data = {
            "keyword": keyword,  # Keep original keyword in results
            "serp_keyword": serp_keyword,  # Include the SERP keyword used
            "timestamp": datetime.now().isoformat(),
            "search_results": [],  # No longer using Google Custom Search
            "paa_questions": paa_questions,
            "gsc_data": gsc_result if not isinstance(gsc_result, Exception) else None,
            "serp_analysis": serp_analysis_result if not isinstance(serp_analysis_result, Exception) else None,
            "intent_classification": intent_classification,  # SEO intent mapping
            "content_gaps": [],
            "recommended_length": intent_classification['guidance'].get('min_word_count', 2500)
        }

        # Use SERP analysis for content insights if available
        if serp_data["serp_analysis"]:
            try:
                serp_data["content_gaps"] = self._extract_content_gaps_from_analysis(serp_data["serp_analysis"])
                serp_data["recommended_length"] = self._calculate_length_from_analysis(serp_data["serp_analysis"])
            except Exception as e:
                logger.warning(f"Content analysis failed: {e}")
                serp_data["content_gaps"] = []

        return serp_data

    async def _generate_serp_keyword(self, original_keyword: str, keyword_context: str = "") -> str:
        """Generate a shorter, broader keyword optimized for SERP analysis using AI"""
        try:
            # Use Perplexity to generate a good SERP keyword
            context_block = ""
            if keyword_context:
                context_block = f"\nConfirmed topic interpretation:\n{keyword_context}\n"

            prompt = f"""Given this keyword: "{original_keyword}"
{context_block}
Create a shorter, optimized version (4-6 words) that would be effective for SERP analysis and People Also Ask questions.

Requirements:
- Analyze semantically to identify core concepts and distinguishing qualifiers
- Preserve terms that differentiate this topic from related but different topics
- Keep domain-specific qualifiers that add critical semantic meaning
- Remove question words and generic fluff ("state of", "guide to", "best", year numbers)
- Make it broad enough to capture related searches but specific enough to maintain unique context
- Focus on what users actually search for in this domain
- Target 4-6 words for natural, contextually rich queries

Return only the optimized keyword, no explanation.
"""
            payload = {
                "model": "sonar",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 30
            }
            data = await post_sonar_chat(self.session, payload, timeout=30, explicit_key=self.perplexity_key)
            if data and data.get("choices"):
                serp_keyword = data["choices"][0]["message"]["content"].strip()
                serp_keyword = serp_keyword.strip('"').strip("'").strip('*').strip()
                if (len(serp_keyword) > 5 and len(serp_keyword) < len(original_keyword)
                    and ' ' in serp_keyword and len(serp_keyword.split()) <= 6):
                    return serp_keyword

        except Exception as e:
            logger.warning(f"AI keyword generation failed: {e}")

        # Fallback to simple extraction
        words = original_keyword.split()
        # Skip question words and take content words, preserving all important terms
        content_words = [w for w in words if w.lower() not in {'which', 'what', 'how', 'why', 'when', 'where', 'who', 'are', 'the', 'best', 'top'}]
        # Take up to 6 words for better context preservation
        result = ' '.join(content_words[:6]) if content_words else ' '.join(words[:6])
        logger.info(f"Fallback SERP keyword: '{result}' from '{original_keyword}'")
        return result


    async def _extract_paa(self, keyword: str) -> List[str]:
        """Extract People Also Ask questions using Perplexity with robust fallback strategy"""
        if not self.perplexity_key:
            logger.warning("PERPLEXITY_API_KEY not found, using AI-generated fallback questions")
            return await self._generate_fallback_questions(keyword)

        # Multi-model fallback strategy with retry - use reasoning model first for better quality
        models = ["sonar-reasoning-pro", "sonar-pro"]
        max_retries = 3

        for attempt in range(max_retries):
            for model in models:
                try:
                    questions = await self._try_extract_with_model(keyword, model)
                    if questions and len(questions) >= 8:
                        logger.info(f"Successfully extracted {len(questions)} PAA questions for '{keyword}' using {model}")
                        return questions
                except Exception as e:
                    logger.warning(f"{model} failed on attempt {attempt + 1}: {e}")
                    continue

            # Exponential backoff between attempts
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

        # All models failed, use AI-generated fallbacks
        logger.warning(f"All PAA extraction methods failed for '{keyword}', using AI-generated fallback questions")
        return await self._generate_fallback_questions(keyword)

    async def _try_extract_with_model(self, keyword: str, model: str) -> List[str]:
        """Try extracting PAA with a specific model"""
        icp_context = load_icp_context_for_research()
        logger.info(f"🎯 PAA extraction: ICP context injected ({len(icp_context)} chars)")

        prompt = _topic_context_block(getattr(self, '_keyword_context', '')) + f"""You are analyzing search intent and related questions for the keyword: "{keyword}"

Task: Extract and generate the most relevant "People Also Ask" (PAA) style questions that searchers would ask related to this keyword.
{icp_context}
Context Guidelines:
- Consider the search intent behind "{keyword}" (informational, commercial, navigational, transactional)
- Focus on questions that address different aspects: what, how, why, when, where, which, who
- Include questions about implementation, benefits, challenges, comparisons, costs, best practices
- Prioritize questions that reflect real user pain points and information needs
- Consider both beginner and advanced user perspectives

Quality Guidelines:
- Prioritize questions from credible, recent sources (2025-2026)
- Focus on specific, actionable questions over generic ones
- Prefer questions with data-driven or expert backing

Return ONLY a valid JSON object with this exact structure:
{{
  "questions": ["Question 1?", "Question 2?", "Question 3?"]
}}

Requirements:
- Array must contain 8-12 high-quality, relevant questions
- Each question must be a complete sentence ending with ?
- Questions should naturally follow from search intent for "{keyword}"
- Vary question types (what, how, why, best, compare, etc.)
- No explanations or additional text outside the JSON structure"""

        payload = {
            "model": model,
            "messages": [{
                "role": "system",
                "content": "You are an expert SEO analyst specializing in understanding search intent and related questions. You analyze keywords and extract the most relevant questions that users would ask."
            }, {
                "role": "user",
                "content": prompt
            }],
            "temperature": 0.2,
            "max_tokens": 1500,
            "search_recency_filter": "month",
            "return_citations": True
        }

        data = await post_sonar_chat(self.session, payload, timeout=120, explicit_key=self.perplexity_key)
        if not data or not data.get("choices"):
            raise Exception("Sonar PAA request returned no data")

        content = data["choices"][0]["message"]["content"]
        return self._parse_and_validate_paa_response(content, keyword)

    def _parse_and_validate_paa_response(self, content: str, keyword: str) -> List[str]:
        """Parse JSON response and validate questions"""
        # Clean content first
        content = self._clean_json_content(content)

        try:
            # Try to parse as JSON object with questions key
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "questions" in parsed:
                questions = parsed["questions"]
                if isinstance(questions, list):
                    return self._validate_and_filter_questions(questions, keyword)

            # Fallback: try to parse as direct array
            if content.strip().startswith('['):
                questions = json.loads(content)
                if isinstance(questions, list):
                    return self._validate_and_filter_questions(questions, keyword)

        except json.JSONDecodeError:
            # Try to repair common JSON issues
            repaired_content = self._repair_json_content(content)
            if repaired_content:
                try:
                    parsed = json.loads(repaired_content)
                    if isinstance(parsed, dict) and "questions" in parsed:
                        questions = parsed["questions"]
                        if isinstance(questions, list):
                            return self._validate_and_filter_questions(questions, keyword)
                    elif isinstance(parsed, list):
                        return self._validate_and_filter_questions(parsed, keyword)
                except json.JSONDecodeError:
                    pass

        # Last resort: extract questions with regex
        questions = re.findall(r'"([^"]+\?)"', content)
        if questions:
            return self._validate_and_filter_questions(questions, keyword)

        return []

    def _clean_json_content(self, content: str) -> str:
        """Clean JSON content by removing thinking tags and extra text"""
        # Remove thinking tags
        content = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL)
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)

        # Remove markdown code blocks
        content = re.sub(r'```json\s*', '', content)
        content = re.sub(r'```\s*$', '', content)

        # Extract JSON object or array
        content = content.strip()

        # Find JSON boundaries
        start_brace = content.find('{')
        start_bracket = content.find('[')

        if start_brace != -1 and (start_bracket == -1 or start_brace < start_bracket):
            # JSON object
            end_brace = content.rfind('}')
            if end_brace > start_brace:
                return content[start_brace:end_brace + 1]
        elif start_bracket != -1:
            # JSON array
            end_bracket = content.rfind(']')
            if end_bracket > start_bracket:
                return content[start_bracket:end_bracket + 1]

        return content

    def _repair_json_content(self, content: str) -> str:
        """Repair common JSON formatting issues"""
        try:
            # Remove trailing commas
            content = re.sub(r',(\s*[}\]])', r'\1', content)
            # Fix unquoted keys
            content = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', content)
            # Fix single quotes
            content = re.sub(r"'([^']*)'", r'"\1"', content)
            # Fix missing quotes around string values
            content = re.sub(r':\s*([a-zA-Z][^,}\]]*)\s*([,}\]])', r': "\1"\2', content)
            return content
        except Exception as e:
            logger.debug("JSON syntax fix failed: %s", e)
            return None

    def _validate_and_filter_questions(self, questions: list, keyword: str) -> List[str]:
        """Validate and filter extracted questions"""
        if not isinstance(questions, list):
            return []

        validated = []
        seen = set()
        profile = getattr(self, "brand_profile", None) or load_brand_profile()
        profile_terms: set[str] = set()
        if profile.is_configured():
            for phrase in [profile.category, *profile.customer_language_terms, *profile.icp_priority_topics]:
                for tok in re.findall(r"[a-z0-9]+", (phrase or "").lower()):
                    if len(tok) >= 4:
                        profile_terms.add(tok)

        for q in questions:
            if not isinstance(q, str):
                continue

            q = q.strip()
            # Relaxed length check - allow shorter questions and longer ones
            if len(q) < 8 or len(q) > 300:
                continue

            if not q.endswith('?'):
                continue

            # Check for duplicates
            q_lower = q.lower()
            if q_lower in seen:
                continue
            seen.add(q_lower)

            # Relaxed quality check - PAA questions are related to search intent
            # but don't always contain the exact keyword parts
            keyword_lower = keyword.lower()
            # Allow questions that contain at least one significant word from keyword
            # or overlap configured BrandProfile domain terminology
            q_words = set(re.findall(r"[a-z0-9]+", q_lower))
            significant_words = [w for w in re.findall(r"[a-z0-9]+", keyword_lower) if len(w) > 2]
            has_relevance = any(word in q_lower for word in significant_words) or bool(q_words & profile_terms)

            if not has_relevance:
                continue

            validated.append(q)

        # Ensure we have at least 8 questions, pad with basic ones if needed
        if len(validated) < 8:
            basic_questions = [
                f"What is {keyword}?",
                f"How does {keyword} work?",
                f"What are the benefits of {keyword}?",
                f"How much does {keyword} cost?",
                f"What are the best {keyword} tools?",
                f"How to implement {keyword}?",
                f"What are {keyword} features?",
                f"Why use {keyword}?"
            ]
            for q in basic_questions:
                if q not in validated and len(validated) < 12:
                    validated.append(q)

        return validated[:12]

    async def _generate_fallback_questions(self, keyword: str) -> List[str]:
        """Generate fallback questions using SmartAIRouter"""
        try:
            async with SmartAIRouter() as ai:
                questions = await ai.generate_questions(keyword, count=10)
                # Filter to questions only
                return [q for q in questions if '?' in q][:10]
        except Exception as e:
            logger.warning(f"AI fallback failed: {e}")
            # Ultimate fallback
            return [
                f"What is {keyword}?",
                f"How does {keyword} work?",
                f"What are the benefits of {keyword}?",
                f"How much does {keyword} cost?",
                f"What are the best {keyword} tools?",
                f"How to implement {keyword}?",
                f"What are {keyword} features?",
                f"Why use {keyword}?",
                f"How to choose {keyword}?",
                f"What problems does {keyword} solve?"
            ]
    
    async def _get_gsc_data(self, keyword: str) -> Dict[str, Any]:
        """Get Google Search Console data for the keyword with traffic potential scoring.

        Implements Traffic Maximization Formula:
        Traffic Potential = Impressions × Expected CTR at Target Position

        This quantifies keyword value beyond raw impressions - a keyword at
        position 5 with 1000 impressions has more potential than one at
        position 20 with 2000 impressions.
        """
        if not self._gsc_service:
            return {}

        try:
            end_date = datetime.now() - timedelta(days=3)
            start_date = end_date - timedelta(days=90)  # Extended to 90 days for better data

            # Use the core keyword only - keep it simple
            request_body = {
                'startDate': start_date.strftime('%Y-%m-%d'),
                'endDate': end_date.strftime('%Y-%m-%d'),
                'dimensions': ['query'],
                'dimensionFilterGroups': [{
                    'filters': [{
                        'dimension': 'query',
                        'expression': keyword,
                        'operator': 'contains'
                    }]
                }],
                'rowLimit': 50  # Get more keywords for comprehensive analysis
            }

            request = self._gsc_service.searchanalytics().query(
                siteUrl=self.site_url,
                body=request_body
            )
            response = await asyncio.wait_for(asyncio.to_thread(request.execute), timeout=30)

            if 'rows' in response and response['rows']:
                # Process all keywords with traffic potential
                keyword_data = []
                for row in response['rows']:
                    impressions = int(float(row.get('impressions', 0) or 0))
                    position = float(row.get('position', 100) or 100)
                    traffic_potential = calculate_traffic_potential(impressions, position)

                    keyword_data.append({
                        'query': row.get('keys', [keyword])[0],
                        'impressions': impressions,
                        'clicks': int(float(row.get('clicks', 0) or 0)),
                        'ctr': float(row.get('ctr', 0) or 0),
                        'position': position,
                        'traffic_potential': traffic_potential
                    })

                # Sort by traffic potential (not just clicks) for prioritization
                keyword_data.sort(key=lambda x: x['traffic_potential'], reverse=True)

                # Find primary keyword (highest traffic potential with 4+ words preferred)
                high_intent = [k for k in keyword_data if len(k['query'].split()) >= 4]
                primary = high_intent[0] if high_intent else keyword_data[0]

                # Get secondary keywords for H2 distribution (top 4-10 by position)
                secondaries = sorted(
                    [k for k in keyword_data if k != primary and k['impressions'] > 10],
                    key=lambda x: x['position']
                )[:10]

                # Extract question keywords for FAQ
                questions = [
                    k for k in keyword_data
                    if any(k['query'].lower().startswith(q) for q in
                           ['what', 'how', 'why', 'when', 'where', 'which', 'who', 'can', 'does', 'is'])
                ]

                return {
                    'primary_keyword': primary,
                    'secondary_keywords': secondaries[:5],  # Top 5 for H2s
                    'questions': questions[:10],  # For FAQ section
                    'all_keywords': keyword_data,
                    'total_traffic_potential': sum(k['traffic_potential'] for k in keyword_data),
                    # Legacy fields for backwards compatibility
                    'impressions': primary.get('impressions', 0),
                    'clicks': primary.get('clicks', 0),
                    'ctr': primary.get('ctr', 0),
                    'position': primary.get('position', 0),
                    'query': primary.get('query', keyword)
                }

        except Exception as e:
            logger.warning(f"GSC query failed for '{keyword}': {e}")

        return {}

    async def get_gsc_content_recommendations(self, keyword: str) -> Dict[str, Any]:
        """
        Get comprehensive GSC-driven recommendations for content creation.

        Uses Traffic Maximization Formula for keyword prioritization and
        provides actionable insights:
        - Primary keyword (use EXACT in title)
        - Secondary keywords (distribute across H2s)
        - Question keywords (use for FAQ section)
        - Traffic potential estimate

        Args:
            keyword: Target keyword for content

        Returns:
            Dict with structured recommendations for content generation
        """
        try:
            async with GSCAnalyzer() as gsc:
                if not gsc.is_available:
                    logger.warning("GSC not available for content recommendations")
                    return {'available': False}

                # Get comprehensive recommendations
                recommendations = await gsc.get_content_recommendations(keyword)

                # Log key insights
                primary = recommendations.get('primary_keyword', {})
                if primary.get('query'):
                    traffic_potential = primary.get('traffic_potential', 0)
                    logger.info(f"🎯 GSC Primary Keyword: '{primary['query']}' (Traffic Potential: {traffic_potential:.0f})")

                    if primary.get('position'):
                        logger.info(f"   Current Position: {primary['position']:.1f}")

                secondaries = recommendations.get('secondary_keywords', [])
                if secondaries:
                    logger.info(f"📊 Secondary Keywords for H2s: {len(secondaries)}")
                    for i, kw in enumerate(secondaries[:4], 1):
                        logger.info(f"   H2 {i}: '{kw['query']}' (pos: {kw['position']:.1f})")

                faq_questions = recommendations.get('faq_questions', [])
                if faq_questions:
                    logger.info(f"❓ GSC-derived FAQ questions: {len(faq_questions)}")

                return {
                    'available': True,
                    **recommendations
                }

        except Exception as e:
            logger.error(f"Failed to get GSC recommendations: {e}")
            return {'available': False, 'error': str(e)}

    def _analyze_content_gaps(self, serp_data: Dict[str, Any]) -> List[str]:
        """Identify content gaps from SERP analysis"""
        gaps = []
        
        # Common topics in top results
        search_results = serp_data.get("search_results", [])
        snippets = []
        
        # Handle different data structures safely
        for r in search_results:
            if isinstance(r, dict):
                snippets.append(r.get("snippet", ""))
            elif isinstance(r, str):
                snippets.append(r)
        
        all_snippets = " ".join(snippets)
        
        # Simple gap detection based on common patterns
        patterns = {
            "how to": r"how to \w+",
            "benefits": r"benefits of|advantages",
            "vs/comparison": r"vs\.|versus|compared to",
            "best practices": r"best practices|tips",
            "tools": r"tools?|software|platforms?",
            "guide": r"guide|tutorial|step.by.step",
            "cost/pricing": r"cost|pricing|price|how much",
            "examples": r"examples?|case studies"
        }
        
        for gap_type, pattern in patterns.items():
            if re.search(pattern, all_snippets, re.IGNORECASE):
                gaps.append(gap_type)
        
        return gaps
    
    def _calculate_optimal_length(self, serp_data: Dict[str, Any]) -> int:
        """Calculate recommended content length based on SERP competition"""
        # Simple heuristic based on competition
        num_results = len(serp_data.get("search_results", []))
        base_length = 2000

        # Add length for competitive keywords
        if num_results >= 8:
            base_length += 500

        # Add length for content gaps
        gaps = serp_data.get("content_gaps", [])
        base_length += len(gaps) * 200

        # Cap at reasonable maximum
        return min(base_length, 3500)

    def _extract_content_gaps_from_analysis(self, serp_analysis: Dict[str, Any]) -> List[str]:
        """Extract content gaps from Perplexity SERP analysis"""
        if not serp_analysis or not isinstance(serp_analysis, dict):
            return []

        analysis_text = serp_analysis.get("analysis", "").lower()
        gaps = []

        # Look for common gap indicators in the analysis
        gap_indicators = [
            "missing", "lacking", "no content", "gap", "opportunity",
            "not covered", "overlooked", "missing information", "needs more",
            "lacks", "doesn't include", "missing from"
        ]

        # Extract potential gaps from analysis text
        for indicator in gap_indicators:
            if indicator in analysis_text:
                # Try to extract the topic after the indicator
                start_idx = analysis_text.find(indicator)
                if start_idx != -1:
                    # Get context around the indicator
                    context_start = max(0, start_idx - 50)
                    context_end = min(len(analysis_text), start_idx + 100)
                    context = analysis_text[context_start:context_end]

                    # Extract meaningful phrases
                    sentences = context.split('.')
                    for sentence in sentences:
                        if indicator in sentence:
                            # Clean up the sentence
                            clean_sentence = sentence.strip()
                            if len(clean_sentence) > 20 and len(clean_sentence) < 100:
                                gaps.append(clean_sentence)

        # Deduplicate and limit
        unique_gaps = list(set(gaps))[:5]  # Limit to 5 gaps
        return unique_gaps

    def _calculate_length_from_analysis(self, serp_analysis: Dict[str, Any]) -> int:
        """Calculate recommended content length from SERP analysis"""
        if not serp_analysis or not isinstance(serp_analysis, dict):
            return 2500

        analysis_text = serp_analysis.get("analysis", "").lower()
        base_length = 2000

        # Increase length for competitive signals
        competitive_signals = [
            "highly competitive", "competitive market", "many competitors",
            "top results", "featured snippets", "comprehensive guides",
            "in-depth analysis", "detailed reviews"
        ]

        for signal in competitive_signals:
            if signal in analysis_text:
                base_length += 300

        # Increase for content gaps
        if "gap" in analysis_text or "missing" in analysis_text:
            base_length += 400

        # Cap at reasonable maximum
        return min(base_length, 3500)
    
    async def _analyze_serp_landscape(self, keyword: str, style: str = "standard", depth: str = "standard") -> Dict[str, Any]:
        """Deep SERP analysis using Perplexity sonar-reasoning-pro with style-aware quality filtering"""
        if not self.perplexity_key:
            logger.warning("⚠️  Perplexity API key not found - skipping SERP analysis")
            return {}

        # Dynamic date context for freshness (SEO best practice)
        current_date = datetime.now().strftime("%B %Y")  # e.g., "January 2026"
        current_year = datetime.now().strftime("%Y")     # e.g., "2026"
        previous_year = str(int(current_year) - 1)       # e.g., "2025"

        # Determine model before logging
        if style == "research":
            model = "sonar-deep-research"
            timeout = 1200  # 20 minutes
        else:
            model = "sonar-reasoning-pro"
            profile = DEPTH_PROFILES.get(depth, DEPTH_PROFILES["standard"])
            timeout = profile["serp_timeout"]  # 180s quick / 300s otherwise

        logger.info(f"🔍 Starting SERP analysis using {model} (timeout: {timeout}s)...")

        headers = {
            "Authorization": f"Bearer {self.perplexity_key}",
            "Content-Type": "application/json"
        }

        # Tiered quality instructions based on content style (with dynamic dates)
        if style == "research":
            quality_filter = f"""
RESEARCH-GRADE SOURCES REQUIRED - Must meet these quality standards:
CURRENT DATE CONTEXT: {current_date}

SOURCE CATEGORIES (of equivalent reputation/quality/trust):
* Market research firms with published methodologies
* Survey platforms with disclosed sample sizes and methods
* Think tanks & consulting firms with peer-reviewed analysis
* Academic journals and university research publications
* Government data sources and official statistics
* Industry research organizations with transparent data collection

QUALITY REQUIREMENTS:
* Cited data with sources and methodology disclosed
* Recent publication ({previous_year}-{current_year} strongly preferred)
* Author credentials and expertise verified
* Peer-reviewed or editorially vetted content
* Transparent data collection and analysis methods

- REJECT: opinion blogs, promotional content, uncited claims, aggregator sites without original research
"""
        else:
            quality_filter = f"""
PREFER credible sources:
CURRENT DATE CONTEXT: {current_date}
* Established publications with specific data and examples
* Recent content ({previous_year}-{current_year} preferred)
* Expert authors with credentials
* Sources that cite data or provide evidence

- AVOID: pure opinion, outdated (pre-{int(current_year)-2}), overly promotional
"""

        prompt = _topic_context_block(getattr(self, '_keyword_context', '')) + f"""Analyze the SERP landscape for "{keyword}":
{quality_filter}

1. Top ranking domains and their content approach
2. Common content formats (guides, tools, comparisons)
3. Average content depth and comprehensiveness
4. Unique angles competitors are taking
5. Missing topics that users are searching for
6. Featured snippets and their content structure

Provide actionable insights for creating content that will outrank competitors."""

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "search_recency_filter": "month",
            "return_citations": True
        }

        # Research style: exclude low-credibility hosts at the API level so the
        # returned citations skew toward primary/analyst sources. Perplexity caps
        # search_domain_filter at 20 entries; a leading '-' denylists a domain.
        if style == "research":
            payload["search_domain_filter"] = [
                "-pinterest.com", "-quora.com", "-reddit.com", "-medium.com",
                "-facebook.com", "-x.com", "-twitter.com", "-linkedin.com",
                "-youtube.com", "-tiktok.com", "-slideshare.net", "-scribd.com",
                "-ezinearticles.com", "-hubpages.com", "-blogspot.com",
            ]
            logger.debug(
                f"Research style: SERP credibility denylist active "
                f"({len(payload['search_domain_filter'])} domains)")

        try:
            logger.info("   ⏳ Calling Sonar SERP research API...")
            data = await post_sonar_chat(self.session, payload, timeout=timeout, explicit_key=self.perplexity_key)
            if data and data.get("choices"):
                analysis = data["choices"][0]["message"]["content"]
                citations = data.get("citations", [])
                search_results = data.get("search_results", [])
                logger.info(f"   ✅ Sonar analysis complete: {len(analysis)} chars, {len(citations)} citations")
                return {
                    "analysis": analysis,
                    "citations": citations,
                    "search_results": search_results
                }
            logger.warning("   ⚠️  Sonar SERP API returned empty result")
            return {}

        except asyncio.TimeoutError:
            logger.error(f"   ❌ Sonar API timeout after {timeout}s")
            return {}
        except Exception as e:
            logger.error(f"   ❌ SERP analysis error: {e}")
            return {}

    async def search_research_indexes(
        self,
        keyword: str,
        max_results: int = 10,
        style: str = "research",
    ) -> Dict[str, Any]:
        """Optional Interfaze web-search enrichment for research-grade sources.

        Calls Interfaze ``interfaze-beta`` (OpenAI-compatible) with a
        ``<task>web_search</task>`` system message and keeps only Tier A/B
        results. Disabled silently when ``INTERFAZE_API_KEY`` is absent — this
        step never fails the pipeline.
        """
        key = os.getenv("INTERFAZE_API_KEY")
        if not key:
            logger.debug("Interfaze key not set - skipping research-index search")
            return {"sources": [], "enabled": False}

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        query = (
            f'Authoritative, citable research, market data, statistics and '
            f'studies about "{keyword}". Prefer primary sources: academic '
            f'journals, government data, analyst and market-research firms, '
            f'and official reports with disclosed methodology.'
        )
        payload = {
            "model": "interfaze-beta",
            "messages": [
                {"role": "system", "content": "<task>web_search</task>"},
                {"role": "user", "content": query},
            ],
        }

        logger.info(f"🔍 Searching Interfaze research indexes for '{keyword}'...")
        t0 = time.time()
        try:
            logger.info("   ⏳ Calling Interfaze research-index search...")
            async with self.session.post(
                "https://api.interfaze.ai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=120,
            ) as response:
                if response.status != 200:
                    dt = time.time() - t0
                    logger.warning(
                        f"   ⚠️  Interfaze search failed: HTTP {response.status} ({dt:.1f}s)"
                    )
                    return {"sources": [], "enabled": False}
                data = await response.json()

            raw_results: List[Dict[str, Any]] = []
            for block in data.get("precontext", []) or []:
                for item in (block or {}).get("result", []) or []:
                    if isinstance(item, dict) and item.get("url"):
                        raw_results.append(item)

            sources: List[Dict[str, Any]] = []
            for item in raw_results[: max_results * 3]:
                norm = _normalize_source(item, "interfaze")
                if norm and norm["credibility_tier"] in ("A", "B"):
                    sources.append(norm)

            dt = time.time() - t0
            logger.info(
                f"   ✅ Interfaze research-index: {len(sources)}/"
                f"{len(raw_results)} sources kept (Tier A/B) ({dt:.1f}s)"
            )
            logger.info(
                f"📊 Interfaze done: {len(sources)} verified sources · "
                f"{len(raw_results)} raw results"
            )
            return {
                "sources": sources[:max_results],
                "enabled": True,
                "raw_count": len(raw_results),
            }

        except asyncio.TimeoutError:
            dt = time.time() - t0
            logger.warning(f"   ⚠️  Interfaze search timeout ({dt:.1f}s)")
            return {"sources": [], "enabled": False}
        except Exception as e:
            dt = time.time() - t0
            logger.warning(f"   ⚠️  Interfaze search error: {e} ({dt:.1f}s)")
            logger.debug("search_research_indexes traceback", exc_info=True)
            return {"sources": [], "enabled": False}

    async def _discover_topical_insights(
        self,
        keyword: str,
        topic_scope: str = "broad",
        max_insights: int = 10,
        keyword_context: str = ""
    ) -> Dict[str, Any]:
        """
        Discover topical authority insights using Perplexity sonar-reasoning-pro.

        This is SEPARATE from PAA extraction - focuses on:
        - Industry insights & recent developments
        - Expert opinions & thought leadership
        - Data points & statistics from authoritative sources
        - Case studies & real-world examples

        Args:
            keyword: Target keyword to research
            topic_scope: Search breadth - "narrow" (specific solutions),
                        "broad" (industry trends), "industry" (market analysis)
            max_insights: Maximum insights to return after filtering

        Returns:
            Dict with insights, sources, and confidence levels
        """
        if not self.perplexity_key:
            logger.warning("⚠️  Perplexity API key not found - skipping topical insights")
            return {'insights': [], 'sources': [], 'enabled': False}

        self._keyword_context = keyword_context or getattr(self, '_keyword_context', '')
        logger.info(f"🔍 Discovering topical insights for '{keyword}' (scope: {topic_scope})...")

        # Dynamic date context
        current_date = datetime.now().strftime("%B %Y")
        current_year = datetime.now().strftime("%Y")
        previous_year = str(int(current_year) - 1)

        # Scope-based search configuration
        scope_prompts = {
            "narrow": f"""Focus on SPECIFIC SOLUTIONS and tools related to "{keyword}":
- Feature comparisons between leading solutions
- Implementation case studies with quantified outcomes
- Pricing and ROI data from real deployments
- Expert reviews and evaluations""",
            "broad": f"""Explore INDUSTRY TRENDS and best practices for "{keyword}":
- Emerging trends and innovations in this space
- Best practices from industry leaders
- Common challenges and proven solutions
- Expert perspectives and thought leadership""",
            "industry": f"""Analyze MARKET DYNAMICS for "{keyword}":
- Market size, growth rates, and projections
- Competitive landscape and key players
- Investment and funding trends
- Regulatory and compliance considerations"""
        }

        scope_prompt = scope_prompts.get(topic_scope, scope_prompts["broad"])

        icp_filter = load_icp_context_for_research()
        prompt = _topic_context_block(getattr(self, '_keyword_context', '')) + f"""Research authoritative insights about "{keyword}" for content creation.

{scope_prompt}

CURRENT DATE: {current_date}
{icp_filter}
QUALITY REQUIREMENTS:
- Recent sources ({previous_year}-{current_year} strongly preferred)
- Specific data points with percentages, dollar amounts, timeframes
- Named sources (companies, research firms, publications)
- Actionable insights that can inform content creation

Return 8-12 distinct insights, each with:
1. The specific insight or data point
2. Source attribution (company/publication name)
3. Why this matters for the target audience
4. Confidence level: RESEARCH (peer-reviewed/major research firms), INDUSTRY (established publications/vendors), or OBSERVATIONAL (community/practitioner sources)

Format each insight as:
INSIGHT: [Specific data point or finding]
SOURCE: [Named source]
RELEVANCE: [Why this matters]
CONFIDENCE: [RESEARCH/INDUSTRY/OBSERVATIONAL]
---"""

        payload = {
            "model": "sonar-reasoning-pro",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "search_recency_filter": "month",
            "return_citations": True
        }

        try:
            data = await post_sonar_chat(self.session, payload, timeout=120, explicit_key=self.perplexity_key)
            if data and data.get("choices"):
                content = data["choices"][0]["message"]["content"]
                citations = data.get("citations", [])
                parsed_insights = self._parse_topical_insights(content, keyword)
                filtered_insights = await self._filter_topical_insights(parsed_insights, keyword)
                classified_insights = self._classify_source_confidence(filtered_insights)
                final_insights = classified_insights[:max_insights]
                logger.info(f"   ✅ Topical insights: {len(final_insights)}/{len(parsed_insights)} passed filter")
                return {
                    'insights': final_insights,
                    'sources': citations,
                    'scope': topic_scope,
                    'enabled': True,
                    'raw_count': len(parsed_insights),
                    'filtered_count': len(final_insights)
                }
            logger.warning("   ⚠️  Topical insights API returned empty result")
            return {'insights': [], 'sources': [], 'enabled': False}

        except asyncio.TimeoutError:
            logger.error(f"   ❌ Topical insights timeout after 120s")
            return {'insights': [], 'sources': [], 'enabled': False}
        except Exception as e:
            logger.error(f"   ❌ Topical insights error: {e}")
            return {'insights': [], 'sources': [], 'enabled': False}

    def _parse_topical_insights(self, content: str, keyword: str) -> List[Dict[str, Any]]:
        """Parse structured insights from Perplexity response."""
        insights = []

        # Split by separator
        blocks = content.split('---')

        for block in blocks:
            if not block.strip():
                continue

            insight = {
                'text': '',
                'source': '',
                'relevance': '',
                'confidence': 'OBSERVATIONAL'  # Default
            }

            lines = block.strip().split('\n')
            for line in lines:
                line = line.strip()
                if line.startswith('INSIGHT:'):
                    insight['text'] = line.replace('INSIGHT:', '').strip()
                elif line.startswith('SOURCE:'):
                    insight['source'] = line.replace('SOURCE:', '').strip()
                elif line.startswith('RELEVANCE:'):
                    insight['relevance'] = line.replace('RELEVANCE:', '').strip()
                elif line.startswith('CONFIDENCE:'):
                    conf = line.replace('CONFIDENCE:', '').strip().upper()
                    if conf in ['RESEARCH', 'INDUSTRY', 'OBSERVATIONAL']:
                        insight['confidence'] = conf

            # Only add if we have meaningful content
            if insight['text'] and len(insight['text']) > 20:
                insights.append(insight)

        # Fallback: if structured parsing fails, extract paragraphs
        if not insights:
            paragraphs = [p.strip() for p in content.split('\n\n') if p.strip() and len(p.strip()) > 50]
            for para in paragraphs[:10]:
                insights.append({
                    'text': para,
                    'source': 'Perplexity Research',
                    'relevance': f'Related to {keyword}',
                    'confidence': 'INDUSTRY'
                })

        return insights

    async def _filter_topical_insights(self, insights: List[Dict[str, Any]], keyword: str) -> List[Dict[str, Any]]:
        """Filter insights for topical relevance, brand ICP alignment, and concrete evidence."""
        if not insights:
            return []

        filtered = []
        keyword_lower = keyword.lower()
        profile = load_brand_profile()
        icp_positive = [
            t.lower()
            for t in (profile.icp_priority_topics + profile.customer_language_terms + ([profile.category] if profile.category else []))
            if t and len(t) > 2
        ]

        for insight in insights:
            text_lower = insight.get('text', '').lower()
            relevance_score = 0

            keyword_parts = keyword_lower.split()
            for part in keyword_parts:
                if len(part) > 3 and part in text_lower:
                    relevance_score += 2

            for term in icp_positive:
                if term in text_lower:
                    relevance_score += 1

            if any(char.isdigit() for char in insight.get('text', '')):
                relevance_score += 2
            if '%' in insight.get('text', ''):
                relevance_score += 1
            if '$' in insight.get('text', ''):
                relevance_score += 1
            if insight.get('source') and insight['source'] != 'Perplexity Research':
                relevance_score += 1

            if relevance_score >= 2:
                insight['relevance_score'] = relevance_score
                filtered.append(insight)

        filtered.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
        return filtered

    def _classify_source_confidence(self, insights: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Classify and validate source confidence levels.

        RESEARCH-GRADE: Peer-reviewed, .edu/.gov, Gartner/Forrester/McKinsey
        INDUSTRY: Established publications, vendor reports
        OBSERVATIONAL: Community insights, practitioner sources
        """
        research_indicators = [
            'gartner', 'forrester', 'mckinsey', 'harvard', 'mit', 'stanford',
            '.edu', '.gov', 'research', 'study', 'survey', 'pew', 'nielsen',
            'deloitte', 'accenture', 'bcg', 'bain', 'idc', 'statista'
        ]

        industry_indicators = [
            'hubspot', 'salesforce', 'marketo', 'linkedin', 'google',
            'meta', 'facebook', 'microsoft', 'adobe', 'oracle',
            'martech', 'adweek', 'marketing week', 'techcrunch', 'forbes',
            'harvard business review', 'hbr', 'entrepreneur'
        ]

        for insight in insights:
            source_lower = insight.get('source', '').lower()
            text_lower = insight.get('text', '').lower()

            # Check for research-grade indicators
            if any(ind in source_lower or ind in text_lower for ind in research_indicators):
                insight['confidence'] = 'RESEARCH'
            # Check for industry indicators
            elif any(ind in source_lower or ind in text_lower for ind in industry_indicators):
                insight['confidence'] = 'INDUSTRY'
            # Default to observational if not already set higher
            elif insight.get('confidence') not in ['RESEARCH', 'INDUSTRY']:
                insight['confidence'] = 'OBSERVATIONAL'

        return insights


class CommunityResearcher:
    """Handles Reddit, Quora, and community insights"""

    def __init__(self):
        self.ai = None
        self.session = None

    async def _extract_question_topic(self, keyword: str, ai: Optional[SmartAIRouter] = None) -> str:
        """Extract core topic from a long keyword using AI for summarization."""
        try:
            prompt = _topic_context_block(getattr(self, '_keyword_context', '')) + f"""
            Extract the core topic from the following keyword. The topic should be a concise phrase of 5-8 words that captures the main subject and the user's intent.
            It should be suitable for generating search queries for community research on platforms like Reddit and Quora.

            - Preserve key concepts, entities, and relationships.
            - Remove generic filler words but keep important context.
            - The output should be a phrase of 5-8 words, not a full sentence.

            Keyword: "{keyword}"

            Core Topic (5-8 words):
            """
            
            router = ai or self.ai
            if router and router.or_client:
                topic_raw = await router._nebius_with_fallback(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=50,
                    purpose="question topic extraction",
                    reasoning_enabled=False,
                )
                if topic_raw:
                    topic = topic_raw.strip().strip('"')
                    if topic:
                        logger.info(f"Extracted question topic: '{topic}' from '{keyword}'")
                        return topic

        except Exception as e:
            err_line = ((str(e).splitlines() or [type(e).__name__])[0] or type(e).__name__)[:160]
            logger.warning(f"AI topic extraction failed ({err_line}); falling back to rule-based method.")
            logger.debug("Full exception repr: %r", e)

        # Fallback to a simpler, improved rule-based method
        # Clean the string
        cleaned_keyword = re.sub(r'[()]', '', keyword)
        words = cleaned_keyword.split()

        stop_words = {
            'a', 'an', 'the', 'and', 'or', 'in', 'on', 'at', 'by', 'from', 'to', 'vs',
            'how', 'to', 'it', 'for', 'as', 'with'
        }
        
        # A simple noun phrase extractor (very basic)
        content_words = [w for w in words if w.lower() not in stop_words and len(w) > 2]
        
        # Join and limit length
        result = ' '.join(content_words)
        if len(result) > 100: # Limit to a reasonable length
            result = result[:100]

        logger.info(f"Extracted question topic (fallback): '{result}' from '{keyword}'")
        return result

    async def __aenter__(self):
        self.ai = SmartAIRouter()
        self.session = aiohttp.ClientSession(timeout=_AIOHTTP_TIMEOUT)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
            self.session = None
        await self.ai.__aexit__(exc_type, exc_val, exc_tb)

    async def mine_reddit(
        self,
        keyword: str,
        serp_context: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        style: Optional[str] = None,
        progress_callback: Optional[Callable[..., None]] = None,
        keyword_context: str = "",
    ) -> Dict[str, Any]:
        """Extract Reddit discussions using Perplexity with Gemini fallback.

        Args:
            keyword: Target keyword
            serp_context: Optional SERP context for better targeting
            limit: Optional limit on number of insights (for --nrl flag)
            style: Optional content style for question type optimization (e.g., 'research' for data-driven questions)
            progress_callback: Optional ``cb(current, total, *, insights=None, query=None, note=None)``
                called before each query and after each result so callers can render
                a live progress bar in the research panel.
        """
        self._keyword_context = keyword_context or getattr(self, '_keyword_context', '')
        try:
            # Initialize AI router for question generation and community research
            async with self.ai as ai:
                # Extract core topic for question generation
                question_topic = await self._extract_question_topic(keyword, ai=ai)

                # Enhance topic with SERP context if available
                if serp_context and 'paa_questions' in serp_context:
                    paa_questions = serp_context['paa_questions'][:5]  # Use top 5 PAA questions
                    if paa_questions:
                        # Use AI to blend topic with PAA insights
                        enhanced_topic = await self._enhance_topic_with_serp(question_topic, paa_questions, ai)
                        question_topic = enhanced_topic or question_topic
                        logger.info(f"Enhanced topic with SERP context: '{question_topic}'")

                # Generate 100 natural language questions optimized for Reddit search
                logger.info(f"Generating 100 Reddit-optimized queries for '{keyword}'")
                context_questions = serp_context.get('paa_questions', [])[:3] if serp_context else []
                # Surface "generating queries…" in the live row immediately,
                # tick an elapsed-seconds heartbeat while each batch is in
                # flight, AND expose the batch-level (done/total) counter so
                # the user sees discrete advancement every ~7s as each Gemini
                # call lands — instead of sitting at 0/N for the full duration.
                _early_total = limit if limit is not None else 10
                _batch_state = {"done": 0, "total": 100}

                def _on_question_batch(done: int, total: int) -> None:
                    _batch_state["done"] = done
                    _batch_state["total"] = total
                    if progress_callback:
                        try:
                            progress_callback(
                                0, _early_total, insights=0, query=None,
                                note=f"generating queries… {done}/{total}",
                            )
                        except Exception:
                            pass

                async def _reddit_heartbeat():
                    t0 = time.monotonic()
                    try:
                        while True:
                            elapsed = time.monotonic() - t0
                            if progress_callback:
                                try:
                                    progress_callback(
                                        0, _early_total, insights=0, query=None,
                                        note=(
                                            f"generating queries… "
                                            f"{_batch_state['done']}/{_batch_state['total']} · "
                                            f"{elapsed:.0f}s"
                                        ),
                                    )
                                except Exception:
                                    pass
                            await asyncio.sleep(0.5)
                    except asyncio.CancelledError:
                        pass

                _hb = asyncio.create_task(_reddit_heartbeat())
                try:
                    all_questions = await ai.generate_questions(
                        question_topic,
                        count=100,
                        context_questions=context_questions,
                        platform="reddit",
                        style=style,
                        progress_callback=_on_question_batch,
                    )
                finally:
                    _hb.cancel()
                    try:
                        await _hb
                    except (asyncio.CancelledError, Exception):
                        pass
                logger.info(f"Generated {len(all_questions)} Reddit-optimized questions")

                # Find highly relevant insights - configurable max queries with minimum enforcement
                all_insights = []
                all_citations = []
                used_questions = []
                successful_queries = 0
                consecutive_failures = 0

                # Determine max queries and minimum based on limit parameter
                # Research mode (limit >= 25): minimum 25 queries, then 1 failure allowed to stop
                # Limited mode (limit = 3): max 3 queries
                # Unlimited mode: max 10 queries
                if limit is not None:
                    max_successful_queries = limit
                    # For research mode, enforce 25 query minimum
                    min_successful_queries = 25 if limit >= 25 else 0

                    if limit >= 25:
                        logger.info(f"Mining Reddit (research mode): target {limit} queries, minimum {min_successful_queries}")
                        logger.info(f"Reddit strategy: 5 retries before {min_successful_queries} queries, then 1 failure stops mining")
                    else:
                        logger.info(f"Mining Reddit (limited mode): target {limit} queries, allows 5 consecutive failures")
                else:
                    max_successful_queries = 10  # Default cap for unlimited mode
                    min_successful_queries = 0
                    logger.info(f"Mining Reddit for relevant insights (max {max_successful_queries} queries, allows 5 consecutive failures)")

                question_index = 0

                if progress_callback:
                    progress_callback(0, max_successful_queries, insights=0, query=None, note="queued")

                while successful_queries < max_successful_queries and question_index < len(all_questions):
                    current_question = [all_questions[question_index]]
                    q_preview = current_question[0]

                    logger.info(f"🔍 Reddit query {question_index + 1}: '{q_preview[:80]}'")
                    if progress_callback:
                        progress_callback(
                            successful_queries, max_successful_queries,
                            insights=len(all_insights), query=q_preview, note="querying…",
                        )

                    t0 = time.time()
                    reddit_results = await ai.research_with_compound(current_question, platform="reddit")
                    dt = time.time() - t0

                    batch_insights = reddit_results.get('insights', [])
                    batch_citations = reddit_results.get('citations', [])

                    if batch_insights:
                        all_insights.extend(batch_insights)
                        all_citations.extend(batch_citations)
                        used_questions.append(current_question[0])
                        successful_queries += 1
                        consecutive_failures = 0

                        progress_pct = int((successful_queries / max_successful_queries) * 100)
                        logger.info(
                            f"✅ Reddit +{len(batch_insights)} insights ({dt:.1f}s) "
                            f"[{successful_queries}/{max_successful_queries} · {progress_pct}%] "
                            f"total={len(all_insights)} citations={len(all_citations)}"
                        )
                        if progress_callback:
                            progress_callback(
                                successful_queries, max_successful_queries,
                                insights=len(all_insights), query=q_preview,
                                note=f"+{len(batch_insights)} ({dt:.1f}s)",
                            )
                    else:
                        consecutive_failures += 1
                        if min_successful_queries > 0 and successful_queries >= min_successful_queries:
                            failure_tolerance = 1
                        else:
                            failure_tolerance = 5

                        logger.info(
                            f"❌ Reddit no hits ({dt:.1f}s) consecutive_failures="
                            f"{consecutive_failures}/{failure_tolerance}"
                        )
                        if progress_callback:
                            progress_callback(
                                successful_queries, max_successful_queries,
                                insights=len(all_insights), query=q_preview,
                                note=f"miss {consecutive_failures}/{failure_tolerance} ({dt:.1f}s)",
                            )

                        if consecutive_failures >= failure_tolerance:
                            if successful_queries >= min_successful_queries:
                                logger.warning(
                                    f"⚠️  Reddit stop: {consecutive_failures} failure(s); minimum "
                                    f"{min_successful_queries} met ({successful_queries} total)"
                                )
                                break
                            else:
                                logger.info(
                                    f"🔄 Reddit reset: {consecutive_failures} failures but minimum "
                                    f"{min_successful_queries} not met ({successful_queries}/{min_successful_queries})"
                                )
                                consecutive_failures = 0

                    question_index += 1

                    if question_index >= len(all_questions):
                        logger.warning(f"⚠️  Reddit exhausted question pool after {successful_queries} successes")
                        break

                logger.info(
                    f"📊 Reddit done: {successful_queries} successful queries · "
                    f"{len(all_insights)} insights · {len(all_citations)} citations"
                )

                # NOTE: Do NOT truncate insights here - send ALL to AI filter for intelligent selection
                # The 'limit' parameter controls QUERIES, not insight count
                # AI filter will intelligently select the most relevant insights from the full set

                return {
                    "insights": all_insights,
                    "questions": used_questions,
                    "citations": all_citations,
                    "successful_queries": successful_queries,
                    "success": successful_queries > 0  # Success if we found any relevant insights
                }
                
        except Exception as e:
            err_line = ((str(e).splitlines() or [type(e).__name__])[0] or type(e).__name__)[:160]
            logger.error(f"mine_reddit failed ({err_line})")
            logger.debug("mine_reddit traceback", exc_info=True)
            return {"insights": [], "questions": [], "citations": [], "success": False}
    
    async def mine_quora(
        self,
        keyword: str,
        serp_context: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        style: Optional[str] = None,
        progress_callback: Optional[Callable[..., None]] = None,
        keyword_context: str = "",
    ) -> Dict[str, Any]:
        """Extract Quora questions using Perplexity with Gemini fallback.

        Args:
            keyword: Target keyword
            serp_context: Optional SERP context for better targeting
            limit: Optional limit on number of insights (for --nrl flag)
            style: Optional content style for question type optimization (e.g., 'research' for data-driven questions)
            progress_callback: Optional ``cb(current, total, *, insights=None, query=None, note=None)``
                called before each query and after each result so callers can render
                a live progress bar in the research panel.
        """
        self._keyword_context = keyword_context or getattr(self, '_keyword_context', '')
        try:
            # Initialize AI router for question generation and community research
            async with SmartAIRouter() as ai:
                # Extract core topic for question generation
                question_topic = await self._extract_question_topic(keyword, ai=ai)

                # Enhance topic with SERP context if available
                if serp_context and 'paa_questions' in serp_context:
                    paa_questions = serp_context['paa_questions'][:5]  # Use top 5 PAA questions
                    if paa_questions:
                        # Use AI to blend topic with PAA insights
                        enhanced_topic = await self._enhance_topic_with_serp(question_topic, paa_questions, ai)
                        question_topic = enhanced_topic or question_topic
                        logger.info(f"Enhanced topic with SERP context: '{question_topic}'")

                # Generate 100 natural language questions optimized for Quora search
                logger.info(f"Generating 100 Quora-optimized questions for '{keyword}'")
                context_questions = serp_context.get('paa_questions', [])[:3] if serp_context else []
                # Surface "generating queries…" + heartbeat + batch counter —
                # same reasoning as mine_reddit above.
                _early_total = min(limit, 5) if limit is not None else 5
                _batch_state = {"done": 0, "total": 100}

                def _on_question_batch(done: int, total: int) -> None:
                    _batch_state["done"] = done
                    _batch_state["total"] = total
                    if progress_callback:
                        try:
                            progress_callback(
                                0, _early_total, insights=0, query=None,
                                note=f"generating queries… {done}/{total}",
                            )
                        except Exception:
                            pass

                async def _quora_heartbeat():
                    t0 = time.monotonic()
                    try:
                        while True:
                            elapsed = time.monotonic() - t0
                            if progress_callback:
                                try:
                                    progress_callback(
                                        0, _early_total, insights=0, query=None,
                                        note=(
                                            f"generating queries… "
                                            f"{_batch_state['done']}/{_batch_state['total']} · "
                                            f"{elapsed:.0f}s"
                                        ),
                                    )
                                except Exception:
                                    pass
                            await asyncio.sleep(0.5)
                    except asyncio.CancelledError:
                        pass

                _hb = asyncio.create_task(_quora_heartbeat())
                try:
                    all_questions = await ai.generate_questions(
                        question_topic,
                        count=100,
                        context_questions=context_questions,
                        platform="quora",
                        style=style,
                        progress_callback=_on_question_batch,
                    )
                finally:
                    _hb.cancel()
                    try:
                        await _hb
                    except (asyncio.CancelledError, Exception):
                        pass
                logger.info(f"Generated {len(all_questions)} Quora-optimized questions")

                # Find highly relevant insights - stop after 2 consecutive failures or max 5 questions
                all_insights = []
                all_citations = []
                used_questions = []
                successful_queries = 0
                consecutive_failures = 0
                max_consecutive_failures = 2
                max_successful_queries = 5  # Cap at 5 questions
                start_index = 50 if len(all_questions) >= 100 else 0
                question_index = start_index  # Start from middle when 100+ questions exist, else from 0

                # If limit mode is active, reduce query attempts to match target
                if limit is not None:
                    max_successful_queries = min(limit, max_successful_queries)
                    logger.info(f"Mining Quora in limited mode (target: {limit} insights, max {max_successful_queries} queries)")
                else:
                    logger.info(f"Mining Quora for relevant insights (max {max_successful_queries} questions, stops after {max_consecutive_failures} consecutive failures)")

                if progress_callback:
                    progress_callback(0, max_successful_queries, insights=0, query=None, note="queued")

                while consecutive_failures < max_consecutive_failures and successful_queries < max_successful_queries and question_index < len(all_questions):
                    current_question = [all_questions[question_index]]
                    q_preview = current_question[0]

                    logger.info(f"🔍 Quora query {question_index - start_index + 1}: '{q_preview[:80]}'")
                    if progress_callback:
                        progress_callback(
                            successful_queries, max_successful_queries,
                            insights=len(all_insights), query=q_preview, note="querying…",
                        )

                    t0 = time.time()
                    quora_results = await ai.research_with_compound(current_question, platform="quora")
                    dt = time.time() - t0

                    batch_insights = quora_results.get('insights', [])
                    batch_citations = quora_results.get('citations', [])

                    if batch_insights:
                        all_insights.extend(batch_insights)
                        all_citations.extend(batch_citations)
                        used_questions.append(current_question[0])
                        successful_queries += 1
                        consecutive_failures = 0

                        progress_pct = int((successful_queries / max_successful_queries) * 100)
                        logger.info(
                            f"✅ Quora +{len(batch_insights)} insights ({dt:.1f}s) "
                            f"[{successful_queries}/{max_successful_queries} · {progress_pct}%] "
                            f"total={len(all_insights)} citations={len(all_citations)}"
                        )
                        if progress_callback:
                            progress_callback(
                                successful_queries, max_successful_queries,
                                insights=len(all_insights), query=q_preview,
                                note=f"+{len(batch_insights)} ({dt:.1f}s)",
                            )
                    else:
                        consecutive_failures += 1
                        logger.info(
                            f"❌ Quora no hits ({dt:.1f}s) consecutive_failures="
                            f"{consecutive_failures}/{max_consecutive_failures}"
                        )
                        if progress_callback:
                            progress_callback(
                                successful_queries, max_successful_queries,
                                insights=len(all_insights), query=q_preview,
                                note=f"miss {consecutive_failures}/{max_consecutive_failures} ({dt:.1f}s)",
                            )

                        if consecutive_failures >= max_consecutive_failures:
                            logger.warning(
                                f"⚠️  Quora stop: {max_consecutive_failures} consecutive failures"
                            )
                            break

                    question_index += 1

                    if question_index >= len(all_questions):
                        logger.warning(f"⚠️  Quora exhausted question pool after {successful_queries} successes")
                        break

                logger.info(
                    f"📊 Quora done: {successful_queries} successful queries · "
                    f"{len(all_insights)} insights · {len(all_citations)} citations"
                )

                # NOTE: Do NOT truncate insights here — 'limit' caps queries, not retained insights
                # (consistent with mine_reddit; downstream AI filter selects the strongest items).

                # Format results to match expected structure
                return {
                    "questions": used_questions,
                    "expert_insights": all_insights,
                    "citations": all_citations,
                    "raw_content": f"Collected {len(all_insights)} insights from {successful_queries} successful queries",
                    "successful_queries": successful_queries,
                    "success": successful_queries > 0  # Success if we found any relevant insights
                }
                
        except Exception as e:
            err_line = ((str(e).splitlines() or [type(e).__name__])[0] or type(e).__name__)[:160]
            logger.error(f"mine_quora failed ({err_line})")
            logger.debug("mine_quora traceback", exc_info=True)
            return {"questions": [], "expert_insights": [], "citations": [], "success": False}
    
    async def analyze_citations(self, content: str) -> Dict[str, Any]:
        """Verify and analyze citations in content"""
        # Extract all claims that might need citations
        claims_pattern = r'(?:According to|Studies show|Research indicates|Data shows|Reports suggest)[^.]+\.'
        claims = re.findall(claims_pattern, content, re.IGNORECASE)
        
        # Extract existing citations
        citation_pattern = r'\[(\d+)\]|\(([^)]+, \d{4})\)'
        citations = re.findall(citation_pattern, content)
        
        return {
            "claims_needing_citation": claims[:10],
            "existing_citations": len(citations),
            "citation_ratio": len(citations) / max(len(claims), 1)
        }
    
    def _extract_insights(self, text: str) -> List[str]:
        """Extract key insights from text"""
        insights = []
        
        # Look for insight patterns
        patterns = [
            r'(?:pain point|frustration|problem|issue|challenge)[:\s]+([^.]+)',
            r'(?:users? (?:report|mention|say|complain))[:\s]+([^.]+)',
            r'(?:common (?:mistake|misconception|problem))[:\s]+([^.]+)',
            r'(?:the (?:biggest|main|primary) (?:issue|problem|challenge))[:\s]+([^.]+)'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            insights.extend([m.strip() for m in matches if len(m.strip()) > 20])
        
        # Deduplicate
        seen = set()
        unique_insights = []
        for insight in insights:
            insight_lower = insight.lower()
            if insight_lower not in seen:
                seen.add(insight_lower)
                unique_insights.append(insight)
        
        return unique_insights
    
    async def _enhance_topic_with_serp(self, base_topic: str, paa_questions: List[str], ai) -> Optional[str]:
        """Use AI to enhance topic with SERP context for better question generation"""
        try:
            if not ai.or_client:
                return None

            prompt = f"""Given the base topic: "{base_topic}"

And these People Also Ask questions from search results:
{chr(10).join(f'- {q}' for q in paa_questions)}

Create an enhanced topic (2-5 words) that captures the core intent and includes key concepts from both the base topic and the PAA questions. Focus on what users are actually searching for.

Enhanced topic should be:
"""

            response = await ai._qwen_create(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=50,
                purpose="topic enhancement",
            )
            enhanced = ai._safe_content(response)
            if enhanced:
                enhanced = enhanced.strip().strip('"').strip("'")
            if enhanced and len(enhanced.split()) >= 2 and len(enhanced) < len(base_topic) + 20:
                return enhanced
        except Exception as e:
            logger.warning(f"Failed to enhance topic with SERP: {e}")

        return None

    def _extract_questions(self, text: str) -> List[str]:
        """Extract questions from text"""
        # Simple question extraction
        questions = re.findall(r'[^.!]+\?', text)

        # Clean and filter
        cleaned = []
        for q in questions:
            q = q.strip()
            if len(q) > 15 and len(q) < 200:
                # Remove quotes if present
                q = re.sub(r'^["\']+|["\']+$', '', q)
                cleaned.append(q)

        return cleaned
