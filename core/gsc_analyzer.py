"""
GSC Analyzer - Google Search Console integration for data-driven content decisions.

Based on advanced GSC analysis methodology:
- Cannibalization checking before content creation
- Keyword categorization (Rising Stars, Fading Giants, Striking Distance, High-Impact)
- Traffic Maximization Formula for quantitative keyword selection
- Real PAA question extraction from GSC queries

Usage:
    from core.gsc_analyzer import GSCAnalyzer

    async with GSCAnalyzer() as gsc:
        # Before creating new content
        result = await gsc.check_cannibalization("mcp servers for ad analytics")

        # Analyze existing page
        perf = await gsc.analyze_page("/blog/best-mcp-servers")

        # Get keyword opportunities
        keywords = await gsc.categorize_keywords(["mcp", "ad analytics", "claude integration"])
"""

import math
import os
import re
import asyncio
import logging
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
import ssl

try:
    import httplib2
    import google_auth_httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    httplib2 = None
    google_auth_httplib2 = None
    service_account = None
    build = None
    HttpError = None

from .paths import load_repo_env
from . import jev

# Load .env from the project root without overriding the process environment.
load_repo_env()
logger = logging.getLogger(__name__)


class KeywordCategory(Enum):
    """Keyword categories for SEO prioritization"""
    RISING_STAR = "rising_star"           # Improving positions/impressions
    FADING_GIANT = "fading_giant"         # Declining performance (needs refresh)
    STRIKING_DISTANCE = "striking_distance"  # Positions 11-20 (page 1 opportunities)
    HIGH_IMPACT = "high_impact"           # Positions 4-10 (quick wins)
    OPPORTUNITY = "opportunity"           # High impressions, low CTR
    TOP_PERFORMER = "top_performer"       # Positions 1-3 (protect these)
    LONG_TAIL = "long_tail"               # 4+ words, specific intent


class ContentRecommendation(Enum):
    """Content action recommendations"""
    CREATE_NEW = "create_new"             # No existing content - safe to create
    OPTIMIZE_EXISTING = "optimize_existing"  # Existing content ranks - improve it
    ABORT = "abort"                       # Strong existing content - don't cannibalize
    CONSOLIDATE = "consolidate"           # Multiple pages - consider merging


@dataclass
class KeywordData:
    """Structured keyword performance data"""
    query: str
    clicks: int = 0
    impressions: int = 0
    ctr: float = 0.0
    position: float = 0.0
    traffic_potential: float = 0.0
    category: Optional[KeywordCategory] = None
    is_question: bool = False
    word_count: int = 0

    def __post_init__(self):
        self.word_count = len(self.query.split())
        self.is_question = any(
            self.query.lower().startswith(q)
            for q in ['what', 'how', 'why', 'when', 'where', 'which', 'who', 'can', 'does', 'is']
        )


@dataclass
class PagePerformance:
    """Page performance metrics from GSC"""
    url: str
    total_clicks: int = 0
    total_impressions: int = 0
    avg_ctr: float = 0.0
    avg_position: float = 0.0
    keywords: List[KeywordData] = field(default_factory=list)
    primary_keyword: Optional[KeywordData] = None
    rising_stars: List[KeywordData] = field(default_factory=list)
    opportunities: List[KeywordData] = field(default_factory=list)
    questions: List[KeywordData] = field(default_factory=list)


@dataclass
class CannibalizationResult:
    """Result of cannibalization check"""
    keyword: str
    existing_urls: List[Dict[str, Any]] = field(default_factory=list)
    recommendation: ContentRecommendation = ContentRecommendation.CREATE_NEW
    reason: str = ""
    best_existing_url: Optional[str] = None
    best_existing_position: Optional[float] = None


# CTR expectations by position (industry averages)
CTR_BY_POSITION = {
    1: 0.32,   # 30-35%
    2: 0.17,   # 15-18%
    3: 0.11,   # 10-12%
    4: 0.08,   # 6-8%
    5: 0.07,
    6: 0.05,   # 3-5%
    7: 0.04,
    8: 0.035,
    9: 0.03,
    10: 0.025,
}


def calculate_traffic_potential(impressions: int, position: float) -> float:
    """
    Traffic Maximization Formula for keyword prioritization.

    Traffic Potential = Impressions × Expected CTR at Target Position

    This quantifies keyword value beyond just impressions - a keyword
    with 1000 impressions at position 5 has more potential than one
    with 2000 impressions at position 20.
    """
    if position <= 0 or impressions <= 0:
        return 0.0

    pos_rounded = round(position)

    if pos_rounded <= 10:
        expected_ctr = CTR_BY_POSITION.get(pos_rounded, 0.025)
    elif pos_rounded <= 20:
        expected_ctr = 0.015  # Page 2: 1-2%
    else:
        expected_ctr = 0.005  # Page 3+: <1%

    return impressions * expected_ctr


# Traffic potential (expected clicks/mo) that maps to a 100% score. Log-scaled so
# small differences at the low end matter more than the long tail: 25% ≈ 5 clicks/mo.
GSC_SCORE_FULL_POTENTIAL = 1000.0

# Fallback impressions floor used when the default 50-impression opportunity
# filter would discard every row a query actually returned (young/sparse site).
MIN_IMPRESSIONS_FLOOR = 1

# Minimum semantically-similar queries needed before an estimated opportunity
# score is meaningful; below this the estimate is noise and is not surfaced.
MIN_ESTIMATE_NEIGHBORS = 3
GSC_TRANSPORT_TIMEOUT_SECONDS = 30.0


def normalize_traffic_potential_score(traffic_potential: float) -> int:
    """Normalize raw traffic potential onto a 0-100 opportunity score."""
    if traffic_potential <= 0:
        return 0
    scaled = 100.0 * math.log1p(traffic_potential) / math.log1p(GSC_SCORE_FULL_POTENTIAL)
    return min(100, round(scaled))


# Estimated-score tuning: inferred-not-measured discount and the realistic
# landing-position clamp. The site's demonstrated position on semantically
# related queries decides where a NEW article plausibly lands — never assume
# top-5 for a young site, never worse than page 2.
ESTIMATE_DISCOUNT = 0.5
ESTIMATE_POSITION_CLAMP = (5.0, 20.0)
ESTIMATE_TOP_NEIGHBORS = 20


