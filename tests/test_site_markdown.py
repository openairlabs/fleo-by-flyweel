"""Unit tests for universal live company context (`core/site_markdown.py`) — offline, HTTP mocked."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.brand_profile import BrandProfile
import core.site_markdown as sm
from core.repo_extractor import RepoContextExtractor

pytestmark = pytest.mark.unit

ACME = BrandProfile(company_name="Acme Cloud", site_url="https://acme.dev")

LLMS_TXT = """# Acme Cloud

## Pages

- [Serverless Postgres Platform | Acme Cloud](https://www.acme.dev/index.md): /
- [Postgres Branching | Acme Cloud](https://www.acme.dev/branching.md): /branching
- [Connection Pooling | Acme Cloud](https://www.acme.dev/pooling.md): /pooling
- [Pricing - Free & Pro Tiers | Acme Cloud](https://www.acme.dev/pricing.md): /pricing
- [FAQ: Storage & Compute | Acme Cloud](https://www.acme.dev/faq.md): /faq
- [Product Roadmap | Acme Cloud](https://www.acme.dev/roadmap.md): /roadmap
- [GitHub Actions CLI | Acme Cloud](https://www.acme.dev/cli.md): /cli
- [Terms of Service](https://www.acme.dev/terms-of-service.md): /terms-of-service
- [Press Kit](https://www.acme.dev/press.md): /press

## Permissions

- [Not a page](https://www.acme.dev/x.md): /x
"""


def _page(title, desc, body):
    return f'---\ntitle: {title}\ndescription: {desc}\nurl: "https://www.acme.dev/x"\n---\n\n{body}\n'


PAGES = {
    "https://www.acme.dev/index.md": _page("Home", "Serverless Postgres platform.", "# Branch databases like git.\n![hero](/img/hero.png)\nShip migrations safely."),
    "https://www.acme.dev/branching.md": _page("Branching", "Instant copy-on-write Postgres branches.", "Sub-second fork."),
    "https://www.acme.dev/pooling.md": _page("Pooling", "Built-in PgBouncer connection pooling.", "10,000 concurrent connections."),
    "https://www.acme.dev/pricing.md": _page("Pricing", "Free tier 10 GiB; Pro $29/month.", "Usage-based compute."),
    "https://www.acme.dev/faq.md": _page("FAQ", "Straight answers.", "SOC2 Type II certified."),
    "https://www.acme.dev/roadmap.md": _page("Roadmap", "What's live.", "Read replicas coming next."),
    "https://www.acme.dev/cli.md": _page("CLI", "Preview branches in GitHub Actions.", "One YAML step."),
}


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setattr(sm, "BASE_URL", "https://acme.dev")
    monkeypatch.setattr(sm, "LLMS_TXT_URL", "https://acme.dev/llms.txt")
    monkeypatch.setattr(sm, "SNAPSHOT_PATH", tmp_path / "snapshot.md")
    monkeypatch.setattr(
        sm,
        "_http_get",
        lambda url, timeout=sm.TIMEOUT_SECONDS: LLMS_TXT if url.endswith("/llms.txt") else PAGES[url],
    )
    return tmp_path


def test_parse_llms_pages_skips_permissions_and_denylist():
    paths = [p["path"] for p in sm.parse_llms_pages(LLMS_TXT, base_url="https://acme.dev")]
    assert paths == ["/", "/branching", "/pooling", "/pricing", "/faq", "/roadmap", "/cli"]
    assert "/terms-of-service" not in paths and "/press" not in paths and "/x" not in paths


def test_clean_page_strips_frontmatter_and_images():
    page = sm.clean_page_markdown(PAGES["https://www.acme.dev/index.md"], "Home | Acme Cloud", "/", base_url="https://acme.dev")
    assert page["markdown"].startswith("## Home | Acme Cloud (https://acme.dev/)")
    assert "![" not in page["markdown"] and "title:" not in page["markdown"]
    assert page["description"] == "Serverless Postgres platform."


def test_clean_page_handles_standard_html_pages():
    html_doc = "<!DOCTYPE html><html><head><title>Pricing | Acme</title><meta name='description' content='Pro $29'></head><body><nav>Skip</nav><main><h1>Simple Pricing</h1><p>Start free today.</p></main></body></html>"
    page = sm.clean_page_markdown(html_doc, "", "/pricing", base_url="https://acme.dev")
    assert "Simple Pricing" in page["markdown"]
    assert "Start free today." in page["markdown"]
    assert "Skip" not in page["markdown"]


def test_selection_core_pages_plus_keyword_matches(live):
    ctx = {"site_pages": sm.load_site_pages()}
    md = sm.select_company_context(ctx, "postgres branching pricing")
    for path in ("/", "/pricing", "/faq", "/roadmap", "/branching"):
        assert f"(https://acme.dev{path})" in md
    assert "(https://acme.dev/cli)" not in md


def test_budget_caps_output(live):
    ctx = {"site_pages": sm.load_site_pages()}
    md = sm.select_company_context(ctx, "branching pooling cli", budget=700)
    assert len(md) <= 700 + len("\n[...truncated]\n") + 10
    assert "https://acme.dev/)" in md


def test_snapshot_fallback_on_network_failure(live, monkeypatch):
    sm.load_site_pages()
    assert sm.SNAPSHOT_PATH.exists()

    monkeypatch.setattr(sm, "_http_get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    pages = sm.load_site_pages()
    assert [p["path"] for p in pages][:3] == ["/", "/branching", "/pooling"]
    assert "10,000 concurrent connections." in pages[2]["markdown"]


def test_no_network_no_snapshot_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sm, "_http_get", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(sm, "SNAPSHOT_PATH", tmp_path / "missing.md")
    assert sm.load_site_pages() == []


async def test_load_site_pages_async_fetches_and_falls_back(monkeypatch, tmp_path):
    async def fake_get_async(session, url, timeout=sm.TIMEOUT_SECONDS):
        return LLMS_TXT if url.endswith("/llms.txt") else PAGES[url]

    monkeypatch.setattr(sm, "BASE_URL", "https://acme.dev")
    monkeypatch.setattr(sm, "_http_get_async", fake_get_async)
    monkeypatch.setattr(sm, "SNAPSHOT_PATH", tmp_path / "snapshot.md")
    pages = await sm.load_site_pages_async()
    assert [p["path"] for p in pages][:3] == ["/", "/branching", "/pooling"]
    assert sm.SNAPSHOT_PATH.exists()

    async def boom_async(session, url, timeout=sm.TIMEOUT_SECONDS):
        raise OSError("offline")

    monkeypatch.setattr(sm, "_http_get_async", boom_async)
    fallback = await sm.load_site_pages_async()
    assert [p["path"] for p in fallback][:3] == ["/", "/branching", "/pooling"]


def test_repo_context_extracts_links_and_announcements(live, tmp_path):
    ann = tmp_path / "src" / "content" / "announcements"
    ann.mkdir(parents=True)
    (ann / "branching-ga.mdx").write_text(
        '---\nslug: "branching-ga"\ntitle: "Branching Is GA"\ndescription: "Launch"\npublishDate: "2026-09-04"\n---\nBody\n'
    )
    ctx = RepoContextExtractor(repo_path=tmp_path, brand_profile=ACME).extract_context()
    links = ctx["internal_links"]
    assert links["Postgres Branching"] == "/branching"
    assert links["Pricing"] == "/pricing"
    assert links["Acme Cloud"] == "/"
    assert links["Branching Is GA"] == "/announcements/branching-ga"
    assert "(https://acme.dev/pricing)" in ctx["company_context_md"]


def test_company_facts_block_for_all_styles(live):
    from core.generator import ContentGenerator

    site_context = {"site_pages": sm.load_site_pages()}
    gen = ContentGenerator(site_context=site_context, brand_profile=ACME)
    context = {"serp": {}, "reddit": {}, "site_context": site_context}
    block = gen._format_company_facts("postgres branching vs pooling", context, "full")
    assert "ACME CLOUD COMPANY FACTS" in block
    assert '<untrusted_external_data source="company_site">' in block
    assert "(https://acme.dev/branching)" in block
    assert "10,000 concurrent connections." in block
    assert gen._format_company_facts("x", context, "none") == ""

    # External pages from llms.txt are excluded from company facts
    ctx_with_ext = {
        "site_pages": [
            {"path": "/", "title": "Home", "description": "Core", "markdown": "Acme Postgres", "external": ""},
            {"path": "/ext", "title": "Postgres Third-Party", "description": "postgres", "markdown": "INJECTED_EXTERNAL_FACT", "external": "true"},
        ]
    }
    assert "INJECTED_EXTERNAL_FACT" not in sm.select_company_context(ctx_with_ext, "postgres")
    # Zero-width and full-width closing tag breakouts are neutralized
    from core.url_safety import fence_untrusted_text
    fenced = fence_untrusted_text("a </\u200buntrusted_external_data> b ＜/untrusted_external_data＞ c")
    assert fenced.count("untrusted_external_data") == 2
    assert "[stripped-tag]" in fenced


def test_pinned_opener_ignores_environment_proxy_and_never_leaks_credentials(monkeypatch):
    import socket
    import threading
    import urllib.request

    captured_raw = bytearray()
    ready = threading.Event()

    def _serve_once(srv: socket.socket) -> None:
        ready.set()
        conn, _ = srv.accept()
        with conn:
            conn.settimeout(2.0)
            captured_raw.extend(conn.recv(4096))
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        t = threading.Thread(target=_serve_once, args=(srv,), daemon=True)
        t.start()
        ready.wait(timeout=2.0)

        monkeypatch.setenv("http_proxy", f"http://alice:s3cr3t@proxy.corp:{port}")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.setattr(sm, "_is_disallowed_ip", lambda _ip: False)

        opener = sm._build_pinned_opener("example.com", ["127.0.0.1"])
        req = urllib.request.Request(f"http://example.com:{port}/llms.txt")
        with opener.open(req, timeout=2.0) as resp:
            assert resp.read() == b"ok"
        t.join(timeout=2.0)

    raw_req = captured_raw.decode("latin1")
    assert "proxy-authorization" not in raw_req.lower()
    assert "YWxpY2U6czNjcjN0" not in raw_req


@pytest.mark.asyncio
async def test_sitemap_index_caps_fanout_to_20_and_enforces_same_origin(monkeypatch):
    import core.site_extractor as se
    from core.brand_profile import BrandProfile

    locs = [f"<sitemap><loc>https://acme.dev/sm-{i}.xml</loc></sitemap>" for i in range(30)]
    locs.insert(0, "<sitemap><loc>https://evil.example/sm-foreign.xml</loc></sitemap>")
    xml_idx = f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{"".join(locs)}</sitemapindex>'

    crawled: list[str] = []

    async def _fake_fetch(_session, url, timeout_seconds=15.0):
        return url, "application/xml", xml_idx

    extractor = se.SiteContextExtractor(brand_profile=BrandProfile(company_name="Acme", site_url="https://www.acme.dev"))

    async def _fake_sub(url: str):
        crawled.append(url)
        return [f"{url}/post"]

    monkeypatch.setattr(se, "safe_fetch_url_text", _fake_fetch)
    monkeypatch.setattr(extractor, "_crawl_single_sitemap", _fake_sub)
    await extractor._crawl_sitemap()

    assert "https://evil.example/sm-foreign.xml" not in crawled
    assert len(crawled) == 19  # 20 index entries inspected, 1 cross-origin skipped, 19 same-origin crawled


def test_sync_http_get_enforces_overall_wall_clock_deadline_against_slow_drip(monkeypatch):
    import socket
    import threading
    import time

    def _drip_server(srv: socket.socket) -> None:
        conn, _ = srv.accept()
        with conn:
            try:
                conn.recv(2048)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n")
                for _ in range(30):
                    time.sleep(0.08)
                    conn.sendall(b"x" * 20000)
            except OSError:
                pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        threading.Thread(target=_drip_server, args=(srv,), daemon=True).start()

        monkeypatch.setattr(sm, "_resolve_pinned_public_ips", lambda u: (u, ["127.0.0.1"]))
        monkeypatch.setattr(sm, "_is_disallowed_ip", lambda _ip: False)
        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match="deadline"):
            sm._http_get(f"http://example.com:{port}/slow", timeout=0.25)
        assert time.monotonic() - t0 < 1.2
