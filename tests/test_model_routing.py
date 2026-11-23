import json
from types import SimpleNamespace

import pytest

from core.ai_router import SmartAIRouter


pytestmark = pytest.mark.unit


class FakeCompletions:
    def __init__(self, content="ok", provider=None):
        self.content = content
        self.provider = provider
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            provider=self.provider,
        )


class FailingCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError(
            "Error code: 403 - Provider returned error: "
            "Requests to this API generativelanguage.googleapis.com are blocked"
        )


def _router(completions=None, direct_completions=None):
    router = SmartAIRouter.__new__(SmartAIRouter)
    router.or_client = (
        SimpleNamespace(chat=SimpleNamespace(completions=completions))
        if completions is not None
        else None
    )
    router.direct_gemini_client = SimpleNamespace(
        chat=SimpleNamespace(completions=direct_completions or FakeCompletions()))
    router.gemini_client = router.direct_gemini_client
    router.models_used = {}
    router.model_generation = "openai/gpt-6-sol"
    router.model_draft_fallback = "google/gemini-3.1-pro-preview"
    router.model_editor = "google/gemini-3.8-flash"
    router.model_seo = "google/gemini-3.8-flash"
    router.model_light = "openai/gpt-6-luna"
    router.model_polish = "anthropic/claude-sonnet-5.5"
    router.model_fallback_openai = "openai/gpt-6-sol"
    router.OR_PROVIDER_OPENAI_LOW = {
        "provider": {"only": ["openai"]},
        "reasoning": {"effort": "low"},
    }
    router.OR_PROVIDER_OPENAI_MEDIUM = {
        "provider": {"only": ["openai"]},
        "reasoning": {"effort": "medium"},
    }
    router.OR_PROVIDER_QWEN_FAST = {
        "provider": {"allow_fallbacks": True, "require_parameters": True},
        "reasoning": {"enabled": False},
    }
    router.OR_PROVIDER_QWEN = {
        "provider": {"allow_fallbacks": True, "require_parameters": True},
    }
    router.OR_PROVIDER_POLISH = {
        "provider": {"allow_fallbacks": True, "require_parameters": True},
    }
    return router