def estimate_traffic_potential_from_neighbors(
    neighbors: List[tuple],
) -> Dict[str, Any]:
    """Estimate a new topic's traffic potential from semantically similar queries.

    ``neighbors`` is ``[(similarity, KeywordData), ...]``. The estimate is a
    topical-authority-adjusted demand proxy:
    - est_impressions = Σ(similarity × impressions) over the top-N neighbors —
      the demand adjacent to this topic the site already sees;
    - proxy position = impression-weighted mean of neighbor positions, clamped —
      the site's demonstrated authority in this cluster decides the realistic
      landing position for a new article;
    - traffic potential = calculate_traffic_potential(est, proxy) × discount.

    Pure function (no I/O) so the math is directly unit-testable.
    """
    ranked = sorted(neighbors, key=lambda pair: pair[0], reverse=True)[:ESTIMATE_TOP_NEIGHBORS]
    ranked = [(sim, kw) for sim, kw in ranked if sim > 0 and kw.impressions > 0]
    if not ranked:
        return {
            'traffic_potential': 0.0,
            'neighbor_count': 0,
            'proxy_position': None,
            'estimated': True,
            'top_neighbors': [],
        }

    est_impressions = sum(sim * kw.impressions for sim, kw in ranked)
    weight = sum(kw.impressions for _, kw in ranked)
    raw_position = sum(kw.position * kw.impressions for _, kw in ranked) / weight
    lo, hi = ESTIMATE_POSITION_CLAMP
    proxy_position = min(max(raw_position, lo), hi)

    traffic_potential = calculate_traffic_potential(
        int(est_impressions), proxy_position) * ESTIMATE_DISCOUNT

    return {
        'traffic_potential': traffic_potential,
        'neighbor_count': len(ranked),
        'proxy_position': round(proxy_position, 1),
        'estimated': True,
        'top_neighbors': [
            {'query': kw.query, 'similarity': round(sim, 3),
             'impressions': kw.impressions, 'position': round(kw.position, 1)}
            for sim, kw in ranked[:12]
        ],
    }


def _keyword_tokens(text: str) -> set:
    """Lowercased content tokens for near-duplicate query comparison."""
    stop = {'a', 'an', 'the', 'of', 'for', 'to', 'in', 'on', 'with', 'and',
            'or', 'is', 'are', 'your', 'my', 'best', 'top', 'how', 'what'}
    return {t for t in re.findall(r'[a-z0-9]+', text.lower()) if t not in stop}


def select_inferred_secondaries(
    keyword: str,
    neighbors: List[Dict[str, Any]],
    chosen_queries: Optional[List[str]] = None,
    max_keywords: int = 5,
) -> List[Dict[str, Any]]:
    """Shape embedding-neighbor queries into inferred secondary keywords.

    When ``chosen_queries`` is provided (AI curation picks), keep those
    neighbors in the chosen order. Otherwise fall back to a deterministic
    dedupe: drop queries that are near-duplicates of the target keyword or of
    an already-kept query (high token overlap), ranked by
    similarity × impressions. Output matches the direct GSC
    ``secondary_keywords`` shape plus ``inferred: True``.
    """
    pool = [n for n in neighbors if isinstance(n, dict) and n.get('query')]
    if not pool:
        return []

    if chosen_queries:
        by_query = {n['query']: n for n in pool}
        picked = [by_query[q] for q in chosen_queries if q in by_query]
    else:
        kw_tokens = _keyword_tokens(keyword)
        picked = []
        kept_tokens: List[set] = []
        ranked = sorted(
            pool,
            key=lambda n: float(n.get('similarity') or 0) * float(n.get('impressions') or 0),
            reverse=True,
        )
        for n in ranked:
            tokens = _keyword_tokens(n['query'])
            if not tokens:
                continue
            overlaps = [kw_tokens] + kept_tokens
            if any(len(tokens & other) / len(tokens | other) >= 0.75
                   for other in overlaps if tokens | other):
                continue
            picked.append(n)
            kept_tokens.append(tokens)

    return [
        {
            'query': n['query'],
            'position': n.get('position'),
            'impressions': n.get('impressions', 0),
            'suggested_h2': True,
            'inferred': True,
        }
        for n in picked[:max_keywords]
    ]


def categorize_keyword(kw: KeywordData, historical_data: Optional[Dict] = None) -> KeywordCategory:
    """
    Categorize keyword based on SEO prioritization methodology.

    Categories:
    - TOP_PERFORMER: Positions 1-3 (protect)
    - HIGH_IMPACT: Positions 4-10 (quick wins)
    - STRIKING_DISTANCE: Positions 11-20 (page 1 opportunities)
    - RISING_STAR: Improving trend (needs historical data)
    - FADING_GIANT: Declining trend (needs historical data)
    - OPPORTUNITY: High impressions, low CTR
    - LONG_TAIL: 4+ words (specific intent)
    """
    # Position-based categorization (primary)
    if kw.position <= 3:
        return KeywordCategory.TOP_PERFORMER
    elif kw.position <= 10:
        return KeywordCategory.HIGH_IMPACT
    elif kw.position <= 20:
        return KeywordCategory.STRIKING_DISTANCE

    # CTR-based opportunity detection
    expected_ctr = CTR_BY_POSITION.get(min(round(kw.position), 10), 0.015)
    if kw.impressions > 100 and kw.ctr < expected_ctr * 0.5:
        # High impressions but CTR is less than half expected
        return KeywordCategory.OPPORTUNITY

    # Long-tail detection
    if kw.word_count >= 4:
        return KeywordCategory.LONG_TAIL

    # Historical trend analysis (if data available)
    if historical_data:
        current_pos = kw.position
        historical_pos = historical_data.get('position', kw.position)

        if current_pos < historical_pos - 2:  # Position improved by 2+
            return KeywordCategory.RISING_STAR
        elif current_pos > historical_pos + 3:  # Position dropped by 3+
            return KeywordCategory.FADING_GIANT

    return KeywordCategory.LONG_TAIL  # Default for low-volume keywords


