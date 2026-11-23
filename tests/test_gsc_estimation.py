"""Unit tests for GSC opportunity estimation.

Covers:
- estimate_traffic_potential_from_neighbors (pure math: weighting, clamps,
  discount, filtering)
- get_keyword_opportunities adaptive impressions floor + status field
- get_content_recommendations status propagation (gsc_status / adaptive_floor)
- _ensure_property fallback matrix
- estimate_new_topic_opportunity with a fake embedding router

All offline: the GSC service is a plain stub object and GSCAnalyzer instances
are built via __new__ to bypass _init_gsc (no env / credentials dependence).
"""
import asyncio

import pytest

from core.gsc_analyzer import (
    CannibalizationResult,
    ESTIMATE_DISCOUNT,
    ESTIMATE_POSITION_CLAMP,
    GSCAnalyzer,
    KeywordData,
    MIN_ESTIMATE_NEIGHBORS,
    calculate_traffic_potential,
    estimate_traffic_potential_from_neighbors,
    select_inferred_secondaries,
)


pytestmark = pytest.mark.unit


def _kw(query: str, impressions: int, position: float, clicks: int = 0, ctr: float = 0.0) -> KeywordData:
    return KeywordData(query=query, clicks=clicks, impressions=impressions, ctr=ctr, position=position)


# ---------------------------------------------------------------------------
# Pure estimator math
# ---------------------------------------------------------------------------

