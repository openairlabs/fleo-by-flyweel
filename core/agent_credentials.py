"""Per-agent API key, base URL, and model routing resolver + live connectivity tester."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import aiohttp

ProviderKind = Literal["openrouter", "google_ai_studio", "perplexity", "openai", "anthropic", "custom", "missing"]

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GOOGLE_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENAI_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_OPENAI_BASE_URL = "https://api.anthropic.com/v1/"
PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"

ROLE_TIER_DEFAULTS: Dict[str, Dict[str, str]] = {
    "balanced": {
        "RESEARCH": "sonar-reasoning-pro",
        "DRAFT": "openai/gpt-6-sol",
        "POLISH": "anthropic/claude-sonnet-5.5",
        "SEO": "google/gemini-3.8-flash",
        "LIGHT": "openai/gpt-6-luna",
        "EMBED": "qwen/qwen3-embedding-8b",
        "EXTRACTOR": "openai/gpt-6-luna",
        "DECISIONS": "typesafe/jev-1.13",
    },
    "max": {
        "RESEARCH": "sonar-reasoning-pro",
        "DRAFT": "anthropic/claude-opus-5.5",
        "POLISH": "anthropic/claude-opus-5.5",
        "SEO": "google/gemini-3.8-flash",
        "LIGHT": "openai/gpt-6-luna",
        "EMBED": "qwen/qwen3-embedding-8b",
        "EXTRACTOR": "openai/gpt-6-sol",
        "DECISIONS": "typesafe/jev-1.13",
    },
}

AGENT_ROLES_META: List[Dict[str, str]] = [
    {"role": "RESEARCH", "label": "Research Agent (SERP + Reddit/Quora)", "keyEnv": "FLEO_RESEARCH_API_KEY", "modelEnv": "FLEO_RESEARCH_MODEL"},
    {"role": "DRAFT", "label": "Writer / Draft Agent (3-part article)", "keyEnv": "FLEO_DRAFT_API_KEY", "modelEnv": "FLEO_DRAFT_MODEL"},
    {"role": "POLISH", "label": "Polish / Humanizer Agent", "keyEnv": "FLEO_POLISH_API_KEY", "modelEnv": "FLEO_POLISH_MODEL"},
    {"role": "SEO", "label": "SEO & Section Editor Agent", "keyEnv": "FLEO_SEO_API_KEY", "modelEnv": "FLEO_SEO_MODEL"},
    {"role": "LIGHT", "label": "Classifier & GEO Quality Judge", "keyEnv": "FLEO_LIGHT_API_KEY", "modelEnv": "FLEO_LIGHT_MODEL"},
    {"role": "EMBED", "label": "Semantic Embeddings Agent", "keyEnv": "FLEO_EMBED_API_KEY", "modelEnv": "FLEO_EMBED_MODEL"},
    {"role": "EXTRACTOR", "label": "Brand Website Extractor Agent", "keyEnv": "FLEO_EXTRACTOR_API_KEY", "modelEnv": "FLEO_EXTRACTOR_MODEL"},
    {"role": "DECISIONS", "label": "Typed Audit Decisions (Jev)", "keyEnv": "FLEO_DECISIONS_API_KEY", "modelEnv": "FLEO_JEV_ENABLED"},
]


@dataclass(frozen=True)
class RoleCredential:
    role: str
    api_key: Optional[str]
    base_url: str
    model: str
    provider: ProviderKind
    key_source_env: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.provider != "missing"


def detect_provider_from_key(api_key: str, explicit_base_url: Optional[str] = None) -> tuple[ProviderKind, str]:
    key = (api_key or "").strip()
    if not key:
        return "missing", OPENROUTER_BASE_URL
    if explicit_base_url:
        base = explicit_base_url.strip()
        try:
            parsed_base = urlsplit(base)
        except ValueError:
            return "missing", base
        if parsed_base.scheme.lower() != "https" or not parsed_base.netloc:
            return "missing", base
        host = (parsed_base.hostname or "").lower().rstrip(".")
        for domain, kind in (
            ("openrouter.ai", "openrouter"),
            ("generativelanguage.googleapis.com", "google_ai_studio"),
            ("perplexity.ai", "perplexity"),
            ("anthropic.com", "anthropic"),
            ("openai.com", "openai"),
        ):
            if host == domain or host.endswith(f".{domain}"):
                return kind, base
        return "custom", base

    if key.startswith("sk-or-"):
        return "openrouter", OPENROUTER_BASE_URL
    if key.startswith("pplx-"):
        return "perplexity", PERPLEXITY_API_URL
    if key.startswith("AIza"):
        return "google_ai_studio", GOOGLE_OPENAI_BASE_URL
    if key.startswith("sk-ant-"):
        return "anthropic", ANTHROPIC_OPENAI_BASE_URL
    if key.startswith(("sk-proj-", "sk-svcacct-", "sk-admin-")):
        return "openai", OPENAI_BASE_URL
    # Refuse unknown or ambiguous key prefixes (including bare `sk-` keys such as DeepSeek)
    # when no explicit HTTPS base URL is set so credentials are never routed to the wrong vendor.
    return "missing", ""


def normalize_model_for_provider(model: str, provider: ProviderKind) -> str:
    slug = (model or "").strip()
    if provider == "openrouter":
        if slug in {"sonar-reasoning-pro", "sonar-deep-research", "sonar-pro", "sonar"}:
            return f"perplexity/{slug}"
        if slug.startswith("gemini-"):
            return f"google/{slug}"
        return slug
    if provider == "perplexity":
        return slug.removeprefix("perplexity/")
    if provider == "google_ai_studio":
        return slug.removeprefix("google/")
    if provider == "openai":
        if slug == "qwen/qwen3-embedding-8b":
            return "text-embedding-3-small"
        return slug.removeprefix("openai/")
    if provider == "anthropic":
        return slug.removeprefix("anthropic/")
    return slug


def resolve_role_credential(
    role: str,
    *,
    env: Optional[Dict[str, str]] = None,
    tier: Optional[str] = None,
) -> RoleCredential:
    """Resolve `(api_key, base_url, model, provider, source_env)` for one agent function."""
    source = env if env is not None else os.environ
    role_up = role.strip().upper()
    active_tier = (tier or source.get("MODEL_TIER") or "balanced").strip().lower()
    if active_tier not in ROLE_TIER_DEFAULTS:
        active_tier = "balanced"
    defaults = ROLE_TIER_DEFAULTS[active_tier]

    explicit_base = (source.get(f"FLEO_{role_up}_BASE_URL") or "").strip() or None

    # Model resolution: FLEO_<ROLE>_MODEL -> legacy env -> tier default
    legacy_model_env = {
        "DRAFT": "GENERATION_MODEL",
        "LIGHT": "LIGHT_MODEL",
        "EXTRACTOR": "FLEO_LIGHT_MODEL",
    }.get(role_up)
    raw_model = (
        (source.get(f"FLEO_{role_up}_MODEL") or "").strip()
        or ((source.get(legacy_model_env) or "").strip() if legacy_model_env else "")
        or defaults.get(role_up, defaults["LIGHT"])
    )

    # Key precedence: FLEO_<ROLE>_API_KEY -> role-native provider env -> OPENROUTER_API_KEY
    candidates: List[tuple[str, str, Optional[ProviderKind], Optional[str]]] = [
        (f"FLEO_{role_up}_API_KEY", (source.get(f"FLEO_{role_up}_API_KEY") or "").strip(), None, explicit_base),
    ]
    if role_up == "RESEARCH":
        candidates.append(("PERPLEXITY_API_KEY", (source.get("PERPLEXITY_API_KEY") or "").strip(), "perplexity", PERPLEXITY_API_URL))
    elif role_up == "SEO":
        candidates.append(("GOOGLE_API_KEY", (source.get("GOOGLE_API_KEY") or "").strip(), "google_ai_studio", GOOGLE_OPENAI_BASE_URL))
        candidates.append(("GEMINI_API_KEY", (source.get("GEMINI_API_KEY") or "").strip(), "google_ai_studio", GOOGLE_OPENAI_BASE_URL))
    elif role_up == "EXTRACTOR":
        candidates.append(("FLEO_LIGHT_API_KEY", (source.get("FLEO_LIGHT_API_KEY") or "").strip(), None, source.get("FLEO_LIGHT_BASE_URL")))

    candidates.append(("OPENROUTER_API_KEY", (source.get("OPENROUTER_API_KEY") or "").strip(), "openrouter", OPENROUTER_BASE_URL))

    for env_name, key_val, forced_provider, forced_base in candidates:
        if not key_val:
            continue
        if forced_provider and not env_name.startswith("FLEO_"):
            provider, base_url = forced_provider, (forced_base or OPENROUTER_BASE_URL)
        else:
            candidate_base = explicit_base or ((forced_base or "").strip() or None)
            provider, base_url = detect_provider_from_key(key_val, candidate_base)
        if provider == "missing":
            if explicit_base or env_name.startswith("FLEO_"):
                return RoleCredential(
                    role=role_up,
                    api_key=None,
                    base_url=base_url,
                    model=raw_model,
                    provider="missing",
                    key_source_env="",
                )
            continue
        # Decisions API is OpenRouter-only
        if role_up == "DECISIONS" and provider != "openrouter":
            continue
        return RoleCredential(
            role=role_up,
            api_key=key_val,
            base_url=base_url,
            model=normalize_model_for_provider(raw_model, provider),
            provider=provider,
            key_source_env=env_name,
        )

    return RoleCredential(
        role=role_up,
        api_key=None,
        base_url=explicit_base or OPENROUTER_BASE_URL,
        model=raw_model,
        provider="missing",
        key_source_env="",
    )


def resolve_all_roles(env: Optional[Dict[str, str]] = None) -> Dict[str, RoleCredential]:
    return {meta["role"]: resolve_role_credential(meta["role"], env=env) for meta in AGENT_ROLES_META}


def can_generate_with_credentials(env: Optional[Dict[str, str]] = None) -> tuple[bool, List[str]]:
    """True when either OPENROUTER_API_KEY is set or core roles (RESEARCH, DRAFT, POLISH, SEO, LIGHT) all resolve."""
    roles = resolve_all_roles(env=env)
    required_roles = ("RESEARCH", "DRAFT", "POLISH", "SEO", "LIGHT")
    missing = [r for r in required_roles if not roles[r].configured]
    if not missing:
        return True, []
    return False, ["OPENROUTER_API_KEY (or per-agent keys for: " + ", ".join(missing) + ")"]


async def test_single_credential(cred: RoleCredential, timeout_seconds: float = 6.0) -> Dict[str, Any]:
    import aiohttp

    if not cred.configured or not cred.api_key:
        return {"role": cred.role, "ok": False, "provider": cred.provider, "detail": "Not configured"}

    t0 = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=4.0)
    try:
        parsed_base = urlsplit(cred.base_url.strip())
        scheme = parsed_base.scheme.lower()
        host = (parsed_base.hostname or "").lower()
        if scheme != "https" and not (scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}):
            return {
                "role": cred.role,
                "ok": False,
                "provider": cred.provider,
                "detail": "Rejected insecure non-HTTPS gateway URL (HTTPS required for remote endpoints)",
            }

        async with aiohttp.ClientSession(timeout=timeout) as session:
            base = cred.base_url.rstrip("/")
            if cred.provider == "openrouter":
                headers = {"Authorization": f"Bearer {cred.api_key}"}
                probe_url = base if base.endswith("/key") else f"{base}/key"
                async with session.get(probe_url, headers=headers) as resp:
                    ms = int((time.monotonic() - t0) * 1000)
                    if resp.status == 200:
                        return {"role": cred.role, "ok": True, "provider": "openrouter", "latencyMs": ms, "detail": f"OpenRouter verified ({ms}ms)"}
                    return {"role": cred.role, "ok": False, "provider": "openrouter", "detail": f"HTTP {resp.status} from OpenRouter"}

            if cred.provider == "google_ai_studio":
                headers = {"Authorization": f"Bearer {cred.api_key}"}
                probe_url = base if base.endswith("/models") else f"{base}/models"
                async with session.get(probe_url, headers=headers) as resp:
                    ms = int((time.monotonic() - t0) * 1000)
                    if resp.status == 200:
                        return {"role": cred.role, "ok": True, "provider": "google_ai_studio", "latencyMs": ms, "detail": f"Google AI Studio verified ({ms}ms)"}
                    return {"role": cred.role, "ok": False, "provider": "google_ai_studio", "detail": f"HTTP {resp.status} from Google AI Studio"}

            if cred.provider == "perplexity":
                headers = {"Authorization": f"Bearer {cred.api_key}", "Content-Type": "application/json"}
                payload = {"model": "sonar", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
                probe_url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
                async with session.post(probe_url, headers=headers, json=payload) as resp:
                    ms = int((time.monotonic() - t0) * 1000)
                    if resp.status == 200:
                        return {"role": cred.role, "ok": True, "provider": "perplexity", "latencyMs": ms, "detail": f"Perplexity Sonar verified ({ms}ms)"}
                    return {"role": cred.role, "ok": False, "provider": "perplexity", "detail": f"HTTP {resp.status} from Perplexity"}

            # OpenAI / Anthropic / custom OpenAI-compatible /models probe
            headers = {"Authorization": f"Bearer {cred.api_key}"}
            models_url = base if base.endswith("/models") else f"{base}/models"
            async with session.get(models_url, headers=headers) as resp:
                ms = int((time.monotonic() - t0) * 1000)
                if resp.status < 400:
                    return {"role": cred.role, "ok": True, "provider": cred.provider, "latencyMs": ms, "detail": f"{cred.provider} verified ({ms}ms)"}
                return {"role": cred.role, "ok": False, "provider": cred.provider, "detail": f"HTTP {resp.status} from {cred.provider}"}
    except Exception as exc:
        return {"role": cred.role, "ok": False, "provider": cred.provider, "detail": f"{type(exc).__name__}: {str(exc)[:100]}"}


async def post_sonar_chat(
    session: Optional[aiohttp.ClientSession],
    payload: Dict[str, Any],
    *,
    timeout: Any = None,
    explicit_key: Optional[str] = None,
    credential: Optional[RoleCredential] = None,
) -> Optional[Dict[str, Any]]:
    """Execute a Perplexity Sonar request via direct Perplexity API or OpenRouter/custom Sonar fallback.

    Normalizes OpenRouter's `annotations` URL citations into top-level `citations` so all
    downstream SERP/Reddit/Quora parsers work identically in 1-key OpenRouter mode.
    """
    if credential is not None and (not explicit_key or credential.api_key == explicit_key.strip()):
        cred = credential
    elif explicit_key:
        resolved = resolve_role_credential("RESEARCH")
        trimmed = explicit_key.strip()
        if resolved.configured and resolved.api_key == trimmed:
            cred = resolved
        else:
            fleo_research_key = (os.getenv("FLEO_RESEARCH_API_KEY") or "").strip()
            explicit_base = (os.getenv("FLEO_RESEARCH_BASE_URL") or "").strip() or None
            if explicit_base and fleo_research_key and trimmed == fleo_research_key:
                provider, url = detect_provider_from_key(trimmed, explicit_base)
            elif trimmed.startswith("sk-or-"):
                provider, url = "openrouter", OPENROUTER_BASE_URL
            else:
                provider, url = "perplexity", PERPLEXITY_API_URL
            cred = RoleCredential(
                role="RESEARCH",
                api_key=trimmed,
                base_url=url,
                model=str(payload.get("model") or resolved.model or "sonar-reasoning-pro"),
                provider=provider,
                key_source_env="EXPLICIT",
            )
    else:
        cred = resolve_role_credential("RESEARCH")

    if not cred.configured or not cred.api_key:
        return None

    import aiohttp

    req_body = dict(payload)
    raw_model = str(req_body.get("model") or cred.model or "sonar-reasoning-pro")
    headers = {
        "Authorization": f"Bearer {cred.api_key}",
        "Content-Type": "application/json",
    }

    if cred.provider == "perplexity":
        base = (cred.base_url or PERPLEXITY_API_URL).rstrip("/")
        endpoint = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        req_body["model"] = raw_model.removeprefix("perplexity/")
    else:
        endpoint = f"{cred.base_url.rstrip('/')}/chat/completions"
        req_body["model"] = normalize_model_for_provider(raw_model, cred.provider)
        if cred.provider == "openrouter":
            headers["HTTP-Referer"] = os.getenv("OPENROUTER_SITE_URL", "https://github.com/openairlabs/fleo-by-flyweel")
            headers["X-OpenRouter-Title"] = os.getenv("OPENROUTER_APP_NAME", "Fleo by Flyweel")
            # Strip Perplexity-proprietary top-level keys that non-Perplexity gateways reject if strict
            req_body.pop("search_mode", None)

    req_timeout = (
        timeout
        if isinstance(timeout, aiohttp.ClientTimeout)
        else aiohttp.ClientTimeout(total=float(timeout or 180), connect=30)
    )
    own = session is None
    client = session or aiohttp.ClientSession(timeout=req_timeout)
    try:
        async with client.post(endpoint, headers=headers, json=req_body, timeout=req_timeout) as resp:
            if resp.status != 200:
                return None
            try:
                data = await resp.json(content_type=None)
            except TypeError:
                data = await resp.json()
            if not isinstance(data, dict):
                return None
            if not data.get("citations"):
                annotations = (((data.get("choices") or [{}])[0].get("message") or {}).get("annotations") or [])
                urls = []
                for ann in annotations:
                    u = ((ann.get("url_citation") or {}).get("url") if isinstance(ann, dict) else None) or (
                        ann.get("url") if isinstance(ann, dict) else None
                    )
                    if isinstance(u, str) and u.startswith(("http://", "https://")) and u not in urls:
                        urls.append(u)
                if urls:
                    data["citations"] = urls
            return data
    finally:
        if own:
            await client.close()


async def test_agent_keys(overrides: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Deduplicate identical `(provider, api_key)` probes across roles so testing all 8 roles is instant."""
    merged_env = dict(os.environ)
    if overrides:
        for k, v in overrides.items():
            if v is not None and str(v).strip():
                merged_env[str(k).strip().upper()] = str(v).strip()

    roles = resolve_all_roles(env=merged_env)
    seen_probes: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    results: Dict[str, Any] = {}
    for role_name, cred in roles.items():
        if not cred.configured or not cred.api_key:
            results[role_name] = {"role": role_name, "ok": False, "configured": False, "provider": "missing", "model": cred.model, "sourceEnv": "", "detail": "Unset"}
            continue
        cache_key = (cred.provider, cred.base_url, cred.api_key)
        if cache_key not in seen_probes:
            seen_probes[cache_key] = await test_single_credential(cred)
        probe = dict(seen_probes[cache_key])
        probe.update({"role": role_name, "configured": True, "model": cred.model, "sourceEnv": cred.key_source_env})
        results[role_name] = probe

    core_ok = all(results[r]["ok"] for r in ("RESEARCH", "DRAFT", "POLISH", "SEO", "LIGHT"))
    return {"allCoreReady": core_ok, "roles": results}