class GSCAnalyzer:
    """
    Google Search Console integration for data-driven content decisions.

    Implements advanced SEO methodology:
    - GSC-driven keyword selection
    - Cannibalization prevention
    - Traffic Maximization Formula
    - Keyword categorization
    """

    def __init__(self):
        self._service = None
        self._credentials = None
        self._site_url = None
        self._initialized = False
        self._property_verified = False
        self._property_lock = asyncio.Lock()
        self._service_lock = asyncio.Lock()
        self._query_inventory = None
        self.transport_timeout_seconds = GSC_TRANSPORT_TIMEOUT_SECONDS
        self._init_gsc()

    def _init_gsc(self):
        """Initialize Google Search Console API client"""
        if service_account is None or build is None:
            logger.warning("Google API client libraries not installed - GSC features disabled")
            return

        creds_path = os.getenv('GOOGLE_SERVICE_ACCOUNT_PATH')

        if not creds_path or creds_path == '/path/to/service-account.json':
            logger.debug("GOOGLE_SERVICE_ACCOUNT_PATH not set - GSC features disabled")
            return

        if not os.path.exists(creds_path):
            logger.warning(f"GSC credentials file not found: {creds_path}")
            return

        try:
            credentials = service_account.Credentials.from_service_account_file(
                creds_path,
                scopes=['https://www.googleapis.com/auth/webmasters.readonly']
            )
            # Kept so every request can be executed on its own transport — the
            # discovery client's shared httplib2.Http is NOT thread-safe, and the
            # pipeline fires GSC calls concurrently (see _execute).
            self._credentials = credentials
            # Discovery-client construction is deferred to the first async
            # request because ``build`` may perform synchronous discovery I/O.
            self._service = None

            # Get site URL from env - support both domain property and URL prefix.
            # No hardcoded default: a wrong property 403s on every query, which is
            # indistinguishable from "no data" downstream.
            self._site_url = os.getenv('GSC_PROPERTY_URL') or os.getenv('GSC_SITE_URL') or None
            if not self._site_url:
                logger.warning("GSC_PROPERTY_URL / GSC_SITE_URL not set - GSC features disabled")
                self._service = None
                return

            self._initialized = True
            logger.debug(f"✅ GSC initialized for {self._site_url}")

        except Exception as e:
            logger.error(f"Failed to initialize GSC: {e}")
            self._service = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        shared_http = getattr(self._service, "_http", None)
        self._close_http_connections(shared_http)

    @staticmethod
    def _close_http_connections(http: Any) -> None:
        """Close httplib2's pooled sockets (the transport has no close())."""
        connections = getattr(http, "connections", None)
        if not isinstance(connections, dict):
            return
        for connection in list(connections.values()):
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        connections.clear()

    @property
    def is_available(self) -> bool:
        """Check if GSC is available and initialized"""
        return bool(
            self._initialized
            and self._site_url
            and (self._service is not None or self._credentials is not None)
        )

    async def _ensure_service(self) -> bool:
        if self._service is not None:
            return True
        if not self._initialized or self._credentials is None or build is None:
            return False
        async with self._service_lock:
            if self._service is not None:
                return True
            try:
                self._service = await asyncio.wait_for(
                    asyncio.to_thread(
                        build,
                        'searchconsole',
                        'v1',
                        credentials=self._credentials,
                    ),
                    timeout=self.transport_timeout_seconds,
                )
            except Exception as exc:
                logger.error("Failed to build GSC service: %s", exc)
                self._initialized = False
                return False
        return True

    @staticmethod
    def _normalize_response(response: Any) -> Any:
        """Copy a GSC response and coerce metric fields at the API boundary."""
        if not isinstance(response, dict):
            return response

        def number(value: Any, integer: bool) -> Any:
            try:
                parsed = float(value or 0)
                return int(parsed) if integer else parsed
            except (TypeError, ValueError, OverflowError):
                return 0 if integer else 0.0

        normalized = dict(response)
        rows = []
        for raw_row in response.get('rows', []) or []:
            if not isinstance(raw_row, dict):
                continue
            row = dict(raw_row)
            row['clicks'] = number(row.get('clicks'), True)
            row['impressions'] = number(row.get('impressions'), True)
            row['ctr'] = number(row.get('ctr'), False)
            row['position'] = number(row.get('position'), False)
            rows.append(row)
        if 'rows' in response:
            normalized['rows'] = rows
        return normalized

    async def _execute(self, build_request, description: str):
        """Execute a GSC API request on its own transport, off the event loop.

        googleapiclient's discovery client wraps a single ``httplib2.Http``,
        which is NOT thread-safe. The pipeline runs GSC calls concurrently
        (``get_content_recommendations`` gathers two; keyword-ideas fans out
        ~10, each gathering two). Sharing one TLS socket across those worker
        threads corrupts it — surfacing as ``SSLError: BAD_RECORD_TYPE``, a
        generic ``HttpError``, or a hard ``double free`` process crash. Giving
        each request a fresh ``AuthorizedHttp`` (own socket, no shared state)
        makes concurrency safe while preserving parallelism.

        ``build_request`` is a thunk that constructs the request, e.g.
        ``lambda: self._service.searchanalytics().query(...)``.
        """
        credentials = getattr(self, '_credentials', None)
        timeout = getattr(self, 'transport_timeout_seconds', GSC_TRANSPORT_TIMEOUT_SECONDS)

        def _run():
            request = build_request()
            if google_auth_httplib2 is not None and credentials is not None:
                try:
                    inner_http = httplib2.Http(timeout=timeout)
                except TypeError:  # lightweight test doubles / older httplib2
                    inner_http = httplib2.Http()
                http = google_auth_httplib2.AuthorizedHttp(credentials, http=inner_http)
                try:
                    return request.execute(http=http)
                finally:
                    self._close_http_connections(inner_http)
            # Stripped env (or no credentials) — fall back to the shared
            # transport (original behavior).
            return request.execute()

        response = await asyncio.wait_for(
            asyncio.to_thread(_run),
            timeout=timeout,
        )
        return self._normalize_response(response)

    @staticmethod
    def _error_status(e: Exception) -> str:
        """Diagnostic status label for a failed GSC call.

        Surfaces the HTTP status (``HttpError(403)``) or SSL reason
        (``SSLError(BAD_RECORD_TYPE)``) instead of a bare class name, so a
        genuine future failure is legible in logs and the TUI row. Non-HTTP/SSL
        exceptions keep the bare class name.
        """
        if HttpError is not None and isinstance(e, HttpError):
            status = getattr(getattr(e, 'resp', None), 'status', None)
            return f"HttpError({status})" if status else "HttpError"
        if isinstance(e, ssl.SSLError):
            reason = getattr(e, 'reason', None)
            return f"SSLError({reason})" if reason else "SSLError"
        return type(e).__name__

    async def _ensure_property(self) -> bool:
        """Verify the configured property is accessible; auto-fall-back once.

        A misconfigured property (e.g. URL-prefix ``https://…`` when only the
        ``sc-domain:`` property is shared with the service account) 403s on
        every query, which the fetch methods would otherwise swallow into
        "no data". Called lazily at the top of each query method; one
        ``sites().list()`` call per instance, result cached.
        """
        if not await self._ensure_service():
            return False
        if self._property_verified:
            return self.is_available
        if not self.is_available:
            return False

        # Serialize verification across concurrent callers. The pipeline fires
        # GSC calls concurrently (a 2-way gather in get_content_recommendations,
        # ~10-way in keyword-ideas). Without this, the first caller sets the
        # flag and awaits sites.list while a sibling races past, sees the flag,
        # and queries the still-unresolved (URL-prefix) property → a spurious
        # 403. The lock makes the first caller fully resolve self._site_url
        # before any sibling proceeds.
        async with self._property_lock:
            if self._property_verified:
                return self.is_available
            # Set the verified flag only AFTER resolution finishes (finally),
            # never before the sites.list await — otherwise a sibling that
            # early-returns on the flag would read the still-stale self._site_url
            # and 403. The lock + late flag makes siblings wait for the resolved
            # property.
            try:
                try:
                    listing = await self._execute(
                        lambda: self._service.sites().list(), "sites.list")
                except Exception as e:
                    # Can't verify (quota/transient) — keep the configured
                    # property and let the query methods surface any real
                    # failure as an error status.
                    logger.warning(f"GSC property verification skipped (sites.list failed): {e}")
                    return True

                accessible = [
                    entry['siteUrl'] for entry in listing.get('siteEntry', [])
                    if entry.get('permissionLevel') != 'siteUnverifiedUser'
                ]
                if self._site_url in accessible:
                    return True

                # Build fallback candidates: the sc-domain: variant of a
                # URL-prefix property and vice versa, then a lone accessible one.
                host = self._site_url
                if host.startswith('sc-domain:'):
                    host = host.replace('sc-domain:', '')
                else:
                    host = host.split('://')[-1].rstrip('/').removeprefix('www.')
                candidates = [f'sc-domain:{host}', f'https://{host}/', f'https://{host}', f'https://www.{host}/', f'https://www.{host}']
                fallback = next((c for c in candidates if c in accessible), None)

                if fallback:
                    logger.warning(
                        f"GSC property '{self._site_url}' not accessible to the service account; "
                        f"using '{fallback}' instead (accessible: {accessible})")
                    self._site_url = fallback
                    return True

                logger.error(
                    f"GSC property '{self._site_url}' not accessible and no usable fallback "
                    f"(accessible: {accessible or 'none'}) - GSC features disabled")
                self._service = None
                self._initialized = False
                return False
            finally:
                self._property_verified = True

    def _get_base_url(self) -> str:
        """Extract base URL from GSC property URL (sc-domain:example.com or https://example.com)."""
        if not self._site_url:
            from .brand_profile import load_brand_profile
            return load_brand_profile().site_url or 'https://example.com'

        # Handle domain property format: sc-domain:example.com -> https://example.com
        if self._site_url.startswith('sc-domain:'):
            domain = self._site_url.replace('sc-domain:', '')
            return f'https://{domain}'

        # Handle URL prefix format: https://example.com -> https://example.com
        if self._site_url.startswith('http'):
            return self._site_url

        # Fallback: prepend https://
        return f'https://{self._site_url}'

    async def check_cannibalization(
        self,
        keyword: str,
        days: int = 90,
        min_impressions: int = 10
    ) -> CannibalizationResult:
        """
        Check if existing Flyweel content already ranks for this keyword.

        CRITICAL: Run this before creating new content to avoid cannibalization.

        Based on SEO best practices:
        - If position 1-10 exists → recommend optimization, not new content
        - If position 11-20 exists → consider consolidation
        - If no ranking → safe to create new content

        Args:
            keyword: Target keyword to check
            days: Number of days to analyze (default 90)
            min_impressions: Minimum impressions threshold

        Returns:
            CannibalizationResult with recommendation
        """
        result = CannibalizationResult(keyword=keyword)

        if not await self._ensure_property():
            logger.warning("GSC not available - skipping cannibalization check")
            result.reason = "GSC not available - proceed with caution"
            return result

        try:
            end_date = datetime.now() - timedelta(days=3)  # GSC has 3-day delay
            start_date = end_date - timedelta(days=days)

            request_body = {
                'startDate': start_date.strftime('%Y-%m-%d'),
                'endDate': end_date.strftime('%Y-%m-%d'),
                'dimensions': ['page', 'query'],
                'dimensionFilterGroups': [{
                    'filters': [{
                        'dimension': 'query',
                        'expression': keyword.lower(),
                        'operator': 'contains'
                    }]
                }],
                'rowLimit': 100
            }

            response = await self._execute(
                lambda: self._service.searchanalytics().query(
                    siteUrl=self._site_url,
                    body=request_body
                ),
                "cannibalization"
            )

            if 'rows' not in response or not response['rows']:
                result.recommendation = ContentRecommendation.CREATE_NEW
                result.reason = f"No existing content ranks for '{keyword}' - safe to create"
                return result

            # Aggregate by page URL
            page_data = {}
            for row in response['rows']:
                page_url = row['keys'][0]
                query = row['keys'][1]

                if page_url not in page_data:
                    page_data[page_url] = {
                        'url': page_url,
                        'clicks': 0,
                        'impressions': 0,
                        'best_position': 100,
                        'queries': []
                    }

                page_data[page_url]['clicks'] += row.get('clicks', 0)
                page_data[page_url]['impressions'] += row.get('impressions', 0)
                page_data[page_url]['best_position'] = min(
                    page_data[page_url]['best_position'],
                    row.get('position', 100)
                )
                page_data[page_url]['queries'].append(query)

            # Filter by minimum impressions and sort by position
            ranking_pages = [
                p for p in page_data.values()
                if p['impressions'] >= min_impressions
            ]
            ranking_pages.sort(key=lambda x: x['best_position'])

            # GSC's `contains` filter is a substring match ("crm" hits every CRM
            # query), so ask Jev whether each page really serves the same search.
            # ponytail: precision only; paraphrased queries the contains-filter
            # misses need an unfiltered page+query pull + Jev, add if
            # cannibalization slips through.
            if ranking_pages and jev.enabled():
                answers = await jev.ask_many(
                    [{"new_article_keyword": keyword, "existing_page": p['url'], "page_queries": p['queries'][:5]}
                     for p in ranking_pages],
                    {"same_search": {"type": "noul", "instructions": (
                        f'A new article targeting "{keyword}" would compete with the existing page '
                        "for the same search, because both answer the same need.")}},
                )
                judged = [(p, jev.noul(a, "same_search")) for p, a in zip(ranking_pages, answers)]
                dropped = [p['url'] for p, prob in judged if prob is not None and prob < 0.5]
                if dropped:
                    logger.info(f"Jev: {len(dropped)} page(s) share only the substring, not the intent: {dropped[:3]}")
                ranking_pages = [p for p, prob in judged if prob is None or prob >= 0.5]

            result.existing_urls = ranking_pages

            if not ranking_pages:
                result.recommendation = ContentRecommendation.CREATE_NEW
                result.reason = f"No pages with significant impressions for '{keyword}'"
                return result

            best_page = ranking_pages[0]
            result.best_existing_url = best_page['url']
            result.best_existing_position = best_page['best_position']

            # Decision logic based on SEO best practices
            if best_page['best_position'] <= 5:
                result.recommendation = ContentRecommendation.ABORT
                result.reason = (
                    f"Strong ranking exists at position {best_page['best_position']:.1f}. "
                    f"Optimize {best_page['url']} instead of creating new content."
                )
            elif best_page['best_position'] <= 10:
                result.recommendation = ContentRecommendation.OPTIMIZE_EXISTING
                result.reason = (
                    f"Page 1 ranking at position {best_page['best_position']:.1f}. "
                    f"Recommend optimizing {best_page['url']} for higher CTR."
                )
            elif len(ranking_pages) > 2:
                result.recommendation = ContentRecommendation.CONSOLIDATE
                result.reason = (
                    f"{len(ranking_pages)} pages compete for '{keyword}'. "
                    f"Consider consolidating into {best_page['url']}."
                )
            else:
                result.recommendation = ContentRecommendation.OPTIMIZE_EXISTING
                result.reason = (
                    f"Striking distance at position {best_page['best_position']:.1f}. "
                    f"Optimize {best_page['url']} for page 1."
                )

            return result

        except Exception as e:
            logger.error(f"Cannibalization check failed: {e}")
            result.reason = f"Check failed: {e} - proceed with caution"
            return result

    async def analyze_page(
        self,
        page_url: str,
        days: int = 90
    ) -> PagePerformance:
        """
        Get comprehensive performance data for an existing page.

        Use this when optimizing existing content to understand:
        - Which keywords drive traffic
        - Which keywords are underperforming (opportunities)
        - Which questions users ask (for FAQ section)

        Args:
            page_url: URL path (e.g., "/blog/best-mcp-servers") or full URL
            days: Number of days to analyze

        Returns:
            PagePerformance with categorized keywords
        """
        result = PagePerformance(url=page_url)

        if not await self._ensure_property():
            logger.warning("GSC not available")
            return result

        try:
            end_date = datetime.now() - timedelta(days=3)
            start_date = end_date - timedelta(days=days)

            # Normalize URL for filter
            base_url = self._get_base_url().rstrip('/')
            if page_url.startswith('/'):
                filter_url = f"{base_url}{page_url}"
            elif not page_url.startswith('http'):
                filter_url = f"{base_url}/{page_url}"
            else:
                filter_url = page_url

            request_body = {
                'startDate': start_date.strftime('%Y-%m-%d'),
                'endDate': end_date.strftime('%Y-%m-%d'),
                'dimensions': ['query'],
                'dimensionFilterGroups': [{
                    'filters': [{
                        'dimension': 'page',
                        'expression': filter_url,
                        'operator': 'equals'
                    }]
                }],
                'rowLimit': 500  # Get more keywords for comprehensive analysis
            }

            response = await self._execute(
                lambda: self._service.searchanalytics().query(
                    siteUrl=self._site_url,
                    body=request_body
                ),
                "page-performance"
            )

            if 'rows' not in response:
                return result

            # Process keywords
            for row in response['rows']:
                query = row['keys'][0]
                kw = KeywordData(
                    query=query,
                    clicks=row.get('clicks', 0),
                    impressions=row.get('impressions', 0),
                    ctr=row.get('ctr', 0),
                    position=row.get('position', 0)
                )
                kw.traffic_potential = calculate_traffic_potential(
                    kw.impressions,
                    kw.position
                )
                kw.category = categorize_keyword(kw)

                result.keywords.append(kw)
                result.total_clicks += kw.clicks
                result.total_impressions += kw.impressions

            if result.keywords:
                # Calculate impression-weighted page averages (guarding zero impressions)
                if result.total_impressions > 0:
                    result.avg_ctr = result.total_clicks / result.total_impressions
                    result.avg_position = (
                        sum(k.position * k.impressions for k in result.keywords)
                        / result.total_impressions
                    )
                else:
                    result.avg_ctr = 0.0
                    result.avg_position = sum(k.position for k in result.keywords) / len(result.keywords)

                # Find primary keyword (highest traffic potential)
                result.primary_keyword = max(
                    result.keywords,
                    key=lambda k: k.traffic_potential
                )

                # Categorize keywords
                result.rising_stars = [
                    k for k in result.keywords
                    if k.category == KeywordCategory.RISING_STAR
                ]
                result.opportunities = [
                    k for k in result.keywords
                    if k.category in [KeywordCategory.OPPORTUNITY, KeywordCategory.STRIKING_DISTANCE]
                ]
                result.questions = [
                    k for k in result.keywords
                    if k.is_question
                ]

            return result

        except Exception as e:
            logger.error(f"Page analysis failed for {page_url}: {e}")
            return result

    async def get_keyword_opportunities(
        self,
        base_keyword: str,
        days: int = 90,
        min_impressions: int = 50
    ) -> Dict[str, List[KeywordData]]:
        """
        Get keyword opportunities related to a base keyword.

        Returns keywords categorized by optimization priority:
        - rising_stars: Improving positions (capitalize on momentum)
        - high_impact: Positions 4-10 (quick wins to page 1 top)
        - striking_distance: Positions 11-20 (page 1 opportunities)
        - opportunities: High impressions, low CTR (title/meta fixes)
        - questions: User questions (FAQ content)

        Args:
            base_keyword: Core keyword to find related opportunities
            days: Number of days to analyze
            min_impressions: Minimum impressions threshold

        Returns:
            Dict with categorized KeywordData lists
        """
        result = {
            'top_performers': [],
            'high_impact': [],
            'striking_distance': [],
            'opportunities': [],
            'rising_stars': [],
            'fading_giants': [],
            'questions': [],
            'all_keywords': [],
            'status': 'no_data',
        }

        if not await self._ensure_property():
            logger.warning("GSC not available")
            return result

        try:
            end_date = datetime.now() - timedelta(days=3)
            start_date = end_date - timedelta(days=days)

            request_body = {
                'startDate': start_date.strftime('%Y-%m-%d'),
                'endDate': end_date.strftime('%Y-%m-%d'),
                'dimensions': ['query'],
                'dimensionFilterGroups': [{
                    'filters': [{
                        'dimension': 'query',
                        'expression': base_keyword.lower(),
                        'operator': 'contains'
                    }]
                }],
                'rowLimit': 500
            }

            response = await self._execute(
                lambda: self._service.searchanalytics().query(
                    siteUrl=self._site_url,
                    body=request_body
                ),
                "keyword-opportunities"
            )

            if 'rows' not in response:
                return result

            def process_rows(floor: int):
                for row in response['rows']:
                    if row.get('impressions', 0) < floor:
                        continue

                    kw = KeywordData(
                        query=row['keys'][0],
                        clicks=row.get('clicks', 0),
                        impressions=row.get('impressions', 0),
                        ctr=row.get('ctr', 0),
                        position=row.get('position', 0)
                    )
                    kw.traffic_potential = calculate_traffic_potential(
                        kw.impressions,
                        kw.position
                    )
                    kw.category = categorize_keyword(kw)

                    result['all_keywords'].append(kw)

                    # Categorize
                    if kw.category == KeywordCategory.TOP_PERFORMER:
                        result['top_performers'].append(kw)
                    elif kw.category == KeywordCategory.HIGH_IMPACT:
                        result['high_impact'].append(kw)
                    elif kw.category == KeywordCategory.STRIKING_DISTANCE:
                        result['striking_distance'].append(kw)
                    elif kw.category == KeywordCategory.OPPORTUNITY:
                        result['opportunities'].append(kw)
                    elif kw.category == KeywordCategory.RISING_STAR:
                        result['rising_stars'].append(kw)
                    elif kw.category == KeywordCategory.FADING_GIANT:
                        result['fading_giants'].append(kw)

                    if kw.is_question:
                        result['questions'].append(kw)

            process_rows(min_impressions)

            # Adaptive floor: on low-volume sites the default 50-impression
            # filter can discard every real row, making modest-but-real data
            # indistinguishable from a brand-new topic. Reprocess the same
            # response (no second API call) with a floor of 1.
            if not result['all_keywords'] and response['rows']:
                process_rows(MIN_IMPRESSIONS_FLOOR)
                result['adaptive_floor'] = True
                logger.info(
                    f"GSC rows for '{base_keyword}' all below {min_impressions} impressions - "
                    f"kept {len(result['all_keywords'])} rows via adaptive floor")

            # Sort each category by traffic potential
            for category in result.values():
                if isinstance(category, list):
                    category.sort(key=lambda k: k.traffic_potential, reverse=True)

            result['status'] = 'ok' if result['all_keywords'] else 'no_data'
            return result

        except Exception as e:
            logger.error(f"Keyword opportunity analysis failed: {e}")
            result['status'] = f'error:{self._error_status(e)}'
            return result

    async def extract_paa_questions(
        self,
        keyword: str,
        days: int = 90,
        limit: int = 10
    ) -> List[str]:
        """
        Extract real People Also Ask questions from GSC query data.

        These are actual questions users searched and saw our content for,
        making them ideal for FAQ sections (better than AI-generated).

        Args:
            keyword: Base keyword to find related questions
            days: Number of days to analyze
            limit: Maximum questions to return

        Returns:
            List of question strings sorted by impressions
        """
        if not self.is_available:
            return []

        try:
            opportunities = await self.get_keyword_opportunities(
                keyword, days, min_impressions=5
            )

            questions = opportunities.get('questions', [])

            # Sort by impressions (most common questions first)
            questions.sort(key=lambda k: k.impressions, reverse=True)

            return [q.query for q in questions[:limit]]

        except Exception as e:
            logger.error(f"PAA extraction failed: {e}")
            return []

    async def inspect_url(
        self,
        url: str,
        language_code: str = "en-US"
    ) -> Dict[str, Any]:
        """
        Inspect Google index status for a deployed URL.

        Uses the Search Console URL Inspection API. The inspected URL must be
        under the configured GSC property. This is a post-deploy observer only;
        it does not request indexing.
        """
        if not await self._ensure_property():
            logger.warning("GSC not available - URL inspection skipped")
            return {
                'available': False,
                'url': url,
                'error': 'GSC not available'
            }

        request_body = {
            'inspectionUrl': url,
            'siteUrl': self._site_url,
            'languageCode': language_code
        }

        try:
            response = await self._execute(
                lambda: self._service.urlInspection().index().inspect(
                    body=request_body
                ),
                "url-inspection"
            )

            result = response.get('inspectionResult', {})
            index_status = (
                result.get('indexStatusResult') or
                result.get('indexStatusInspectionResult') or
                {}
            )

            return {
                'available': True,
                'url': url,
                'site_url': self._site_url,
                'inspection_result_link': result.get('inspectionResultLink'),
                'coverage_state': index_status.get('coverageState'),
                'indexing_state': index_status.get('indexingState'),
                'verdict': index_status.get('verdict'),
                'robots_txt_state': index_status.get('robotsTxtState'),
                'page_fetch_state': index_status.get('pageFetchState'),
                'google_canonical': index_status.get('googleCanonical'),
                'user_canonical': index_status.get('userCanonical'),
                'last_crawl_time': index_status.get('lastCrawlTime') or index_status.get('lastIndexedTime'),
                'raw': response
            }

        except Exception as e:
            logger.error(f"URL inspection failed for {url}: {e}")
            return {
                'available': False,
                'url': url,
                'error': str(e)
            }

    async def get_primary_keyword(
        self,
        target_keyword: str,
        days: int = 90
    ) -> Optional[KeywordData]:
        """
        Get the #1 primary keyword for content optimization.

        Based on GSC-driven content methodology:
        - Find the highest-intent, best-performing keyword
        - Use EXACT keyword in title (not modified)

        Args:
            target_keyword: Base keyword to find primary
            days: Number of days to analyze

        Returns:
            KeywordData for the primary keyword, or None
        """
        opportunities = await self.get_keyword_opportunities(
            target_keyword, days, min_impressions=20
        )

        all_keywords = opportunities.get('all_keywords', [])

        if not all_keywords:
            return None

        # Find keyword with highest traffic potential that's 4+ words (high intent)
        high_intent = [k for k in all_keywords if k.word_count >= 4]

        if high_intent:
            return max(high_intent, key=lambda k: k.traffic_potential)

        # Fallback to highest traffic potential overall
        return max(all_keywords, key=lambda k: k.traffic_potential)

    async def get_content_recommendations(
        self,
        keyword: str,
        days: int = 90
    ) -> Dict[str, Any]:
        """
        Get comprehensive content recommendations based on GSC data.

        Returns actionable insights for content creation/optimization:
        - Primary keyword (use in title exactly)
        - Secondary keywords (use in H2s)
        - Questions (use in FAQ)
        - Cannibalization check result
        - Traffic potential estimate

        Args:
            keyword: Target keyword for content
            days: Number of days to analyze

        Returns:
            Dict with structured recommendations
        """
        # Run analyses in parallel. return_exceptions=True so one failing analysis
        # degrades to its existing no-data fallback instead of discarding both.
        cannibalization, opportunities = await asyncio.gather(
            self.check_cannibalization(keyword, days),
            self.get_keyword_opportunities(keyword, days),
            return_exceptions=True
        )
        if isinstance(cannibalization, Exception):
            logger.warning("Cannibalization check failed, treating as no-ranking: %r", cannibalization)
            cannibalization = CannibalizationResult(keyword=keyword)
        if isinstance(opportunities, Exception):
            logger.warning("Keyword-opportunities lookup failed, treating as empty: %r", opportunities)
            opportunities = {'status': f'error:{self._error_status(opportunities)}'}

        gsc_status = opportunities.get('status', 'no_data')

        all_keywords = opportunities.get('all_keywords', [])
        questions = opportunities.get('questions', [])

        # Find primary keyword
        primary = None
        if all_keywords:
            # Prefer high-intent (4+ words) with good traffic potential
            high_intent = [k for k in all_keywords if k.word_count >= 4]
            if high_intent:
                primary = max(high_intent, key=lambda k: k.traffic_potential)
            else:
                primary = max(all_keywords, key=lambda k: k.traffic_potential)

        # Get secondary keywords for H2s (top 4-5 by position)
        secondaries = sorted(
            [k for k in all_keywords if k != primary],
            key=lambda k: k.position
        )[:5]

        # Format recommendations
        return {
            'target_keyword': keyword,
            'gsc_status': gsc_status,
            'adaptive_floor': bool(opportunities.get('adaptive_floor', False)),
            'cannibalization': {
                'recommendation': cannibalization.recommendation.value,
                'reason': cannibalization.reason,
                'existing_url': cannibalization.best_existing_url,
                'existing_position': cannibalization.best_existing_position
            },
            'primary_keyword': {
                'query': primary.query if primary else keyword,
                'position': primary.position if primary else None,
                'impressions': primary.impressions if primary else 0,
                'traffic_potential': primary.traffic_potential if primary else 0,
                'use_exact': True  # CRITICAL: Use exact keyword in title
            },
            'secondary_keywords': [
                {
                    'query': k.query,
                    'position': k.position,
                    'impressions': k.impressions,
                    'suggested_h2': True
                }
                for k in secondaries
            ],
            'faq_questions': [q.query for q in questions[:10]],
            'opportunities': {
                'striking_distance': len(opportunities.get('striking_distance', [])),
                'high_impact': len(opportunities.get('high_impact', [])),
                'total_keywords': len(all_keywords)
            },
            'meta': {
                'days_analyzed': days,
                'total_impressions': sum(k.impressions for k in all_keywords),
                'total_clicks': sum(k.clicks for k in all_keywords)
            }
        }


    async def get_site_query_inventory(
        self,
        days: int = 180,
        row_limit: int = 1000
    ) -> List[KeywordData]:
        """Fetch the site-wide query corpus (no keyword filter).

        The only unfiltered query pull in the analyzer — the raw material for
        estimating opportunity on topics with no direct GSC data. 180 days
        because young/sparse sites need history for signal. Sorted by
        impressions desc and truncated to the top 500 so the downstream
        embedding batch stays a single call. Cached per instance.
        """
        if self._query_inventory is not None:
            return self._query_inventory

        if not await self._ensure_property():
            return []

        try:
            end_date = datetime.now() - timedelta(days=3)
            start_date = end_date - timedelta(days=days)
            request_body = {
                'startDate': start_date.strftime('%Y-%m-%d'),
                'endDate': end_date.strftime('%Y-%m-%d'),
                'dimensions': ['query'],
                'rowLimit': row_limit
            }
            response = await self._execute(
                lambda: self._service.searchanalytics().query(
                    siteUrl=self._site_url,
                    body=request_body
                ),
                "site-query-inventory"
            )
            inventory = [
                KeywordData(
                    query=row['keys'][0],
                    clicks=row.get('clicks', 0),
                    impressions=row.get('impressions', 0),
                    ctr=row.get('ctr', 0),
                    position=row.get('position', 0)
                )
                for row in response.get('rows', [])
            ]
            inventory.sort(key=lambda k: k.impressions, reverse=True)
            self._query_inventory = inventory[:500]
            logger.debug(f"GSC site query inventory: {len(self._query_inventory)} queries ({days}d)")
            return self._query_inventory

        except Exception as e:
            logger.error(f"Site query inventory fetch failed: {e}")
            return []

    async def estimate_new_topic_opportunity(
        self,
        keyword: str,
        ai_router,
        days: int = 180
    ) -> Dict[str, Any]:
        """Estimate opportunity for a keyword with no direct GSC data.

        Matches the keyword against the site-wide query corpus with embedding
        similarity (``ai_router`` is duck-typed — anything exposing
        ``filter_by_embedding_relevance(query, items, threshold)``), then runs
        the pure neighbor estimator. Fewer than MIN_ESTIMATE_NEIGHBORS related
        queries → zero estimate (greenfield topic, caller decides messaging).
        """
        empty = {
            'traffic_potential': 0.0,
            'neighbor_count': 0,
            'proxy_position': None,
            'estimated': True,
            'top_neighbors': [],
        }
        inventory = await self.get_site_query_inventory(days=days)
        if not inventory:
            return empty

        by_query = {kw.query: kw for kw in inventory}
        try:
            matches = await ai_router.filter_by_embedding_relevance(
                keyword, list(by_query.keys()), threshold=0.60)
        except Exception as e:
            logger.error(f"Embedding relevance for topic estimate failed: {e}")
            return empty

        neighbors = [
            (m['relevance_score'], by_query[m['content']])
            for m in matches if m['content'] in by_query
        ]
        # filter_by_embedding_relevance degrades to relevance 1.0 for ALL items
        # when embeddings fail — a full-corpus "match" is that failure mode,
        # not real similarity; treat it as no signal.
        if len(neighbors) == len(by_query):
            logger.warning("Embedding filter returned the whole corpus - treating as no signal")
            return empty
        if len(neighbors) < MIN_ESTIMATE_NEIGHBORS:
            logger.info(f"Only {len(neighbors)} related GSC queries for '{keyword}' - "
                        "estimate below neighbor floor")
            return {**empty, 'neighbor_count': len(neighbors)}

        estimate = estimate_traffic_potential_from_neighbors(neighbors)
        logger.info(
            f"Estimated opportunity for '{keyword}': tp={estimate['traffic_potential']:.1f} "
            f"from {estimate['neighbor_count']} related queries "
            f"(proxy position {estimate['proxy_position']})")
        return estimate