class TestEstimateFromNeighbors:

    def test_empty_neighbors_returns_zero_estimate(self):
        result = estimate_traffic_potential_from_neighbors([])
        assert result == {
            'traffic_potential': 0.0,
            'neighbor_count': 0,
            'proxy_position': None,
            'estimated': True,
            'top_neighbors': [],
        }

    def test_nonpositive_similarity_and_impressions_are_dropped(self):
        neighbors = [
            (0.0, _kw("zero sim", 500, 8.0)),
            (-0.4, _kw("negative sim", 500, 8.0)),
            (0.9, _kw("zero impressions", 0, 8.0)),
        ]
        result = estimate_traffic_potential_from_neighbors(neighbors)
        assert result['traffic_potential'] == 0.0
        assert result['neighbor_count'] == 0
        assert result['proxy_position'] is None
        assert result['top_neighbors'] == []

    def test_weighted_estimate_math(self):
        neighbors = [
            (0.9, _kw("lead attribution tools", 1000, 8.0)),
            (0.5, _kw("crm attribution", 500, 12.0)),
        ]
        result = estimate_traffic_potential_from_neighbors(neighbors)

        # est_impressions = 0.9*1000 + 0.5*500 = 1150
        # proxy position = (8*1000 + 12*500) / 1500 = 9.333 (inside clamp)
        expected_proxy = (8.0 * 1000 + 12.0 * 500) / 1500
        expected_tp = calculate_traffic_potential(1150, expected_proxy) * ESTIMATE_DISCOUNT

        assert result['neighbor_count'] == 2
        assert result['proxy_position'] == round(expected_proxy, 1)
        assert result['traffic_potential'] == pytest.approx(expected_tp)
        # Position 9 CTR is 3%: 1150 * 0.03 * 0.5
        assert result['traffic_potential'] == pytest.approx(17.25)
        assert result['estimated'] is True

    def test_estimated_impressions_are_int_truncated(self):
        # 0.5 * 101 = 50.5 -> int() -> 50 impressions at position 7 (4% CTR)
        result = estimate_traffic_potential_from_neighbors([(0.5, _kw("kw", 101, 7.0))])
        assert result['traffic_potential'] == pytest.approx(50 * 0.04 * ESTIMATE_DISCOUNT)

    def test_position_clamped_up_to_floor(self):
        # Raw weighted position 2 — a new article never assumes top-5.
        neighbors = [
            (1.0, _kw("brand term one", 100, 1.0)),
            (1.0, _kw("brand term two", 100, 3.0)),
        ]
        result = estimate_traffic_potential_from_neighbors(neighbors)
        lo, _hi = ESTIMATE_POSITION_CLAMP
        assert result['proxy_position'] == lo
        # est 200 impressions at clamped position 5 (7% CTR), discounted
        assert result['traffic_potential'] == pytest.approx(200 * 0.07 * ESTIMATE_DISCOUNT)

    def test_position_clamped_down_to_ceiling(self):
        # Raw weighted position 45 — never assume worse than page 2.
        neighbors = [
            (1.0, _kw("long tail one", 100, 40.0)),
            (1.0, _kw("long tail two", 100, 50.0)),
        ]
        result = estimate_traffic_potential_from_neighbors(neighbors)
        _lo, hi = ESTIMATE_POSITION_CLAMP
        assert result['proxy_position'] == hi
        # est 200 impressions at clamped position 20 (1.5% CTR), discounted
        assert result['traffic_potential'] == pytest.approx(200 * 0.015 * ESTIMATE_DISCOUNT)

    def test_discount_applied_to_raw_potential(self):
        result = estimate_traffic_potential_from_neighbors([(1.0, _kw("kw", 1000, 10.0))])
        undiscounted = calculate_traffic_potential(1000, 10.0)
        assert result['traffic_potential'] == pytest.approx(undiscounted * ESTIMATE_DISCOUNT)
        assert result['traffic_potential'] < undiscounted

    def test_only_top_20_neighbors_by_similarity_used(self):
        neighbors = [
            (0.99 - i * 0.01, _kw(f"query {i}", 100, 10.0))
            for i in range(25)
        ]
        result = estimate_traffic_potential_from_neighbors(neighbors)
        assert result['neighbor_count'] == 20
        assert len(result['top_neighbors']) == 12
        # Highest-similarity neighbor leads the top_neighbors sample.
        assert result['top_neighbors'][0]['query'] == "query 0"
        assert result['top_neighbors'][0]['similarity'] == 0.99


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _StubSearchService:
    """Plain object mimicking the googleapiclient chain the analyzer calls.

    searchanalytics().query(siteUrl=..., body=...).execute() -> canned rows
    sites().list().execute() -> canned property listing
    """

    def __init__(self, query_response=None, query_error=None, listing=None, listing_error=None):
        self._query_response = query_response if query_response is not None else {}
        self._query_error = query_error
        self._listing = listing if listing is not None else {}
        self._listing_error = listing_error
        self.query_bodies = []
        self.query_site_urls = []
        self.list_calls = 0
        self._mode = None

    # searchanalytics chain
    def searchanalytics(self):
        self._mode = 'query'
        return self

    def query(self, siteUrl=None, body=None):
        self.query_site_urls.append(siteUrl)
        self.query_bodies.append(body)
        return self

    # sites chain
    def sites(self):
        self._mode = 'list'
        return self

    def list(self):
        self.list_calls += 1
        return self

    def execute(self):
        if self._mode == 'list':
            if self._listing_error is not None:
                raise self._listing_error
            return self._listing
        if self._query_error is not None:
            raise self._query_error
        return self._query_response


def _make_analyzer(service, site_url='sc-domain:flyweel.co', property_verified=True,
                   credentials=None):
    analyzer = GSCAnalyzer.__new__(GSCAnalyzer)
    analyzer._service = service
    analyzer._credentials = credentials
    analyzer._site_url = site_url
    analyzer._initialized = service is not None
    analyzer._property_verified = property_verified
    analyzer._property_lock = asyncio.Lock()
    analyzer._query_inventory = None
    return analyzer


class _FakeRouter:
    def __init__(self, matches=None, error=None):
        self.matches = matches or []
        self.error = error
        self.calls = []

    async def filter_by_embedding_relevance(self, query, items, threshold=None):
        self.calls.append({'query': query, 'items': list(items), 'threshold': threshold})
        if self.error is not None:
            raise self.error
        return self.matches


