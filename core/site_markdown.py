"""Live company context extractor for ANY website (llms.txt + sitemap.xml + clean HTML->Markdown)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET

from .brand_profile import load_brand_profile
from .paths import site_snapshot_path
from .url_safety import UnsafeUrlError, _is_disallowed_ip, safe_fetch_url_text, validate_public_http_url

logger = logging.getLogger(__name__)

# Overridable module attributes (kept for unit test monkeypatching compatibility)
BASE_URL: str = ""
LLMS_TXT_URL: str = ""
SNAPSHOT_PATH: Optional[Path] = None
TIMEOUT_SECONDS = 10

SKIP_PATHS = {
    "/privacy",
    "/privacy-policy",
    "/terms",
    "/terms-of-service",
    "/cookie-policy",
    "/press",
    "/media-kit",
    "/contact-us",
    "/webmcp",
    "/partners",
    "/login",
    "/signin",
    "/signup",
}
CORE_PATHS = ("/", "/pricing", "/about", "/features", "/product", "/faq", "/roadmap", "/docs")
HIGH_SIGNAL_HINTS = ("pricing", "about", "product", "feature", "platform", "solution", "integration", "faq", "doc", "customer", "security", "roadmap")
DEFAULT_BUDGET_CHARS = 32000

_MD_LINK_WITH_PATH = re.compile(r"^[-*]\s*\[(?P<title>[^\]]+)\]\((?P<url>[^)\s]+)\)(?:\s*:\s*(?P<tail>.*))?$")
_SNAPSHOT_HEADER = re.compile(r"^## (?P<title>.+) \((?P<url>https?://[^)\s]+)\)$", re.M)
_STOP = {"the", "and", "for", "with", "your", "how", "what", "best", "vs", "to", "of", "a", "in", "on", "by"}


def _effective_base_url(site_url: Optional[str] = None) -> str:
    if site_url and site_url.strip():
        raw = site_url.strip().rstrip("/")
        return raw if "://" in raw else f"https://{raw}"
    if BASE_URL:
        return BASE_URL.rstrip("/")
    profile = load_brand_profile()
    return (profile.site_url or "").rstrip("/")


def _effective_snapshot_path() -> Path:
    return SNAPSHOT_PATH if SNAPSHOT_PATH is not None else site_snapshot_path()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _resolve_pinned_public_ips(url: str) -> tuple[str, list[str]]:
    """Validate ``url`` and return ``(validated_url, pinned_public_ips)`` with zero non-public records."""
    import ipaddress
    import socket

    validated = validate_public_http_url(url, resolve_dns=False)
    parsed = urlsplit(validated)
    hostname = (parsed.hostname or "").strip().lower()
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_disallowed_ip(addr):
            raise UnsafeUrlError(f"Disallowed non-public IP {addr}")
        return validated, [str(addr)]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve hostname {hostname!r}: {exc}") from exc
    if not infos:
        raise UnsafeUrlError(f"No DNS records for hostname {hostname!r}")

    pinned: list[str] = []
    for info in infos:
        ip_str = str(info[4][0]).split("%", 1)[0]
        addr = ipaddress.ip_address(ip_str)
        if _is_disallowed_ip(addr):
            raise UnsafeUrlError(f"Hostname {hostname!r} resolves to non-public IP {addr}")
        if ip_str not in pinned:
            pinned.append(ip_str)
    return validated, pinned


def _build_pinned_opener(hostname: str, pinned_ips: list[str]) -> urllib.request.OpenerDirector:
    import http.client
    import ipaddress
    import socket
    import ssl

    def _connect_pinned_socket(port: int, timeout: Any, source_address: Any) -> socket.socket:
        last_err: Optional[OSError] = None
        for ip_str in pinned_ips:
            sock: Optional[socket.socket] = None
            try:
                sock = socket.create_connection((ip_str, port), timeout=timeout, source_address=source_address)
                peer_ip = str(sock.getpeername()[0]).split("%", 1)[0]
                if _is_disallowed_ip(ipaddress.ip_address(peer_ip)):
                    sock.close()
                    raise UnsafeUrlError(f"Connected peer IP {peer_ip} is non-public")
                return sock
            except UnsafeUrlError:
                if sock is not None:
                    sock.close()
                raise
            except OSError as exc:
                if sock is not None:
                    sock.close()
                last_err = exc
        raise last_err or UnsafeUrlError(f"Could not connect to pinned public IP for {hostname!r}")

    class _PinnedHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            if getattr(self, "_tunnel_host", None):
                raise UnsafeUrlError("HTTP proxy tunneling is forbidden")
            self.sock = _connect_pinned_socket(self.port, self.timeout, self.source_address)

    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            if getattr(self, "_tunnel_host", None):
                raise UnsafeUrlError("HTTPS proxy tunneling is forbidden")
            raw_sock = _connect_pinned_socket(self.port, self.timeout, self.source_address)
            try:
                ctx = getattr(self, "_context", None) or ssl.create_default_context()
                self.sock = ctx.wrap_socket(raw_sock, server_hostname=hostname)
            except Exception:
                raw_sock.close()
                raise

    class _PinnedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: urllib.request.Request):
            return self.do_open(_PinnedHTTPConnection, req)

    class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req: urllib.request.Request):
            return self.do_open(_PinnedHTTPSConnection, req)

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _PinnedHTTPHandler,
        _PinnedHTTPSHandler,
        _NoRedirectHandler,
    )


def _http_get(url: str, timeout: int = TIMEOUT_SECONDS, max_redirects: int = 5) -> str:
    current = url
    deadline = time.monotonic() + float(timeout)
    max_bytes = 2 * 1024 * 1024
    for _hop in range(max_redirects + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"HTTP fetch exceeded total deadline ({timeout}s) for {url}")
        current, pinned_ips = _resolve_pinned_public_ips(current)
        hop_host = (urlsplit(current).hostname or "").strip()
        opener = _build_pinned_opener(hop_host, pinned_ips)
        req = urllib.request.Request(
            current,
            headers={"User-Agent": "FleoContentEngine/0.1 (+https://github.com/openairlabs/fleo-by-flyweel)"},
        )
        try:
            with opener.open(req, timeout=max(0.2, min(float(timeout), remaining))) as resp:
                chunks: list[bytes] = []
                total = 0
                while total < max_bytes:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"HTTP fetch exceeded total deadline ({timeout}s) for {url}")
                    chunk = resp.read(min(16384, max_bytes - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                loc = exc.headers.get("Location") if exc.headers else None
                exc.close()
                if not loc:
                    raise UnsafeUrlError(f"Redirect HTTP {exc.code} missing Location header") from exc
                current = urljoin(current, loc)
                continue
            raise
    raise UnsafeUrlError(f"Exceeded maximum redirects ({max_redirects}) for {url}")


async def _http_get_async(session: aiohttp.ClientSession, url: str, timeout: int = TIMEOUT_SECONDS) -> str:
    _final, _ct, text = await safe_fetch_url_text(session, url, timeout_seconds=float(timeout))
    return text


def _path_from_url_or_tail(url: str, tail: Optional[str], base_url: str) -> str:
    if tail and tail.strip().startswith("/") and " " not in tail.strip():
        return tail.strip()
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if path.endswith("/index.md"):
        path = path[: -len("/index.md")] or "/"
    elif path.endswith(".md"):
        path = path[:-3] or "/"
    return path if path.startswith("/") else f"/{path}"


def parse_llms_pages(llms_txt: str, base_url: str = "") -> List[Dict[str, str]]:
    """Parse any standard `llms.txt` markdown link list (supports `.md` AND normal HTML URLs)."""
    pages: List[Dict[str, str]] = []
    seen_paths: set[str] = set()
    skip_section = False

    for raw_line in (llms_txt or "").splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            skip_section = any(bad in heading for bad in ("permission", "legal", "policy", "disallowed", "license"))
            continue
        if skip_section or not line.startswith(("-", "*")):
            continue
        match = _MD_LINK_WITH_PATH.match(line)
        if not match:
            continue
        title = match.group("title").strip()
        raw_url = match.group("url").strip()
        full_url = urljoin((base_url or "https://example.com").rstrip("/") + "/", raw_url)
        tail = match.group("tail")
        path = _path_from_url_or_tail(full_url, tail, base_url)
        if path in SKIP_PATHS or any(path.startswith(f"{sp}/") for sp in SKIP_PATHS if sp != "/"):
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)
        description = ""
        if tail and not tail.strip().startswith("/"):
            description = tail.strip()
        pages.append({"title": title, "md_url": full_url, "path": path, "description": description})
    return pages[:24]


def html_to_clean_markdown(html: str) -> tuple[str, str, str]:
    """Convert raw HTML to compact, clean Markdown `(title, meta_description, markdown_body)`."""
    soup = BeautifulSoup(html or "", "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
    desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    desc = str(desc_tag.get("content") or "").strip() if desc_tag else ""

    for tag in soup.find_all(["script", "style", "noscript", "svg", "iframe", "nav", "footer"]):
        tag.decompose()

    root = soup.find("main") or soup.find("article") or soup.body or soup
    chunks: List[str] = []
    for el in root.find_all(["h1", "h2", "h3", "p", "li", "tr"]):
        txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not txt or len(txt) < 3:
            continue
        name = el.name.lower()
        if name == "h1":
            chunks.append(f"### {txt}")
        elif name == "h2":
            chunks.append(f"### {txt}")
        elif name == "h3":
            chunks.append(f"#### {txt}")
        elif name == "li":
            chunks.append(f"- {txt}")
        elif name == "tr":
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in el.find_all(["th", "td"])]
            if any(cells):
                chunks.append("| " + " | ".join(cells) + " |")
        else:
            chunks.append(txt)

    deduped: List[str] = []
    for line in chunks:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return title, desc, "\n\n".join(deduped)[:12000]


def clean_page_markdown(raw: str, title: str, path: str, base_url: str = "", fetched_url: str = "") -> Dict[str, str]:
    """Normalize either Markdown (with frontmatter) or HTML into `## Title (url)` markdown."""
    effective_base = _effective_base_url(base_url) or "https://example.com"
    base_host = (urlsplit(effective_base).hostname or "").lower().removeprefix("www.")
    fetched_parsed = urlsplit(fetched_url) if fetched_url else None
    fetched_host = (fetched_parsed.hostname or "").lower().removeprefix("www.") if fetched_parsed else ""
    is_external = bool(fetched_host and base_host and fetched_host != base_host)
    if is_external and fetched_parsed is not None:
        ext_origin = f"{fetched_parsed.scheme or 'https'}://{fetched_parsed.netloc}"
        url = f"{ext_origin}{path}"
    else:
        url = f"{effective_base}{path}"

    stripped = (raw or "").lstrip()
    if stripped.startswith(("<!DOCTYPE", "<html", "<HTML", "<head", "<body")) or ("<body" in stripped[:2000].lower()):
        html_title, html_desc, md_body = html_to_clean_markdown(raw)
        final_title = title or html_title or path
        entry = {
            "path": path,
            "title": final_title,
            "url": url,
            "description": html_desc,
            "markdown": f"## {final_title} ({url})\n\n{md_body}\n",
        }
        if is_external:
            entry["external"] = "true"
        return entry

    description = ""
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            front, body = parts[1], parts[2]
            desc = re.search(r"^description:\s*(.+)$", front, re.M)
            description = desc.group(1).strip().strip('"').strip("'") if desc else ""
    lines = [ln.rstrip() for ln in body.splitlines() if "![" not in ln]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    entry = {
        "path": path,
        "title": title,
        "url": url,
        "description": description,
        "markdown": f"## {title} ({url})\n\n{text}\n",
    }
    if is_external:
        entry["external"] = "true"
    return entry


def _discover_sitemap_urls_from_xml(xml_text: str, base_url: str) -> List[Dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    base_host = (urlsplit(base_url).hostname or "").lower().removeprefix("www.")
    urls: List[str] = []
    for elem in root.iter():
        if elem.tag.endswith("loc") and elem.text:
            u = elem.text.strip()
            host = (urlsplit(u).hostname or "").lower().removeprefix("www.")
            if not base_host or host == base_host:
                if not u.endswith(".xml"):
                    urls.append(u)

    def _rank(u: str) -> tuple[int, int]:
        p = urlsplit(u).path.rstrip("/") or "/"
        if p == "/":
            return (0, 0)
        if p in CORE_PATHS:
            return (1, len(p))
        if any(h in p.lower() for h in HIGH_SIGNAL_HINTS) and "/blog/" not in p.lower():
            return (2, len(p))
        if "/blog" in p.lower():
            return (4, len(p))
        return (3, len(p))

    chosen: List[Dict[str, str]] = []
    seen: set[str] = set()
    for u in sorted(set(urls), key=_rank):
        p = urlsplit(u).path or "/"
        if p in SKIP_PATHS or p in seen:
            continue
        seen.add(p)
        label = "Home" if p == "/" else p.strip("/").split("/")[-1].replace("-", " ").title()
        chosen.append({"title": label, "md_url": u, "path": p, "description": ""})
        if len(chosen) >= 10:
            break
    return chosen


def _discover_nav_links_from_home(home_html: str, base_url: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(home_html or "", "html.parser")
    base_host = (urlsplit(base_url).hostname or "").lower().removeprefix("www.")
    items: List[Dict[str, str]] = [{"title": "Home", "md_url": base_url, "path": "/", "description": ""}]
    seen = {"/"}
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url + "/", str(a["href"]).strip())
        parsed = urlsplit(full)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/") or "/"
        if host != base_host or path in seen or path in SKIP_PATHS:
            continue
        if any(h in path.lower() for h in HIGH_SIGNAL_HINTS):
            seen.add(path)
            label = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()[:60] or path.strip("/").title()
            items.append({"title": label, "md_url": full, "path": path, "description": ""})
        if len(items) >= 10:
            break
    return items


def _fetch_live(site_url: Optional[str] = None) -> List[Dict[str, str]]:
    if site_url is not None and not site_url.strip():
        return []
    base = _effective_base_url(site_url)
    llms_url = f"{base}/llms.txt" if site_url else (f"{base}/llms.txt" if base else LLMS_TXT_URL)
    if not llms_url:
        return []

    listing: List[Dict[str, str]] = []
    try:
        listing = parse_llms_pages(_http_get(llms_url), base_url=base)
    except Exception:
        listing = []

    if listing and not base:
        first_parsed = urlsplit(listing[0]["md_url"])
        if first_parsed.scheme and first_parsed.netloc:
            base = f"{first_parsed.scheme}://{first_parsed.netloc}".replace("://www.", "://")

    if not listing and base:
        for sm_suffix in ("/sitemap.xml", "/sitemap-0.xml"):
            try:
                listing = _discover_sitemap_urls_from_xml(_http_get(f"{base}{sm_suffix}"), base)
                if listing:
                    break
            except Exception:
                continue

    if not listing and base:
        home_html = _http_get(base)
        listing = _discover_nav_links_from_home(home_html, base)

    if not listing:
        raise ValueError("Could not discover any pages for site")

    def _safe_get_page(url: str) -> Optional[str]:
        try:
            return _http_get(url)
        except Exception as exc:
            logger.debug("Skipping failed page fetch %s: %s", url, exc)
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        raws = list(pool.map(lambda p: _safe_get_page(p["md_url"]), listing))
    results = [
        clean_page_markdown(raw, p["title"], p["path"], base_url=base, fetched_url=p["md_url"])
        for p, raw in zip(listing, raws)
        if raw
    ]
    if not results:
        raise ValueError("All discovered page fetches failed")
    return results


async def _fetch_live_async(
    session: Optional[aiohttp.ClientSession] = None,
    site_url: Optional[str] = None,
) -> List[Dict[str, str]]:
    if site_url is not None and not site_url.strip():
        return []
    base = _effective_base_url(site_url)
    llms_url = f"{base}/llms.txt" if site_url else (f"{base}/llms.txt" if base else LLMS_TXT_URL)
    if not llms_url:
        return []

    own = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS))
    try:
        listing: List[Dict[str, str]] = []
        try:
            listing = parse_llms_pages(await _http_get_async(session, llms_url), base_url=base)
        except Exception:
            listing = []

        if listing and not base:
            first_parsed = urlsplit(listing[0]["md_url"])
            if first_parsed.scheme and first_parsed.netloc:
                base = f"{first_parsed.scheme}://{first_parsed.netloc}".replace("://www.", "://")

        if not listing and base:
            for sm_suffix in ("/sitemap.xml", "/sitemap-0.xml"):
                try:
                    xml_text = await _http_get_async(session, f"{base}{sm_suffix}")
                    listing = _discover_sitemap_urls_from_xml(xml_text, base)
                    if listing:
                        break
                except Exception:
                    continue

        if not listing and base:
            home_html = await _http_get_async(session, base)
            listing = _discover_nav_links_from_home(home_html, base)

        if not listing:
            raise ValueError(f"Could not discover pages from {base}")

        sem = asyncio.Semaphore(6)

        async def fetch_one(page: Dict[str, str]) -> Optional[str]:
            async with sem:
                try:
                    return await _http_get_async(session, page["md_url"])
                except Exception as exc:
                    logger.debug("Skipping page %s: %s", page["md_url"], exc)
                    return None

        raws = await asyncio.gather(*(fetch_one(p) for p in listing))
        results = [
            clean_page_markdown(raw, p["title"], p["path"], base_url=base, fetched_url=p["md_url"])
            for p, raw in zip(listing, raws)
            if raw
        ]
        if not results:
            raise ValueError(f"No readable pages returned from {base}")
        return results
    finally:
        if own:
            await session.close()


def _write_snapshot(pages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not pages:
        return pages
    from .paths import atomic_write_text, workspace_root

    snap = _effective_snapshot_path()
    try:
        ws = workspace_root() if SNAPSHOT_PATH is None else None
        atomic_write_text(snap, "\n".join(p["markdown"] for p in pages), root=ws)
    except (OSError, ValueError) as exc:
        logger.warning("Could not write site snapshot: %s", exc)
    return pages


def _load_snapshot(err: Optional[Exception] = None, site_url: Optional[str] = None) -> List[Dict[str, str]]:
    if site_url is not None and not site_url.strip():
        return []
    snap = _effective_snapshot_path()
    if err is not None:
        logger.warning("Live site fetch failed (%s); checking snapshot %s", err, snap)
    if snap.is_symlink() or not snap.exists() or not snap.is_file():
        return []
    expected_base = _effective_base_url(site_url) or LLMS_TXT_URL
    expected_host = (urlsplit(expected_base).hostname or "").lower().removeprefix("www.") if expected_base else ""
    if not expected_host:
        return []
    try:
        text = snap.read_text(encoding="utf-8")
        heads = list(_SNAPSHOT_HEADER.finditer(text))
        out: List[Dict[str, str]] = []
        snap_hosts: set[str] = set()
        for i, h in enumerate(heads):
            full_url = h["url"]
            h_host = (urlsplit(full_url).hostname or "").lower().removeprefix("www.")
            if h_host:
                snap_hosts.add(h_host)
            path = urlsplit(full_url).path or "/"
            entry: Dict[str, str] = {
                "path": path,
                "title": h["title"],
                "url": full_url,
                "description": "",
                "markdown": text[h.start() : (heads[i + 1].start() if i + 1 < len(heads) else len(text))].strip() + "\n",
            }
            if h_host and expected_host and h_host != expected_host:
                entry["external"] = "true"
            out.append(entry)
        if expected_host and snap_hosts and expected_host not in snap_hosts:
            logger.warning("Ignoring snapshot %s (belongs to %s, requested %s)", snap, sorted(snap_hosts), expected_host)
            return []
        return out
    except Exception as exc:
        logger.warning("No usable site snapshot (%s); company context empty", exc)
        return []


def load_site_pages(site_url: Optional[str] = None) -> List[Dict[str, str]]:
    """Live pages from site (llms.txt -> sitemap -> nav); snapshot fallback; [] if unconfigured."""
    if (site_url is not None and not site_url.strip()) or (not _effective_base_url(site_url) and not LLMS_TXT_URL):
        return []
    try:
        pages = _fetch_live(site_url=site_url)
        return _write_snapshot(pages) if pages else _load_snapshot(site_url=site_url)
    except Exception as exc:
        return _load_snapshot(exc, site_url=site_url)


async def load_site_pages_async(
    session: Optional[aiohttp.ClientSession] = None,
    site_url: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Async live pages from site (llms.txt -> sitemap -> nav); snapshot fallback; [] if unconfigured."""
    if (site_url is not None and not site_url.strip()) or (not _effective_base_url(site_url) and not LLMS_TXT_URL):
        return []
    try:
        pages = await _fetch_live_async(session=session, site_url=site_url)
        return await asyncio.to_thread(_write_snapshot, pages) if pages else await asyncio.to_thread(_load_snapshot, None, site_url)
    except Exception as exc:
        return await asyncio.to_thread(_load_snapshot, exc, site_url)


