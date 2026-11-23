"""Manual post-deploy index health checks."""

import argparse
import asyncio
import json
import logging
from typing import Any, Dict

from .gsc_analyzer import GSCAnalyzer
from .indexnow import IndexNowClient

logger = logging.getLogger(__name__)


async def inspect(url: str, use_google: bool = True) -> Dict[str, Any]:
    """Inspect URL index state with enabled providers."""
    result: Dict[str, Any] = {'url': url}

    if use_google:
        async with GSCAnalyzer() as gsc:
            result['google'] = await gsc.inspect_url(url)

    return result


async def submit(url: str, use_indexnow: bool = True) -> Dict[str, Any]:
    """Submit URL to indexing notification providers."""
    result: Dict[str, Any] = {'url': url}

    if use_indexnow:
        client = IndexNowClient()
        result['indexnow'] = await client.submit_urls([url])

    return result


async def observe(url: str, use_google: bool = True, use_indexnow: bool = True) -> Dict[str, Any]:
    """Submit then inspect a deployed URL."""
    result: Dict[str, Any] = {'url': url}

    if use_indexnow:
        client = IndexNowClient()
        result['indexnow'] = await client.submit_urls([url])

    if use_google:
        async with GSCAnalyzer() as gsc:
            result['google'] = await gsc.inspect_url(url)

    return result


def _print_result(result: Dict[str, Any], as_json: bool):
    if as_json:
        print(json.dumps(result, indent=2, default=str))
        return

    print(f"URL: {result.get('url')}")
    if 'indexnow' in result:
        indexnow = result['indexnow']
        status = indexnow.get('status') or indexnow.get('error')
        print(f"IndexNow: {status}")
    if 'google' in result:
        google = result['google']
        if google.get('available'):
            print(f"Google coverage: {google.get('coverage_state') or google.get('indexing_state') or 'unknown'}")
            print(f"Google verdict: {google.get('verdict') or 'unknown'}")
            print(f"Last crawl: {google.get('last_crawl_time') or 'unknown'}")
        else:
            print(f"Google: {google.get('error', 'unavailable')}")


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="Inspect and submit deployed URLs for index health.")
    subparsers = parser.add_subparsers(dest='command', required=True)

    inspect_parser = subparsers.add_parser('inspect', help='Inspect URL index status')
    inspect_parser.add_argument('--url', required=True)
    inspect_parser.add_argument('--google', action='store_true')
    inspect_parser.add_argument('--json', action='store_true')

    submit_parser = subparsers.add_parser('submit', help='Submit URL to index notification providers')
    submit_parser.add_argument('--url', required=True)
    submit_parser.add_argument('--indexnow', action='store_true')
    submit_parser.add_argument('--json', action='store_true')

    observe_parser = subparsers.add_parser('observe', help='Submit and inspect URL')
    observe_parser.add_argument('--url', required=True)
    observe_parser.add_argument('--google', action='store_true')
    observe_parser.add_argument('--indexnow', action='store_true')
    observe_parser.add_argument('--json', action='store_true')

    return parser


async def main_async(args: argparse.Namespace):
    """Run selected command."""
    if args.command == 'inspect':
        use_google = True if not args.google else args.google
        result = await inspect(args.url, use_google=use_google)
    elif args.command == 'submit':
        use_indexnow = True if not args.indexnow else args.indexnow
        result = await submit(args.url, use_indexnow=use_indexnow)
    else:
        both_unset = not args.google and not args.indexnow
        result = await observe(
            args.url,
            use_google=(True if both_unset else args.google),
            use_indexnow=(True if both_unset else args.indexnow),
        )

    _print_result(result, as_json=args.json)


def main():
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO)
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