# ---------------------------------------------------------------------------
# get_keyword_opportunities: adaptive floor + status
# ---------------------------------------------------------------------------

class TestKeywordOpportunitiesAdaptiveFloor:

    async def test_adaptive_floor_rescues_low_impression_rows(self):
        rows = [
            {'keys': ['crm attribution setup'], 'clicks': 2, 'impressions': 12, 'ctr': 0.16, 'position': 6.0},
            {'keys': ['crm attribution guide'], 'clicks': 0, 'impressions': 4, 'ctr': 0.0, 'position': 14.0},
        ]
        analyzer = _make_analyzer(_StubSearchService(query_response={'rows': rows}))

        result = await analyzer.get_keyword_opportunities('crm attribution', min_impressions=50)

        assert result['adaptive_floor'] is True
        assert result['status'] == 'ok'
        assert len(result['all_keywords']) == 2
        assert {kw.query for kw in result['all_keywords']} == {
            'crm attribution setup', 'crm attribution guide'}

    async def test_no_adaptive_floor_when_default_filter_keeps_rows(self):
        rows = [
            {'keys': ['crm attribution setup'], 'clicks': 9, 'impressions': 300, 'ctr': 0.03, 'position': 6.0},
            {'keys': ['crm attribution guide'], 'clicks': 0, 'impressions': 4, 'ctr': 0.0, 'position': 14.0},
        ]
        analyzer = _make_analyzer(_StubSearchService(query_response={'rows': rows}))

        result = await analyzer.get_keyword_opportunities('crm attribution', min_impressions=50)

        assert not result.get('adaptive_floor')
        assert result['status'] == 'ok'
        # The 4-impression row stays filtered out by the default floor.
        assert [kw.query for kw in result['all_keywords']] == ['crm attribution setup']

    async def test_no_rows_yields_no_data_status(self):
        analyzer = _make_analyzer(_StubSearchService(query_response={}))

        result = await analyzer.get_keyword_opportunities('crm attribution')

        assert result['status'] == 'no_data'
        assert result['all_keywords'] == []
        assert not result.get('adaptive_floor')

    async def test_query_error_yields_typed_error_status(self):
        analyzer = _make_analyzer(_StubSearchService(query_error=RuntimeError('quota exceeded')))

        result = await analyzer.get_keyword_opportunities('crm attribution')

        assert result['status'] == 'error:RuntimeError'
        assert result['all_keywords'] == []

    async def test_unavailable_service_yields_no_data_status(self):
        analyzer = _make_analyzer(None)
        analyzer._initialized = False

        result = await analyzer.get_keyword_opportunities('crm attribution')

        assert result['status'] == 'no_data'
        assert result['all_keywords'] == []


# ---------------------------------------------------------------------------
# get_content_recommendations: status propagation
# ---------------------------------------------------------------------------