def _norm_page_path(path_str: str) -> str:
    return (path_str or "").rstrip("/") or "/"


def page_summaries(pages: List[Dict[str, str]], core: bool, brand_name: str = "") -> List[str]:
    suffix = f" | {brand_name}" if brand_name else ""
    out: List[str] = []
    for p in pages:
        if (_norm_page_path(p["path"]) in CORE_PATHS) != core:
            continue
        title = p["title"].replace(suffix, "") if suffix else re.split(r"\s+\|\s+", p["title"])[0]
        out.append(f"{title}: {p.get('description', '')}".rstrip(": "))
    return out


def page_links(pages: List[Dict[str, str]], brand_name: str = "") -> Dict[str, str]:
    effective_brand = brand_name or load_brand_profile().company_name or "Home"
    links = {effective_brand: "/"}
    base_host = ""
    for p in pages:
        if not p.get("external") and p.get("url"):
            base_host = (urlsplit(p["url"]).hostname or "").lower().removeprefix("www.")
            if base_host:
                break
    for p in pages:
        if p.get("external") == "true":
            continue
        p_host = (urlsplit(p.get("url") or "").hostname or "").lower().removeprefix("www.")
        if base_host and p_host and p_host != base_host:
            continue
        if p["path"] != "/":
            anchor = re.split(r" [-|] |: ", p["title"])[0].strip()
            if anchor:
                links[anchor] = p["path"]
    return links