# Convenience function for CLI usage
async def analyze_keyword(keyword: str, days: int = 90) -> Dict[str, Any]:
    """
    Quick keyword analysis for CLI usage.

    Usage:
        python -c "import asyncio; from core.gsc_analyzer import analyze_keyword; print(asyncio.run(analyze_keyword('mcp servers')))"
    """
    async with GSCAnalyzer() as gsc:
        return await gsc.get_content_recommendations(keyword, days)


# CLI entry point
if __name__ == "__main__":
    import sys
    import json

    async def main():
        if len(sys.argv) < 2:
            print("Usage: python -m core.gsc_analyzer <keyword> [--page <url>] [--days <n>]")
            print("\nExamples:")
            print("  python -m core.gsc_analyzer 'mcp servers'")
            print("  python -m core.gsc_analyzer --page '/blog/best-mcp-servers'")
            print("  python -m core.gsc_analyzer 'ad analytics' --days 30")
            sys.exit(1)

        keyword = None
        page_url = None
        days = 90

        i = 1
        while i < len(sys.argv):
            arg = sys.argv[i]
            if arg == '--page' and i + 1 < len(sys.argv):
                page_url = sys.argv[i + 1]
                i += 2
            elif arg == '--days' and i + 1 < len(sys.argv):
                days = int(sys.argv[i + 1])
                i += 2
            else:
                keyword = arg
                i += 1

        async with GSCAnalyzer() as gsc:
            if not gsc.is_available:
                print("❌ GSC not available. Check credentials and environment variables.")
                sys.exit(1)

            if page_url:
                print(f"📊 Analyzing page: {page_url}")
                result = await gsc.analyze_page(page_url, days)
                print(f"\n✅ Page Performance ({days} days):")
                print(f"   Clicks: {result.total_clicks:,}")
                print(f"   Impressions: {result.total_impressions:,}")
                print(f"   Avg CTR: {result.avg_ctr:.2%}")
                print(f"   Avg Position: {result.avg_position:.1f}")

                if result.primary_keyword:
                    print(f"\n🎯 Primary Keyword: '{result.primary_keyword.query}'")
                    print(f"   Traffic Potential: {result.primary_keyword.traffic_potential:.0f}")

                if result.questions:
                    print(f"\n❓ FAQ Questions ({len(result.questions)}):")
                    for q in result.questions[:5]:
                        print(f"   - {q.query}")

            elif keyword:
                print(f"🔍 Analyzing keyword: '{keyword}'")
                result = await gsc.get_content_recommendations(keyword, days)
                print(json.dumps(result, indent=2, default=str))

    asyncio.run(main())