class TestContentRecommendationsStatusPropagation:

    async def test_gsc_status_and_adaptive_floor_are_propagated(self):
        analyzer = _make_analyzer(_StubSearchService())

        async def fake_cannibalization(keyword, days=90):
            return CannibalizationResult(keyword=keyword)

        kw = _kw('crm attribution setup', 12, 6.0, clicks=2)
        kw.traffic_potential = calculate_traffic_potential(kw.impressions, kw.position)

        async def fake_opportunities(keyword, days=90):
            return {
                'status': 'ok',
                'adaptive_floor': True,
                'all_keywords': [kw],
                'questions': [],
                'striking_distance': [],
                'high_impact': [],
            }

        analyzer.check_cannibalization = fake_cannibalization
        analyzer.get_keyword_opportunities = fake_opportunities

        rec = await analyzer.get_content_recommendations('crm attribution')

        assert rec['gsc_status'] == 'ok'
        assert rec['adaptive_floor'] is True
        assert rec['primary_keyword']['query'] == 'crm attribution setup'

    async def test_error_status_propagates_and_floor_defaults_false(self):
        analyzer = _make_analyzer(_StubSearchService())

        async def fake_cannibalization(keyword, days=90):
            return CannibalizationResult(keyword=keyword)

        async def fake_opportunities(keyword, days=90):
            return {'status': 'error:HttpError', 'all_keywords': [], 'questions': []}

        analyzer.check_cannibalization = fake_cannibalization
        analyzer.get_keyword_opportunities = fake_opportunities

        rec = await analyzer.get_content_recommendations('crm attribution')

        assert rec['gsc_status'] == 'error:HttpError'
        assert rec['adaptive_floor'] is False

    async def test_opportunities_exception_becomes_typed_error_status(self):
        analyzer = _make_analyzer(_StubSearchService())

        async def fake_cannibalization(keyword, days=90):
            return CannibalizationResult(keyword=keyword)

        async def exploding_opportunities(keyword, days=90):
            raise ValueError('bad response')

        analyzer.check_cannibalization = fake_cannibalization
        analyzer.get_keyword_opportunities = exploding_opportunities

        rec = await analyzer.get_content_recommendations('crm attribution')

        assert rec['gsc_status'] == 'error:ValueError'
        assert rec['adaptive_floor'] is False
        # Cannibalization default survives the partial failure.
        assert rec['cannibalization']['recommendation'] == 'create_new'


# ---------------------------------------------------------------------------
# _ensure_property fallback matrix
# ---------------------------------------------------------------------------

def _listing(*entries):
    return {'siteEntry': [dict(entry) for entry in entries]}