def _terms(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2 and t not in _STOP}


def select_company_context(
    site_context: Optional[Dict[str, Any]],
    keyword: str = "",
    budget: int = DEFAULT_BUDGET_CHARS,
) -> str:
    """Core pages first, then pages ranked by keyword overlap, capped at `budget` chars."""
    raw_pages = (site_context or {}).get("site_pages") or []
    pages = [p for p in raw_pages if p.get("external") != "true"]
    if not pages:
        return ""
    core = [p for path in CORE_PATHS for p in pages if _norm_page_path(p["path"]) == path]
    seen_paths = {_norm_page_path(p["path"]) for p in core}
    kw = _terms(keyword)
    scored = [
        (2 * len(kw & _terms(f"{p['path']} {p['title']}")) + len(kw & _terms(p.get("description", ""))), p)
        for p in pages
        if _norm_page_path(p["path"]) not in seen_paths
    ]
    matched = [p for score, p in sorted(scored, key=lambda s: -s[0]) if score > 0]
    # If no keyword was passed (e.g. generic context preload), include remaining top pages up to budget
    if not keyword.strip():
        matched = [p for _score, p in scored[:4]]
    out: List[str] = []
    used = 0
    for page in core + matched:
        md = page["markdown"]
        room = budget - used
        if room < 500:
            break
        if len(md) > room:
            md = md[:room].rsplit("\n", 1)[0] + "\n[...truncated]\n"
        out.append(md)
        used += len(md)
    return "\n".join(out)
