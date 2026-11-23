"""IndexNow URL submission client."""

import json
import logging
import os
import asyncio
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError

try:
    import aiohttp
except ImportError:
    aiohttp = None

from .paths import load_repo_env

load_repo_env()
logger = logging.getLogger(__name__)


class IndexNowClient:
    """Submit deployed URLs to IndexNow-compatible search engines."""

    def __init__(
        self,
        key: Optional[str] = None,
        endpoint: Optional[str] = None,
        key_location: Optional[str] = None,
        session: Optional[Any] = None
    ):
        self.key = key or os.getenv('INDEXNOW_KEY')
        self.endpoint = endpoint or os.getenv('INDEXNOW_ENDPOINT', 'https://api.indexnow.org/indexnow')
        if urlparse(self.endpoint).scheme != 'https':
            logger.warning("IndexNow endpoint must use HTTPS; disabling submissions")
            self.endpoint = None
        self.key_location = key_location or os.getenv('INDEXNOW_KEY_LOCATION')
        self.session = session

    @property
    def is_available(self) -> bool:
        """Check whether IndexNow has enough configuration to submit."""
        return bool(self.key and self.endpoint)

    async def submit_urls(self, urls: List[str]) -> Dict[str, Any]:
        """Submit one or more URLs to IndexNow."""
        urls = [url for url in urls if url]
        if not urls:
            return {'submitted': False, 'error': 'No URLs provided', 'urls': []}

        if not self.is_available:
            return {
                'submitted': False,
                'error': 'INDEXNOW_KEY is not configured',
                'urls': urls
            }

        if len(urls) > 10000:
            return {
                'submitted': False,
                'error': f'IndexNow accepts at most 10,000 URLs per submission (got {len(urls)})',
                'urls': [],
            }

        batch_urls = list(urls)
        host = urlparse(batch_urls[0]).netloc
        payload = {
            'host': host,
            'key': self.key,
            'urlList': batch_urls
        }
        if self.key_location:
            payload['keyLocation'] = self.key_location

        if self.session is None and aiohttp is None:
            return await asyncio.to_thread(self._submit_with_urllib, payload, batch_urls, host)

        owns_session = self.session is None
        session = self.session or aiohttp.ClientSession()

        try:
            post_kwargs: Dict[str, Any] = {
                'data': json.dumps(payload),
                'headers': {'Content-Type': 'application/json; charset=utf-8'},
            }
            if aiohttp is not None and isinstance(session, aiohttp.ClientSession):
                post_kwargs['allow_redirects'] = False
            async with session.post(self.endpoint, **post_kwargs) as response:
                text = await response.text()
                ok = 200 <= response.status < 300
                return {
                    'submitted': ok,
                    'status': response.status,
                    'urls': batch_urls if ok else [],
                    'host': host,
                    'response': text,
                    'endpoint': self.endpoint
                }
        except Exception as e:
            logger.error(f"IndexNow submission failed: {e}")
            return {
                'submitted': False,
                'error': str(e),
                'urls': [],
                'endpoint': self.endpoint
            }
        finally:
            if owns_session:
                await session.close()

    def _submit_with_urllib(self, payload: Dict[str, Any], urls: List[str], host: str) -> Dict[str, Any]:
        """Submit using stdlib urllib when aiohttp is unavailable (without following redirects)."""
        class _NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        data = json.dumps(payload).encode('utf-8')
        request = Request(
            self.endpoint,
            data=data,
            headers={'Content-Type': 'application/json; charset=utf-8'},
            method='POST'
        )
        opener = build_opener(_NoRedirect)

        try:
            # B310 is waived in CI after the constructor's HTTPS-only check and disabled redirects.
            with opener.open(request, timeout=20) as response:
                body = response.read().decode('utf-8', errors='replace')
                status = response.getcode()
                ok = 200 <= status < 300
                return {
                    'submitted': ok,
                    'status': status,
                    'urls': urls if ok else [],
                    'host': host,
                    'response': body,
                    'endpoint': self.endpoint
                }
        except HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            return {
                'submitted': False,
                'status': e.code,
                'urls': [],
                'host': host,
                'response': body,
                'endpoint': self.endpoint
            }
        except URLError as e:
            return {
                'submitted': False,
                'error': str(e),
                'urls': [],
                'endpoint': self.endpoint
            }


async def submit_url(url: str) -> Dict[str, Any]:
    """Convenience wrapper for submitting a single URL."""
    client = IndexNowClient()
    return await client.submit_urls([url])