class TestEnsureProperty:

    async def test_configured_property_accessible_is_kept(self):
        service = _StubSearchService(listing=_listing(
            {'siteUrl': 'https://flyweel.co/', 'permissionLevel': 'siteOwner'},
        ))
        analyzer = _make_analyzer(service, site_url='https://flyweel.co/', property_verified=False)

        assert await analyzer._ensure_property() is True
        assert analyzer._site_url == 'https://flyweel.co/'
        assert analyzer._service is service

    async def test_url_prefix_falls_back_to_sc_domain_variant(self):
        service = _StubSearchService(listing=_listing(
            {'siteUrl': 'sc-domain:flyweel.co', 'permissionLevel': 'siteFullUser'},
        ))
        analyzer = _make_analyzer(service, site_url='https://flyweel.co/', property_verified=False)

        assert await analyzer._ensure_property() is True
        assert analyzer._site_url == 'sc-domain:flyweel.co'

    async def test_concurrent_callers_resolve_once_without_stale_read(self):
        # Regression: the pipeline calls _ensure_property from concurrent
        # gather siblings. Before the lock, the first caller set the verified
        # flag then awaited sites.list while a sibling raced past and queried
        # the still-unresolved URL-prefix property -> spurious 403.
        service = _StubSearchService(listing=_listing(
            {'siteUrl': 'sc-domain:flyweel.co', 'permissionLevel': 'siteFullUser'},
        ))
        analyzer = _make_analyzer(service, site_url='https://flyweel.co/', property_verified=False)

        results = await asyncio.gather(
            analyzer._ensure_property(),
            analyzer._ensure_property(),
            analyzer._ensure_property(),
        )

        assert all(results)
        # Every caller sees the resolved property, never the stale URL-prefix.
        assert analyzer._site_url == 'sc-domain:flyweel.co'
        # The lock collapses the fan-out to a single sites.list.
        assert service.list_calls == 1

    async def test_sc_domain_falls_back_to_url_prefix_variant(self):
        service = _StubSearchService(listing=_listing(
            {'siteUrl': 'https://flyweel.co/', 'permissionLevel': 'siteOwner'},
        ))
        analyzer = _make_analyzer(service, site_url='sc-domain:flyweel.co', property_verified=False)

        assert await analyzer._ensure_property() is True
        assert analyzer._site_url == 'https://flyweel.co/'

    async def test_www_prefix_is_stripped_when_deriving_candidates(self):
        service = _StubSearchService(listing=_listing(
            {'siteUrl': 'sc-domain:flyweel.co', 'permissionLevel': 'siteOwner'},
        ))
        analyzer = _make_analyzer(
            service, site_url='https://www.flyweel.co', property_verified=False)

        assert await analyzer._ensure_property() is True
        assert analyzer._site_url == 'sc-domain:flyweel.co'

    async def test_lone_accessible_property_used_when_no_variant_matches(self):
        service = _StubSearchService(listing=_listing(
            {'siteUrl': 'sc-domain:other.io', 'permissionLevel': 'siteOwner'},
        ))
        analyzer = _make_analyzer(service, site_url='https://flyweel.co/', property_verified=False)

        assert await analyzer._ensure_property() is False
        assert analyzer._service is None
        assert analyzer.is_available is False

    async def test_multiple_unrelated_properties_disable_service(self):
        service = _StubSearchService(listing=_listing(
            {'siteUrl': 'sc-domain:other.io', 'permissionLevel': 'siteOwner'},
            {'siteUrl': 'sc-domain:another.io', 'permissionLevel': 'siteOwner'},
        ))
        analyzer = _make_analyzer(service, site_url='https://flyweel.co/', property_verified=False)

        assert await analyzer._ensure_property() is False
        assert analyzer._service is None
        assert analyzer.is_available is False

    async def test_unverified_entries_do_not_count_as_accessible(self):
        service = _StubSearchService(listing=_listing(
            {'siteUrl': 'https://flyweel.co/', 'permissionLevel': 'siteUnverifiedUser'},
        ))
        analyzer = _make_analyzer(service, site_url='https://flyweel.co/', property_verified=False)

        assert await analyzer._ensure_property() is False
        assert analyzer._service is None

    async def test_listing_failure_keeps_configured_property(self):
        service = _StubSearchService(listing_error=RuntimeError('quota'))
        analyzer = _make_analyzer(service, site_url='https://flyweel.co/', property_verified=False)

        assert await analyzer._ensure_property() is True
        assert analyzer._site_url == 'https://flyweel.co/'
        assert analyzer._service is service

    async def test_verification_result_is_cached(self):
        service = _StubSearchService(listing=_listing(
            {'siteUrl': 'https://flyweel.co/', 'permissionLevel': 'siteOwner'},
        ))
        analyzer = _make_analyzer(service, site_url='https://flyweel.co/', property_verified=False)

        assert await analyzer._ensure_property() is True
        assert await analyzer._ensure_property() is True
        assert service.list_calls == 1

    async def test_unavailable_analyzer_short_circuits(self):
        analyzer = _make_analyzer(None, property_verified=False)
        analyzer._initialized = False

        assert await analyzer._ensure_property() is False


# ---------------------------------------------------------------------------
# estimate_new_topic_opportunity
# ---------------------------------------------------------------------------

