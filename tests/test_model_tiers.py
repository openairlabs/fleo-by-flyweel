"""MODEL_TIER selection, vendor provider routing, the Jev light-model trial,
and the per-run cost accumulator. All offline."""
import json
from types import SimpleNamespace

import pytest

from core.agent_credentials import AGENT_ROLES_META
from core.ai_router import JEV_ROUTER, SmartAIRouter
from core.quality_judge import judge_quality


pytestmark = pytest.mark.unit


class FakeCompletions:
    def __init__(self, contents, usage=None):
        self.contents = list(contents)
        self.usage = usage
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents.pop(0) if self.contents else "ok"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=self.usage,
        )


@pytest.fixture
def clean_env(monkeypatch):
    role_vars = [
        var
        for meta in AGENT_ROLES_META
        for var in (meta["keyEnv"], meta["modelEnv"], f"FLEO_{meta['role']}_BASE_URL")
    ]
    for var in ("MODEL_TIER", "LIGHT_MODEL", "GENERATION_MODEL",
                "OPENROUTER_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY",
                *role_vars):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _router_with(completions):
    router = SmartAIRouter()
    router.or_client = router.nebius = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    router._dedicated_role_clients = {}
    return router


def test_balanced_tier_is_default(clean_env):
    r = SmartAIRouter()
    assert r.model_tier == "balanced"
    assert r.model_generation == "openai/gpt-6-sol"
    assert r.model_polish == "anthropic/claude-sonnet-5.5"
    assert r.model_creative == "openai/gpt-6-luna"
    assert r.model_precision == r.model_fallback_openai == "openai/gpt-6-sol"
    assert r.model_light == "openai/gpt-6-luna"
    assert r.model_editor == r.model_seo == "google/gemini-3.8-flash"
    assert r.model_draft_fallback == "google/gemini-3.1-pro-preview"


def test_max_tier(clean_env):
    clean_env.setenv("MODEL_TIER", "MAX")
    r = SmartAIRouter()
    assert r.model_tier == "max"
    assert r.model_generation == "anthropic/claude-opus-5.5"
    assert r.model_polish == "anthropic/claude-opus-5.5"
    assert r.model_creative == "openai/gpt-6-sol"
    assert r.model_light == "openai/gpt-6-luna"


def test_unknown_tier_warns_and_falls_back_to_balanced(clean_env, caplog):
    clean_env.setenv("MODEL_TIER", "turbo")
    r = SmartAIRouter()
    assert r.model_tier == "balanced"
    assert r.model_generation == "openai/gpt-6-sol"
    assert "turbo" in caplog.text


def test_env_overrides_generation_and_light(clean_env):
    clean_env.setenv("MODEL_TIER", "max")
    clean_env.setenv("GENERATION_MODEL", "openai/gpt-6-astra")
    clean_env.setenv("LIGHT_MODEL", JEV_ROUTER)
    r = SmartAIRouter()
    assert r.model_generation == "openai/gpt-6-astra"
    assert r.model_light == JEV_ROUTER
    assert r.model_polish == "anthropic/claude-opus-5.5"


def test_nebius_chain_keeps_literal_nebius_slugs(clean_env):
    r = SmartAIRouter()
    assert r.model_creative not in r.nebius_models
    assert r.nebius_models[0] == "nousresearch/hermes-4-405b"


NEBIUS = {"provider": {"only": ["nebius"]}}


@pytest.mark.parametrize("slug", [
    "openai/gpt-6-sol", "openai/gpt-6-luna", "openai/gpt-6-astra",
    "anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5.5", "anthropic/claude-opus-5.5", "anthropic/claude-fable-5.1",
])
def test_openai_and_anthropic_never_get_the_nebius_pin(slug):
    extra = SmartAIRouter._or_extra(slug, legacy=NEBIUS)
    assert "nebius" not in json.dumps(extra)
    expected = {"only": ["openai"]} if slug.startswith("openai/") else {"allow_fallbacks": True}
    assert extra["provider"] == expected


