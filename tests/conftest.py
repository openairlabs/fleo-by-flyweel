#!/usr/bin/env python3
"""
Test configuration and fixtures
Provides shared utilities for all tests
"""
import pytest
import os


PRIMARY_TEST_MARKERS = ("unit", "integration", "research", "generation")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """Reject tests that would otherwise disappear from marker-based CI gates."""
    unclassified = [
        item.nodeid
        for item in items
        if not any(item.get_closest_marker(marker) for marker in PRIMARY_TEST_MARKERS)
    ]
    if unclassified:
        formatted = "\n".join(f"  - {nodeid}" for nodeid in unclassified)
        pytest.exit(
            "Every test must declare a primary marker "
            f"({', '.join(PRIMARY_TEST_MARKERS)}):\n{formatted}",
            returncode=2,
        )


def has_api_keys() -> bool:
    """Check if generation credentials are available (supports 1-key OpenRouter or per-agent keys)."""
    from core.agent_credentials import can_generate_with_credentials
    ok, _missing = can_generate_with_credentials()
    return ok


def skip_if_no_api_keys(func):
    """Decorator to skip tests if API keys are missing"""
    return pytest.mark.skipif(
        not has_api_keys(),
        reason=(
            "API keys not available (OPENROUTER_API_KEY or per-agent keys required)"
        )
    )(func)


@pytest.fixture
def api_keys_available():
    """Fixture to check API key availability"""
    return has_api_keys()


@pytest.fixture
def require_api_keys():
    """Fixture that skips test if API keys are missing"""
    if not has_api_keys():
        pytest.skip("API keys not available - skipping integration test")


@pytest.fixture(autouse=True)
def _jev_off_by_default(monkeypatch):
    """Keep Jev (core/jev.py) off so unit tests never hit the Decisions API.
    Tests that exercise a Jev path set JEV=1 and stub ``jev.ask``."""
    monkeypatch.setenv("JEV", "0")