class TestEstimateNewTopicOpportunity:

    def _inventory(self):
        return [
            _kw('lead attribution tools', 1000, 8.0),
            _kw('crm attribution', 500, 12.0),
            _kw('marketing attribution models', 300, 9.0),
            _kw('unrelated bakery recipes', 900, 4.0),
        ]

    async def test_happy_path_matches_pure_estimator(self):
        analyzer = _make_analyzer(_StubSearchService())
        inventory = self._inventory()
        analyzer._query_inventory = inventory
        matches = [
            {'content': 'lead attribution tools', 'relevance_score': 0.91},
            {'content': 'crm attribution', 'relevance_score': 0.72},
            {'content': 'marketing attribution models', 'relevance_score': 0.65},
        ]
        router = _FakeRouter(matches=matches)

        result = await analyzer.estimate_new_topic_opportunity('ad attribution', router)

        expected = estimate_traffic_potential_from_neighbors([
            (0.91, inventory[0]), (0.72, inventory[1]), (0.65, inventory[2]),
        ])
        assert result == expected
        assert result['estimated'] is True
        assert result['neighbor_count'] == 3
        assert result['traffic_potential'] > 0

        assert router.calls[0]['query'] == 'ad attribution'
        assert router.calls[0]['threshold'] == 0.60
        assert set(router.calls[0]['items']) == {kw.query for kw in inventory}

    async def test_matches_not_in_inventory_are_ignored(self):
        analyzer = _make_analyzer(_StubSearchService())
        analyzer._query_inventory = self._inventory()
        matches = [
            {'content': 'lead attribution tools', 'relevance_score': 0.91},
            {'content': 'crm attribution', 'relevance_score': 0.72},
            {'content': 'hallucinated query', 'relevance_score': 0.99},
        ]
        router = _FakeRouter(matches=matches)

        result = await analyzer.estimate_new_topic_opportunity('ad attribution', router)

        # Only 2 real neighbors survive — below the 3-neighbor floor.
        assert result['neighbor_count'] == 2
        assert result['traffic_potential'] == 0.0

    async def test_below_neighbor_floor_returns_zero_with_count(self):
        analyzer = _make_analyzer(_StubSearchService())
        analyzer._query_inventory = self._inventory()
        router = _FakeRouter(matches=[
            {'content': 'crm attribution', 'relevance_score': 0.8},
        ])

        result = await analyzer.estimate_new_topic_opportunity('ad attribution', router)

        assert result['neighbor_count'] == 1
        assert result['neighbor_count'] < MIN_ESTIMATE_NEIGHBORS
        assert result['traffic_potential'] == 0.0
        assert result['proxy_position'] is None
        assert result['top_neighbors'] == []

    async def test_whole_corpus_match_treated_as_no_signal(self):
        # filter_by_embedding_relevance degrades to relevance 1.0 for ALL items
        # on embedding failure; a full match over a big corpus is that failure.
        analyzer = _make_analyzer(_StubSearchService())
        inventory = [_kw(f'query {i}', 100 + i, 10.0) for i in range(31)]
        analyzer._query_inventory = inventory
        router = _FakeRouter(matches=[
            {'content': kw.query, 'relevance_score': 1.0} for kw in inventory
        ])

        result = await analyzer.estimate_new_topic_opportunity('anything', router)

        assert result['traffic_potential'] == 0.0
        assert result['neighbor_count'] == 0

    async def test_empty_inventory_returns_zero_without_calling_router(self):
        analyzer = _make_analyzer(_StubSearchService())
        analyzer._query_inventory = []
        router = _FakeRouter()

        result = await analyzer.estimate_new_topic_opportunity('ad attribution', router)

        assert result['traffic_potential'] == 0.0
        assert result['neighbor_count'] == 0
        assert router.calls == []

    async def test_embedding_failure_returns_zero_estimate(self):
        analyzer = _make_analyzer(_StubSearchService())
        analyzer._query_inventory = self._inventory()
        router = _FakeRouter(error=RuntimeError('embeddings down'))

        result = await analyzer.estimate_new_topic_opportunity('ad attribution', router)

        assert result['traffic_potential'] == 0.0
        assert result['neighbor_count'] == 0


# ---------------------------------------------------------------------------
# _error_status: legible diagnostic labels (not a fallback — just readable errors)
# ---------------------------------------------------------------------------