def test_or_extra_reasoning_mapping():
    fast = {**NEBIUS, "reasoning": {"enabled": False}}
    assert SmartAIRouter._or_extra("openai/gpt-6-luna", legacy=fast)["reasoning"] == {"enabled": False}
    assert SmartAIRouter._or_extra("openai/gpt-6-luna", legacy=NEBIUS)["reasoning"] == {"effort": "low"}
    medium = {"reasoning": {"effort": "medium"}}
    assert SmartAIRouter._or_extra("anthropic/claude-opus-5.5", legacy=medium)["reasoning"] == {"effort": "medium"}
    assert SmartAIRouter._or_extra("anthropic/claude-sonnet-5", "off")["reasoning"] == {"enabled": False}
    # Mandatory-reasoning models cannot be switched off: low effort instead.
    assert SmartAIRouter._or_extra("anthropic/claude-opus-5.5", "off")["reasoning"] == {"effort": "low"}
    assert SmartAIRouter._or_extra("anthropic/claude-sonnet-5.5", "off")["reasoning"] == {"effort": "low"}


def test_or_extra_keeps_legacy_for_nebius_hosted_slugs():
    assert SmartAIRouter._or_extra("openai/gpt-oss-120b", legacy=NEBIUS) is NEBIUS
    assert SmartAIRouter._or_extra("qwen/qwen3.5-397b-a17b", legacy=NEBIUS) is NEBIUS
    assert SmartAIRouter._or_extra(JEV_ROUTER, legacy=NEBIUS) == {}


async def test_light_call_with_nebius_extra_routes_to_openai(clean_env):
    completions = FakeCompletions(['["a"]'])
    router = _router_with(completions)
    await router._qwen_create([{"role": "user", "content": "x"}],
                              or_extra_body=router.OR_PROVIDER_NEBIUS_FAST)
    assert completions.calls[0]["model"] == "openai/gpt-6-luna"
    assert completions.calls[0]["extra_body"] == {
        "provider": {"only": ["openai"]}, "reasoning": {"enabled": False}}


async def test_creative_polish_override_routes_to_openai(clean_env):
    completions = FakeCompletions(["polished"])
    router = _router_with(completions)
    await router._polish_create([{"role": "user", "content": "x"}], model=router.model_creative)
    assert completions.calls[0]["model"] == "openai/gpt-6-luna"
    assert completions.calls[0]["extra_body"]["provider"] == {"only": ["openai"]}


async def test_jev_light_call_sends_no_extra_params(clean_env):
    clean_env.setenv("LIGHT_MODEL", JEV_ROUTER)
    completions = FakeCompletions(['{"ok": true}'])
    router = _router_with(completions)
    resp = await router._qwen_create(
        [{"role": "user", "content": "x"}],
        response_format={"type": "json_object"},
        or_extra_body=router.OR_PROVIDER_NEBIUS_FAST,
        purpose="classify",
    )
    assert router._safe_content(resp) == '{"ok": true}'
    call = completions.calls[0]
    assert call["model"] == JEV_ROUTER
    assert not {"extra_body", "response_format"} & call.keys()
    assert router.models_used["classify"]["model"] == JEV_ROUTER


@pytest.mark.parametrize("bad", ["", "not json"])
async def test_jev_bad_answer_falls_back_once_to_luna(clean_env, bad):
    clean_env.setenv("LIGHT_MODEL", JEV_ROUTER)
    completions = FakeCompletions([bad, '["kept"]'])
    router = _router_with(completions)
    resp = await router._qwen_create([{"role": "user", "content": "x"}],
                                     expect_json=True, purpose="filter")
    assert router._safe_content(resp) == '["kept"]'
    assert [c["model"] for c in completions.calls] == [JEV_ROUTER, "openai/gpt-6-luna"]
    assert completions.calls[1]["extra_body"]["provider"] == {"only": ["openai"]}
    assert router.models_used["filter"] == {
        "model": "openai/gpt-6-luna", "provider": "unknown-via-openrouter", "downgraded": True}


async def test_cost_accumulates_from_openrouter_usage(clean_env):
    usage = SimpleNamespace(cost=0.0125, total_tokens=500)
    router = SmartAIRouter()
    completions = FakeCompletions(["a", "b"], usage=usage)
    router._meter(completions)
    await completions.create(model="openai/gpt-6-sol", messages=[])
    await completions.create(model="openai/gpt-6-sol", messages=[])

    byok = {"cost": 0.001, "is_byok": True, "cost_details": {"upstream_inference_cost": 0.02}}
    router._record_usage("anthropic/claude-sonnet-5", SimpleNamespace(usage=byok))
    router._record_usage("gemini-3.8-flash", SimpleNamespace(usage={"total_tokens": 900}))
    router._record_usage("gemini-3.8-flash", SimpleNamespace(usage=None))

    summary = router.cost_summary()
    assert summary["by_model"] == {"openai/gpt-6-sol": 0.025, "anthropic/claude-sonnet-5": 0.021}
    assert summary["total_usd"] == pytest.approx(0.046)
    assert summary["uncosted_tokens"] == {"gemini-3.8-flash": 900}