def test_editor_model_defaults_to_gemini_38_flash(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    router = SmartAIRouter()

    assert router.model_editor == "google/gemini-3.8-flash"


def test_polish_model_defaults_to_claude_sonnet_5_5(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_TIER", raising=False)

    router = SmartAIRouter()

    assert router.model_polish == "anthropic/claude-sonnet-5.5"


@pytest.mark.asyncio
async def test_polish_create_uses_lowest_reasoning_and_records_provider():
    completions = FakeCompletions("polished", provider="Anthropic")
    router = _router(completions)

    response = await router._polish_create(
        [{"role": "user", "content": "polish"}],
        purpose="section polish",
    )

    assert router._safe_content(response) == "polished"
    call = completions.calls[0]
    assert call["model"] == "anthropic/claude-sonnet-5.5"
    assert call["extra_body"] == {
        "provider": {"allow_fallbacks": True},
        "reasoning": {"effort": "low"},
    }
    assert call["extra_headers"] == {"X-OpenRouter-Metadata": "enabled"}
    assert router.models_used["section polish"] == {
        "model": "anthropic/claude-sonnet-5.5",
        "provider": "Anthropic",
        "downgraded": False,
    }


@pytest.mark.asyncio
async def test_polish_create_keeps_legacy_route_for_qwen_override():
    completions = FakeCompletions("polished", provider="Nebius")
    router = _router(completions)

    await router._polish_create(
        [{"role": "user", "content": "polish"}],
        model="qwen/qwen3-235b-a22b-2507",
    )

    assert completions.calls[0]["extra_body"] == {
        "provider": {"allow_fallbacks": True, "require_parameters": True},
    }


def test_routed_provider_can_be_read_from_openrouter_metadata():
    response = SimpleNamespace(
        openrouter_metadata={
            "endpoints": {
                "available": [
                    {"provider": "Nebius", "selected": True},
                    {"provider": "Alibaba", "selected": False},
                ]
            }
        }
    )

    assert SmartAIRouter._response_provider(response) == "Nebius"


@pytest.mark.parametrize(
    "router_metadata",
    [
        {"endpoints": []},
        {"endpoints": {"available": [None]}},
        {"endpoints": {}, "attempts": {"provider": "Nebius"}},
    ],
)
def test_routed_provider_ignores_malformed_openrouter_metadata(router_metadata):
    response = SimpleNamespace(openrouter_metadata=router_metadata)

    assert SmartAIRouter._response_provider(response) == "unknown-via-openrouter"


@pytest.mark.asyncio
async def test_question_fallback_returns_empty_when_all_templates_filtered(monkeypatch):
    router = SmartAIRouter.__new__(SmartAIRouter)
    router.gemini_client = None
    router.nebius = None
    monkeypatch.setattr(router, "_filter_platform_questions", lambda qs, **_: [])

    questions = await router.generate_questions("topic", count=3)

    assert questions == []


@pytest.mark.asyncio
async def test_short_qwen_tasks_use_multi_provider_route_on_first_request():
    completions = FakeCompletions("ok", provider="Alibaba")
    router = _router(completions)
    router.model_light = "qwen/qwen3.5-397b-a17b"  # legacy LIGHT_MODEL override

    await router._qwen_create(
        [{"role": "user", "content": "classify"}],
        purpose="classification",
    )

    assert len(completions.calls) == 1
    provider = completions.calls[0]["extra_body"]["provider"]
    assert provider == {"allow_fallbacks": True, "require_parameters": True}
    assert "only" not in provider
    assert completions.calls[0]["extra_body"]["reasoning"] == {"enabled": False}


@pytest.mark.asyncio
async def test_content_generation_uses_gpt_6_sol_with_medium_reasoning():
    completions = FakeCompletions()
    router = _router(completions)

    message = await router._call_generation_model(
        [{"role": "user", "content": "draft"}],
        purpose="multipart generation part 1/3",
    )

    assert message.content == "ok"
    assert completions.calls[0]["model"] == "openai/gpt-6-sol"
    assert completions.calls[0]["extra_body"] == {
        "provider": {"only": ["openai"]},
        "reasoning": {"effort": "medium"},
    }


@pytest.mark.asyncio
async def test_generation_falls_back_to_gemini_31_pro_direct():
    direct = FakeCompletions("drafted")
    router = _router(FailingCompletions(), direct)

    message = await router._call_generation_model([{"role": "user", "content": "draft"}])

    assert message.content == "drafted"
    assert direct.calls[0]["model"] == "gemini-3.1-pro-preview"
    assert router.models_used["generation"]["downgraded"] is True


@pytest.mark.asyncio
async def test_segment_editor_uses_gemini_38_flash():
    direct = FakeCompletions(" ".join(["edited"] * 100))
    router = _router(FakeCompletions(), direct)

    await router._edit_section(" ".join(["original"] * 100), 0, 1)

    assert direct.calls[0]["model"] == "gemini-3.8-flash"
    assert direct.calls[0]["reasoning_effort"] == "none"  # thinking truncated sections
    assert "extra_body" not in direct.calls[0]


@pytest.mark.asyncio
async def test_segment_editor_works_with_direct_google_only():
    direct = FakeCompletions(" ".join(["edited"] * 100))
    router = _router(None, direct)

    result = await router._edit_section(" ".join(["original"] * 100), 0, 1)

    assert result == " ".join(["edited"] * 100)
    assert direct.calls[0]["model"] == "gemini-3.8-flash"


@pytest.mark.asyncio
async def test_segment_editor_falls_back_when_google_provider_returns_403():
    edited = " ".join(["edited"] * 100)
    openrouter = FakeCompletions(edited)
    direct = FailingCompletions()
    router = _router(openrouter, direct)

    result = await router._edit_section(" ".join(["original"] * 100), 0, 1)

    assert result == edited
    assert direct.calls[0]["model"] == "gemini-3.8-flash"
    assert openrouter.calls[0]["model"] == "openai/gpt-6-sol"
    assert openrouter.calls[0]["extra_body"] == router.OR_PROVIDER_OPENAI_LOW


@pytest.mark.asyncio
async def test_segment_editor_preserves_original_when_all_providers_fail():
    router = _router(FakeCompletions(" ".join(["unexpected"] * 100)))
    calls = []

    async def unavailable_editor(**kwargs):
        calls.append(kwargs)
        return None

    router._call_gemini_with_fallback = unavailable_editor
    original = " ".join(["original"] * 100)

    result = await router._edit_section(original, 0, 1)

    assert result == original
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_title_and_heading_helpers_route_to_gemini_38_flash():
    router = _router(FakeCompletions())
    calls = []

    async def fake_gemini(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content="result")

    router._call_gemini_with_fallback = fake_gemini
    messages = [{"role": "user", "content": "seo"}]

    await router._call_title_editor(messages)
    await router._call_heading_editor(messages)

    assert [call["model"] for call in calls] == [
        "google/gemini-3.8-flash",
        "google/gemini-3.8-flash",
    ]


@pytest.mark.asyncio
async def test_tldr_routes_to_gemini_38_flash_with_direct_google_only():
    router = _router(None)
    calls = []
    payload = {
        "answer": (
            "Reducing hidden CRM handoff delays requires clear ownership, automatic stage "
            "timestamps, shared service-level targets, and alerts for stalled records. Teams "
            "should map every transfer, remove duplicate approvals, monitor time between "
            "stages, and review exceptions weekly so leads reach the right person before "
            "intent fades."
        ),
        "takeaways": [
            "Assign one owner to every CRM handoff.",
            "Alert teams when records exceed stage targets.",
            "Review stalled transfers every week.",
        ],
    }

    async def fake_gemini(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=json.dumps(payload))

    router._call_gemini_with_fallback = fake_gemini
    content = "---\ntitle: 'CRM Handoffs'\ndescription: 'Fix delays.'\n---\n\n## Map ownership\n\nStart here."

    result = await router.generate_tldr(content, "reduce CRM handoff delays")

    assert result == payload
    assert calls[0]["model"] == "google/gemini-3.8-flash"