class TestErrorStatus:

    def test_http_error_includes_status_code(self):
        from googleapiclient.errors import HttpError

        class _Resp:
            status = 403
            reason = 'Forbidden'

        err = HttpError(_Resp(), b'{}')
        assert GSCAnalyzer._error_status(err) == 'HttpError(403)'

    def test_ssl_error_includes_reason(self):
        import ssl
        err = ssl.SSLError('bad record')
        err.reason = 'BAD_RECORD_TYPE'
        assert GSCAnalyzer._error_status(err) == 'SSLError(BAD_RECORD_TYPE)'

    def test_ssl_error_without_reason_stays_labelled_ssl(self):
        import ssl
        assert GSCAnalyzer._error_status(ssl.SSLError('opaque')).startswith('SSLError')

    def test_other_exceptions_keep_bare_class_name(self):
        # Guards the existing 'error:RuntimeError' / 'error:ValueError' contract.
        assert GSCAnalyzer._error_status(ValueError('x')) == 'ValueError'
        assert GSCAnalyzer._error_status(RuntimeError('x')) == 'RuntimeError'


# ---------------------------------------------------------------------------
# _execute: fresh transport per request — regression guard for the
# non-thread-safe shared-httplib2 crash (SSLError BAD_RECORD_TYPE / double free).
# ---------------------------------------------------------------------------

class _FakeHttplib2:
    def __init__(self):
        self.http_count = 0

    def Http(self):
        self.http_count += 1
        return ('http', self.http_count)


class _FakeAuthorizedHttpFactory:
    def __init__(self):
        self.made = []

    def AuthorizedHttp(self, credentials, http=None):
        obj = {'credentials': credentials, 'inner_http': http, 'id': len(self.made)}
        self.made.append(obj)
        return obj


class _RecordingRequest:
    def __init__(self):
        self.executed_with = 'unset'

    def execute(self, http=None):
        self.executed_with = http
        return http


class TestExecuteFreshTransport:

    async def test_execute_mints_distinct_transport_per_concurrent_call(self, monkeypatch):
        import core.gsc_analyzer as gsc_mod
        fake_httplib2 = _FakeHttplib2()
        fake_factory = _FakeAuthorizedHttpFactory()
        monkeypatch.setattr(gsc_mod, 'httplib2', fake_httplib2)
        monkeypatch.setattr(gsc_mod, 'google_auth_httplib2', fake_factory)

        creds = object()
        analyzer = _make_analyzer(_StubSearchService(), credentials=creds)

        reqs = [_RecordingRequest(), _RecordingRequest()]
        results = await asyncio.gather(
            analyzer._execute(lambda: reqs[0], 'a'),
            analyzer._execute(lambda: reqs[1], 'b'),
        )

        # Each concurrent call built its own AuthorizedHttp over its own Http().
        assert len(fake_factory.made) == 2
        assert fake_httplib2.http_count == 2
        # The two calls never share a transport — the whole point of the fix.
        assert results[0] is not results[1]
        assert reqs[0].executed_with is not reqs[1].executed_with
        # Built from the analyzer's service-account credentials.
        assert all(m['credentials'] is creds for m in fake_factory.made)

    async def test_execute_falls_back_to_shared_transport_when_lib_absent(self, monkeypatch):
        import core.gsc_analyzer as gsc_mod
        monkeypatch.setattr(gsc_mod, 'google_auth_httplib2', None)

        analyzer = _make_analyzer(_StubSearchService(), credentials=object())
        req = _RecordingRequest()
        result = await analyzer._execute(lambda: req, 'x')

        # Degrades to request.execute() with no injected http (original behavior).
        assert result is None
        assert req.executed_with is None


# ---------------------------------------------------------------------------
# Inferred secondary keywords (Keyword Map backfill)
# ---------------------------------------------------------------------------

def _neighbor(query: str, similarity: float, impressions: int, position: float = 9.0) -> dict:
    return {'query': query, 'similarity': similarity, 'impressions': impressions, 'position': position}