async def test_quality_judge_raw_path_has_no_nebius_pin():
    completions = FakeCompletions([json.dumps({
        d: {"score": 80, "rationale": "r"} for d in ("clarity", "fluency", "authoritative_tone")})])
    router = SimpleNamespace(
        or_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        model_light="openai/gpt-6-luna",
        OR_PROVIDER_NEBIUS_FAST={**NEBIUS, "reasoning": {"enabled": False}},
        _safe_content=SmartAIRouter._safe_content,
    )
    await judge_quality("Body text.", "kw", router)
    assert completions.calls[0]["extra_body"] == {
        "provider": {"only": ["openai"]}, "reasoning": {"enabled": False}}


class FinishCompletions(FakeCompletions):
    """FakeCompletions whose responses carry a finish_reason (Gemini direct)."""
    def __init__(self, replies):
        super().__init__([c for c, _ in replies])
        self.finishes = [f for _, f in replies]

    async def create(self, **kwargs):
        resp = await super().create(**kwargs)
        resp.choices[0].finish_reason = self.finishes.pop(0) if self.finishes else "stop"
        return resp


@pytest.mark.parametrize("model,enabled,effort", [
    ("google/gemini-3.8-flash", False, "none"),
    ("google/gemini-3.8-flash", True, "low"),
    ("google/gemini-3.1-pro-preview", False, "low"),  # Pro rejects "none"
])
async def test_direct_gemini_caps_thinking(clean_env, model, enabled, effort):
    router = SmartAIRouter()
    direct = FinishCompletions([("answer", "stop")])
    router.direct_gemini_client = SimpleNamespace(chat=SimpleNamespace(completions=direct))
    msg = await router._call_gemini_with_fallback(
        [{"role": "user", "content": "x"}], model=model, reasoning_enabled=enabled)
    assert msg.content == "answer"
    assert direct.calls[0]["reasoning_effort"] == effort


async def test_direct_gemini_truncation_falls_back(clean_env):
    """finish_reason=length is a failed call, never a short answer."""
    router = _router_with(FakeCompletions(["full answer"]))
    router.direct_gemini_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FinishCompletions([("cut off mid", "length")])))
    msg = await router._call_gemini_with_fallback(
        [{"role": "user", "content": "x"}], model="google/gemini-3.8-flash", reasoning_enabled=False)
    assert msg.content == "full answer"


def test_semicolon_to_period_capitalises_next_sentence():
    from core.ai_router import _semicolon_to_period
    assert _semicolon_to_period("paid monthly; they are not; **bold**") == "paid monthly. They are not. **Bold**"


def test_merge_cost_combines_auxiliary_routers(clean_env):
    main_router = SmartAIRouter()
    main_router.cost_by_model["openai/gpt-6-sol"] = 0.15
    main_router.uncosted_tokens["google/gemini-3.8-flash"] = 500

    est_router = SmartAIRouter()
    est_router.cost_by_model["qwen/qwen3-embedding-8b"] = 0.01

    comm_router = SmartAIRouter()
    comm_router.cost_by_model["openai/gpt-6-luna"] = 0.04
    comm_router.uncosted_tokens["google/gemini-3.8-flash"] = 300

    main_router.merge_cost(est_router, comm_router, None, main_router)
    summary = main_router.cost_summary()
    assert summary["total_usd"] == 0.20
    assert summary["by_model"] == {
        "openai/gpt-6-sol": 0.15,
        "qwen/qwen3-embedding-8b": 0.01,
        "openai/gpt-6-luna": 0.04,
    }
    assert summary["uncosted_tokens"] == {"google/gemini-3.8-flash": 800}


async def test_router_with_isolates_dedicated_role_keys_without_clean_env(monkeypatch):
    monkeypatch.setenv("FLEO_POLISH_API_KEY", "sk-proj-test-polish-role")
    monkeypatch.setenv("FLEO_LIGHT_API_KEY", "sk-proj-test-light-role")
    completions = FakeCompletions(["polished-ok"])
    router = _router_with(completions)
    await router._polish_create([{"role": "user", "content": "x"}], model=router.model_creative)
    assert len(completions.calls) == 1