class TestSelectInferredSecondaries:

    def test_curated_picks_keep_neighbor_metadata_and_order(self):
        neighbors = [
            _neighbor("marketing attribution tools", 0.8, 900, 7.2),
            _neighbor("cac payback period", 0.7, 400, 11.0),
            _neighbor("roas benchmarks saas", 0.9, 200, 14.5),
        ]
        chosen = ["roas benchmarks saas", "cac payback period", "not a neighbor"]

        result = select_inferred_secondaries("roas lies fastest when sales cycles pass 30 days",
                                             neighbors, chosen_queries=chosen)

        assert [kw['query'] for kw in result] == ["roas benchmarks saas", "cac payback period"]
        assert all(kw['inferred'] is True for kw in result)
        assert all(kw['suggested_h2'] is True for kw in result)
        assert result[0]['impressions'] == 200
        assert result[0]['position'] == 14.5

    def test_heuristic_fallback_dedupes_keyword_and_near_duplicates(self):
        keyword = "marketing attribution tools"
        neighbors = [
            _neighbor("marketing attribution tools", 0.99, 1000),   # dup of keyword
            _neighbor("attribution tools marketing", 0.95, 900),    # reordered dup
            _neighbor("cac payback by channel", 0.7, 500),
            _neighbor("multi touch attribution model", 0.8, 300),
        ]

        result = select_inferred_secondaries(keyword, neighbors)

        queries = [kw['query'] for kw in result]
        assert "marketing attribution tools" not in queries
        assert "attribution tools marketing" not in queries
        assert "cac payback by channel" in queries
        assert "multi touch attribution model" in queries

    def test_heuristic_ranks_by_similarity_times_impressions(self):
        neighbors = [
            _neighbor("low demand niche query", 0.9, 10),
            _neighbor("high demand adjacent query", 0.7, 2000),
        ]
        result = select_inferred_secondaries("some fresh topic", neighbors)
        assert result[0]['query'] == "high demand adjacent query"

    def test_caps_at_max_keywords_and_handles_empty(self):
        neighbors = [_neighbor(f"distinct topic {chr(97 + i)} {i}", 0.8, 100 + i) for i in range(9)]
        assert len(select_inferred_secondaries("kw", neighbors, max_keywords=5)) == 5
        assert select_inferred_secondaries("kw", []) == []
        assert select_inferred_secondaries("kw", [{'similarity': 0.9}]) == []


class TestCurateSecondaryKeywords:
    """AI curation pass — parsing/grounding only, model call stubbed."""

    def _router(self, monkeypatch, response):
        from types import SimpleNamespace
        from core.ai_router import SmartAIRouter
        router = SmartAIRouter()
        router.or_client = SimpleNamespace()

        async def fake_nebius(messages, **kwargs):
            return response

        monkeypatch.setattr(router, "_nebius_with_fallback", fake_nebius)
        return router

    async def test_returns_only_valid_picks_in_order(self, monkeypatch):
        router = self._router(monkeypatch, '{"selected": ["b query", "hallucinated", "a query", "b query"]}')
        neighbors = [_neighbor("a query", 0.9, 100), _neighbor("b query", 0.8, 200)]
        picks = await router.curate_secondary_keywords("topic", neighbors)
        assert picks == ["b query", "a query"]

    async def test_tolerates_fenced_json_and_bad_payloads(self, monkeypatch):
        neighbors = [_neighbor("a query", 0.9, 100)]
        fenced = self._router(monkeypatch, '```json\n{"selected": ["a query"]}\n```')
        assert await fenced.curate_secondary_keywords("topic", neighbors) == ["a query"]

        for bad in (None, "", "not json", '{"other": []}', '{"selected": "a query"}'):
            router = self._router(monkeypatch, bad)
            assert await router.curate_secondary_keywords("topic", neighbors) == []

    async def test_no_client_or_no_neighbors_short_circuits(self, monkeypatch):
        router = self._router(monkeypatch, '{"selected": ["a query"]}')
        assert await router.curate_secondary_keywords("topic", []) == []
        router.or_client = None
        assert await router.curate_secondary_keywords("topic", [_neighbor("a query", 0.9, 100)]) == []
