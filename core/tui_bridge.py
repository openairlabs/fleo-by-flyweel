"""JSONL bridge primitives for the Flyweel Content Engine TUI.

Stdout is intentionally reserved for machine-readable JSONL in the root
``tui_bridge.py`` runner. Keep helpers here free of Rich/print side effects.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, TextIO


BRIDGE_EVENT_TYPES = {
    "setup_status",
    "run_plan",
    "phase_started",
    "task_progress",
    "tool_call",
    "warning",
    "error",
    "artifact_ready",
    "artifact_conflict",
    "review_gate_opened",
    "review_gate_closed",
    "gsc_gate_opened",
    "gsc_gate_closed",
    "generation_complete",
    "saved",
    "published",
}

REVIEW_DECISIONS = {
    "approve",
    "apply_comments",
    "add_final_comment",
    "rerun_comment_edit",
    "cancel",
}

GSC_DECISIONS = {
    "proceed",
    "abort",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class BridgeEvent:
    type: str
    runId: str
    timestamp: str = field(default_factory=utc_now_iso)
    phase: Optional[str] = None
    agent: Optional[str] = None
    taskId: Optional[str] = None
    message: Optional[str] = None
    data: Any = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "type": self.type,
            "runId": self.runId,
            "timestamp": self.timestamp,
        }
        for key in ("phase", "agent", "taskId", "message", "data"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


def event_to_jsonl(event: BridgeEvent) -> str:
    return json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"


def make_event(
    event_type: str,
    run_id: str,
    *,
    phase: Optional[str] = None,
    agent: Optional[str] = None,
    task_id: Optional[str] = None,
    message: Optional[str] = None,
    data: Any = None,
) -> BridgeEvent:
    if event_type not in BRIDGE_EVENT_TYPES:
        raise ValueError(f"Unknown bridge event type: {event_type}")
    return BridgeEvent(
        type=event_type,
        runId=run_id,
        phase=phase,
        agent=agent,
        taskId=task_id,
        message=message,
        data=data,
    )


def parse_command_line(line: str, *, expected_run_id: Optional[str] = None) -> Dict[str, Any]:
    """Parse and validate one command JSONL line.

    Raises ``ValueError`` with user-safe messages. The runner converts these
    into warning events rather than crashing.
    """
    try:
        command = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed command JSON: {exc.msg}") from exc

    if not isinstance(command, dict):
        raise ValueError("Malformed command: expected a JSON object")

    command_type = command.get("type")
    if not isinstance(command_type, str) or command_type not in {"queue_comment", "review_decision", "gsc_decision", "cancel"}:
        raise ValueError(f"Unsupported command type: {command_type!r}")

    run_id = command.get("runId")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Malformed command: runId is required")
    if expected_run_id and run_id != expected_run_id:
        raise ValueError("Command runId does not match this bridge run")

    if command_type == "queue_comment":
        comment = command.get("comment")
        if not isinstance(comment, str) or not comment.strip():
            raise ValueError("Malformed queue_comment: comment is required")
        return {"type": command_type, "runId": run_id, "comment": comment.strip()}

    if command_type == "review_decision":
        decision = command.get("decision")
        if not isinstance(decision, str) or decision not in REVIEW_DECISIONS:
            raise ValueError(f"Malformed review_decision: invalid decision {decision!r}")
        parsed = {"type": command_type, "runId": run_id, "decision": decision}
        comment = command.get("comment")
        if isinstance(comment, str) and comment.strip():
            parsed["comment"] = comment.strip()
        return parsed

    if command_type == "gsc_decision":
        decision = command.get("decision")
        if not isinstance(decision, str) or decision not in GSC_DECISIONS:
            raise ValueError(f"Malformed gsc_decision: invalid decision {decision!r}")
        return {"type": command_type, "runId": run_id, "decision": decision}

    return {"type": command_type, "runId": run_id}


class JsonlEmitter:
    """Write bridge events as JSONL to stdout or a test stream."""

    def __init__(self, run_id: str, stream: Optional[TextIO] = None):
        self.run_id = run_id
        self.stream = stream or sys.stdout
        self._lock = threading.Lock()

    def emit(
        self,
        event_type: str,
        *,
        phase: Optional[str] = None,
        agent: Optional[str] = None,
        task_id: Optional[str] = None,
        message: Optional[str] = None,
        data: Any = None,
    ) -> BridgeEvent:
        event = make_event(
            event_type,
            self.run_id,
            phase=phase,
            agent=agent,
            task_id=task_id,
            message=message,
            data=data,
        )
        line = event_to_jsonl(event)
        with self._lock:
            self.stream.write(line)
            self.stream.flush()
        return event

    def warning(self, message: str, *, data: Any = None) -> BridgeEvent:
        return self.emit("warning", phase="bridge", agent="Bridge", message=message, data=data)


class BridgeRunCancelled(Exception):
    """Raised when Ink sends a cooperative cancel command."""


def _has_module_spec(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


_STDLIB_FRAMEWORK_LABELS = {
    "nextjs": "Next.js",
    "astro": "Astro",
    "webflow": "Webflow",
    "framer": "Framer",
    "nuxt": "Nuxt",
    "sveltekit": "SvelteKit",
    "remix": "Remix / React Router",
    "markdown": "Universal Markdown",
}


def detect_setup_status(repo_root: Optional[Path] = None) -> Dict[str, Any]:
    from types import SimpleNamespace
    from .agent_credentials import AGENT_ROLES_META, can_generate_with_credentials, resolve_all_roles
    from .paths import PACKAGE_ROOT, blog_content_path, brand_profile_path, fleo_dir, output_root, site_contract_path, workspace_root

    ws = workspace_root(repo_root)
    env_path = ws / ".env"
    pkg_env_path = PACKAGE_ROOT / ".env"
    output_path = output_root(repo_root=ws)
    try:
        output_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    resolved_blog_path = blog_content_path(repo_root=ws)

    merged_env: Dict[str, str] = {}
    for candidate_env in (pkg_env_path, env_path):
        if not candidate_env.is_symlink() and candidate_env.exists() and candidate_env.is_file():
            try:
                if candidate_env.stat().st_size > 256 * 1024:
                    continue
                for raw_line in candidate_env.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    val = value.strip().strip("'\"")
                    if key.strip() and val:
                        merged_env[key.strip()] = val
            except OSError:
                pass
    for k, v in os.environ.items():
        if v:
            merged_env[k] = v

    def has_env_key(name: str) -> bool:
        return bool(merged_env.get(name, "").strip())

    gsc_ready = has_env_key("GOOGLE_SERVICE_ACCOUNT_PATH") and (
        has_env_key("GSC_SITE_URL") or has_env_key("GSC_PROPERTY_URL")
    )

    engine_deps_ok = all(
        _has_module_spec(mod)
        for mod in ("openai", "aiohttp", "pydantic", "yaml", "bs4", "defusedxml", "rich", "click", "questionary")
    )
    home_venv = Path.home() / ".fleo" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_ok = engine_deps_ok or any(
        p.exists()
        for p in (
            ws / "venv" / "bin" / "python",
            ws / "venv" / "Scripts" / "python.exe",
            ws / ".venv" / "bin" / "python",
            ws / ".venv" / "Scripts" / "python.exe",
            PACKAGE_ROOT / "venv" / "bin" / "python",
            PACKAGE_ROOT / "venv" / "Scripts" / "python.exe",
            PACKAGE_ROOT / ".venv" / "bin" / "python",
            PACKAGE_ROOT / ".venv" / "Scripts" / "python.exe",
            home_venv,
        )
    )

    creds_ok, _cred_missing_msg = can_generate_with_credentials(env=merged_env)
    roles = resolve_all_roles(env=merged_env)
    try:
        from .brand_profile import discover_product_marketing_file, load_brand_profile
        from .site_adapters import FRAMEWORK_LABELS, load_site_contract

        profile = load_brand_profile(repo_root=ws)
        contract = load_site_contract(repo_root=ws)
        pm_file = discover_product_marketing_file(repo_root=ws)
    except ImportError:
        FRAMEWORK_LABELS = _STDLIB_FRAMEWORK_LABELS

        def _load_json_dict(path: Path) -> Dict[str, Any]:
            if not path.exists():
                return {}
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                return loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                return {}

        raw_bp = _load_json_dict(brand_profile_path(ws))
        c_name = str(raw_bp.get("company_name") or "").strip()
        s_url = str(raw_bp.get("site_url") or "").strip()
        profile = SimpleNamespace(
            company_name=c_name,
            site_url=s_url,
            category=str(raw_bp.get("category") or "").strip(),
            is_configured=lambda: bool(c_name or s_url),
        )
        raw_sc = _load_json_dict(site_contract_path(ws))
        raw_count = raw_sc.get("existing_post_count")
        contract = SimpleNamespace(
            mode=str(raw_sc.get("mode") or "handoff"),
            framework=str(raw_sc.get("framework") or merged_env.get("FLEO_TARGET_FRAMEWORK") or "nextjs"),
            content_dir=str(raw_sc.get("content_dir") or ""),
            existing_post_count=int(raw_count) if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool) else 0,
        )
        pm_candidates = (
            ws / ".agents" / "product-marketing.md",
            ws / ".claude" / "product-marketing-context.md",
        )
        pm_file = next((p for p in pm_candidates if p.exists()), None)
    onboarding_marker = fleo_dir(ws) / "onboarding.json"

    checks = [
        {
            "id": "venv",
            "label": "Python engine runtime",
            "required": True,
            "ok": venv_ok,
            "detail": sys.executable,
        },
        {
            "id": "ai_credentials",
            "label": "AI model credentials",
            "required": True,
            "ok": creds_ok,
            "detail": "OPENROUTER_API_KEY or per-agent FLEO_<ROLE>_API_KEY",
        },
        {
            "id": "perplexity",
            "label": "Direct Perplexity Sonar (optional)",
            "required": False,
            "ok": roles["RESEARCH"].provider == "perplexity",
            "detail": "PERPLEXITY_API_KEY / FLEO_RESEARCH_API_KEY (falls back to OpenRouter Sonar)",
        },
        {
            "id": "google",
            "label": "Direct Google AI Studio (optional)",
            "required": False,
            "ok": roles["SEO"].provider == "google_ai_studio",
            "detail": "GOOGLE_API_KEY / FLEO_SEO_API_KEY (falls back to OpenRouter Gemini)",
        },
        {
            "id": "brand_profile",
            "label": "Brand context profile",
            "required": False,
            "ok": profile.is_configured(),
            "detail": f"{profile.company_name} ({profile.site_url or profile.category})" if profile.is_configured() else "Unconfigured (educational mode)",
        },
        {
            "id": "gsc",
            "label": "Google Search Console (optional)",
            "required": False,
            "ok": gsc_ready,
            "detail": "GOOGLE_SERVICE_ACCOUNT_PATH + GSC_PROPERTY_URL",
        },
        {
            "id": "output",
            "label": "Output directory",
            "required": True,
            "ok": output_path.exists(),
            "detail": str(output_path),
        },
        {
            "id": "site_target",
            "label": "Website target",
            "required": False,
            "ok": contract.mode == "handoff" or resolved_blog_path.exists(),
            "detail": (
                f"Connected {FRAMEWORK_LABELS.get(contract.framework, contract.framework)} → {contract.content_dir}"
                if contract.mode == "repo"
                else f"Agent Handoff (.md) · {FRAMEWORK_LABELS.get(contract.framework, contract.framework)}"
            ),
        },
    ]
    required_missing = [check["id"] for check in checks if check["required"] and not check["ok"]]
    optional_missing = [check["id"] for check in checks if not check["required"] and not check["ok"]]
    can_gen = len(required_missing) == 0

    agent_roles_summary = [
        {
            "role": meta["role"],
            "label": meta["label"],
            "keyEnv": meta["keyEnv"],
            "modelEnv": meta["modelEnv"],
            "configured": roles[meta["role"]].configured,
            "provider": roles[meta["role"]].provider,
            "model": roles[meta["role"]].model,
            "sourceEnv": roles[meta["role"]].key_source_env,
        }
        for meta in AGENT_ROLES_META
    ]

    return {
        "repoRoot": str(ws),
        "checks": checks,
        "canGenerate": can_gen,
        "requiredMissing": required_missing,
        "optionalMissing": optional_missing,
        "onboardingComplete": onboarding_marker.exists() or (can_gen and profile.is_configured()),
        "brandConfigured": profile.is_configured(),
        "brandName": profile.company_name,
        "brandSiteUrl": profile.site_url,
        "brandCategory": profile.category,
        "productMarketingFile": str(pm_file) if pm_file else None,
        "siteTarget": {
            "mode": contract.mode,
            "framework": contract.framework,
            "frameworkLabel": FRAMEWORK_LABELS.get(contract.framework, contract.framework),
            "contentDir": contract.content_dir or str(resolved_blog_path),
            "existingPostCount": contract.existing_post_count,
        },
        "agentRoles": agent_roles_summary,
    }


SETUP_ENV_KEYS = {
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "PERPLEXITY_API_KEY",
    "MODEL_TIER",
    "GENERATION_MODEL",
    "LIGHT_MODEL",
    "FLEO_RESEARCH_API_KEY",
    "FLEO_RESEARCH_MODEL",
    "FLEO_DEEP_RESEARCH_MODEL",
    "FLEO_DRAFT_API_KEY",
    "FLEO_DRAFT_MODEL",
    "FLEO_POLISH_API_KEY",
    "FLEO_POLISH_MODEL",
    "FLEO_SEO_API_KEY",
    "FLEO_SEO_MODEL",
    "FLEO_LIGHT_API_KEY",
    "FLEO_LIGHT_MODEL",
    "FLEO_EMBED_API_KEY",
    "FLEO_EMBED_MODEL",
    "FLEO_EXTRACTOR_API_KEY",
    "FLEO_EXTRACTOR_MODEL",
    "FLEO_DECISIONS_API_KEY",
    "FLEO_JEV_ENABLED",
    "GOOGLE_SERVICE_ACCOUNT_PATH",
    "GSC_SITE_URL",
    "GSC_PROPERTY_URL",
    "INTERFAZE_API_KEY",
    "INDEXNOW_KEY",
    "INDEXNOW_KEY_LOCATION",
    "FLEO_TARGET_FRAMEWORK",
    "FLEO_SITE_REPO_PATH",
    "FLEO_BLOG_CONTENT_PATH",
    "FLEO_BLOG_ASSETS_PATH",
    "ASTRO_BLOG_PATH",
    "ASTRO_ASSETS_PATH",
    "OUTPUT_DIR",
}


def save_setup_changes(values: Dict[str, Any], repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Merge confirmed setup values into repo-root .env.

    The caller is responsible for confirmation. This helper validates keys so
    the TUI cannot accidentally turn arbitrary JSON into environment lines.
    """
    from .paths import atomic_write_text, workspace_root
    root = workspace_root(repo_root)
    env_path = root / ".env"
    updates: Dict[str, str] = {}
    removals: set[str] = set()
    for key, raw_value in values.items():
        normalized_key = str(key).strip().upper()
        value = "" if raw_value is None else str(raw_value).strip()
        if normalized_key not in SETUP_ENV_KEYS:
            raise ValueError(f"Unsupported setup key: {key}")
        if len(value.splitlines()) > 1 or any(
            ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F or ch in ("\u2028", "\u2029")
            for ch in value
        ):
            raise ValueError(f"Setup value for {normalized_key} contains forbidden control characters")
        if value:
            updates[normalized_key] = value
        else:
            removals.add(normalized_key)

    if not updates and not removals:
        raise ValueError("No setup values were provided")

    if env_path.is_symlink() or (env_path.exists() and not env_path.is_file()):
        raise ValueError(".env must be a regular file")

    existing_lines: List[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    written_keys = set()
    next_lines: List[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            next_lines.append(line)
            continue
        key, _value = line.split("=", 1)
        normalized_key = key.strip().upper()
        if normalized_key in removals:
            continue
        if normalized_key in updates:
            next_lines.append(f"{normalized_key}={updates[normalized_key]}")
            written_keys.add(normalized_key)
        else:
            next_lines.append(line)

    for key, value in updates.items():
        if key not in written_keys:
            next_lines.append(f"{key}={value}")

    existing_mode = (env_path.stat().st_mode & 0o777) if env_path.exists() else 0o600
    target_mode = existing_mode & 0o600
    payload = "\n".join(next_lines).rstrip() + "\n"
    atomic_write_text(env_path, payload, root=root, mode=target_mode)
    os.chmod(env_path, target_mode)

    output_dir = updates.get("OUTPUT_DIR")
    if output_dir:
        (root / output_dir).expanduser().mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {
        "envPath": str(env_path),
        "updatedKeys": sorted(updates.keys() | removals),
    }
    if (root / ".git").exists():
        try:
            chk = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-C", str(root), "check-ignore", "-q", ".env"],
                capture_output=True,
                timeout=2,
                check=False,
            )
            if chk.returncode == 1:
                result["gitIgnoreWarning"] = ".env is not in .gitignore in this repository — add .env to .gitignore before committing."
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


class BridgeController:
    """Collect commands from Ink and expose a pre-polish review callback."""

    def __init__(self, run_id: str, emitter: JsonlEmitter):
        self.run_id = run_id
        self.emitter = emitter
        self.queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self.queued_comments: List[str] = []
        self.pending_review_decisions: List[Dict[str, Any]] = []
        self.pending_gsc_decisions: List[Dict[str, Any]] = []
        self.cancelled = False
        self.stdin_closed = False

    def start_stdin_reader(self, stdin: TextIO = sys.stdin) -> threading.Thread:
        loop = asyncio.get_running_loop()

        def _reader() -> None:
            try:
                for line in stdin:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        command = parse_command_line(stripped, expected_run_id=self.run_id)
                    except ValueError as exc:
                        loop.call_soon_threadsafe(
                            self.queue.put_nowait,
                            {"type": "_warning", "message": str(exc)},
                        )
                        continue
                    loop.call_soon_threadsafe(self.queue.put_nowait, command)
            finally:
                loop.call_soon_threadsafe(self.queue.put_nowait, {"type": "_stdin_closed"})

        thread = threading.Thread(target=_reader, name="flyweel-tui-stdin", daemon=True)
        thread.start()
        return thread

    async def drain_pending_commands(self) -> None:
        while True:
            try:
                command = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._handle_background_command(command)

    def drain_pending_commands_nowait(self) -> None:
        while True:
            try:
                command = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._handle_background_command(command)

    def _handle_background_command(self, command: Dict[str, Any]) -> None:
        command_type = command.get("type")
        if command_type == "_warning":
            self.emitter.warning(command.get("message", "Malformed command"))
        elif command_type == "_stdin_closed":
            self.stdin_closed = True
        elif command_type == "queue_comment":
            self.queued_comments.append(command["comment"])
            self.emitter.emit(
                "task_progress",
                phase="review",
                agent="Comment Gate Agent",
                task_id="queued-comments",
                message="Queued comment",
                data={"commentCount": len(self.queued_comments)},
            )
        elif command_type == "cancel":
            self.cancelled = True
            self.emitter.emit("warning", phase="bridge", agent="Bridge", message="Cancel requested")
        elif command_type == "review_decision":
            self.pending_review_decisions.append(command)
        elif command_type == "gsc_decision":
            self.pending_gsc_decisions.append(command)

    async def review_gate_callback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        await self.drain_pending_commands()
        if self.cancelled:
            return {"decision": "cancel", "comments": list(self.queued_comments)}

        content = payload.get("content", "") or ""
        self.emitter.emit(
            "review_gate_opened",
            phase="review",
            agent="Comment Gate Agent",
            task_id="gate-open",
            message="Pre-polish review gate opened",
            data={
                "queuedComments": list(self.queued_comments),
                "wordCount": len(content.split()),
                "citationStatus": payload.get("citation_verification", {}).get("verification_status"),
            },
        )
        self.emitter.emit(
            "artifact_ready",
            phase="review",
            agent="Comment Gate Agent",
            task_id="pre-polish-mdx",
            message="Pre-polish MDX ready",
            data={"artifact": "pre_polish_mdx", "content": content},
        )

        if self.pending_review_decisions:
            command = self.pending_review_decisions.pop(0)
            final_comments = list(self.queued_comments)
            if command.get("comment"):
                final_comments.append(command["comment"])
            self.emitter.emit(
                "review_gate_closed",
                phase="review",
                agent="Comment Gate Agent",
                task_id="gate-closed",
                message=f"Review decision: {command['decision']}",
                data={"decision": command["decision"], "commentCount": len(final_comments)},
            )
            return {
                "decision": command["decision"],
                "comments": final_comments,
                "comment": command.get("comment"),
            }

        if self.stdin_closed:
            decision = "apply_comments" if self.queued_comments else "approve"
            self.emitter.emit(
                "review_gate_closed",
                phase="review",
                agent="Comment Gate Agent",
                task_id="gate-closed",
                message="stdin closed; continuing generation",
                data={"decision": decision, "commentCount": len(self.queued_comments)},
            )
            return {"decision": decision, "comments": list(self.queued_comments)}

        while True:
            command = await self.queue.get()
            command_type = command.get("type")
            if command_type == "_warning":
                self.emitter.warning(command.get("message", "Malformed command"))
                continue
            if command_type == "_stdin_closed":
                self.stdin_closed = True
                decision = "apply_comments" if self.queued_comments else "approve"
                self.emitter.emit(
                    "review_gate_closed",
                    phase="review",
                    agent="Comment Gate Agent",
                    task_id="gate-closed",
                    message="stdin closed; continuing generation",
                    data={"decision": decision, "commentCount": len(self.queued_comments)},
                )
                return {"decision": decision, "comments": list(self.queued_comments)}
            if command_type == "queue_comment":
                self.queued_comments.append(command["comment"])
                self.emitter.emit(
                    "task_progress",
                    phase="review",
                    agent="Comment Gate Agent",
                    task_id="queued-comments",
                    message="Queued comment",
                    data={"commentCount": len(self.queued_comments)},
                )
                continue
            if command_type == "cancel":
                command = {"type": "review_decision", "decision": "cancel"}
            if command_type == "review_decision" or command.get("type") == "review_decision":
                decision = command["decision"]
                final_comments = list(self.queued_comments)
                if command.get("comment"):
                    final_comments.append(command["comment"])
                self.emitter.emit(
                    "review_gate_closed",
                    phase="review",
                    agent="Comment Gate Agent",
                    task_id="gate-closed",
                    message=f"Review decision: {decision}",
                    data={"decision": decision, "commentCount": len(final_comments)},
                )
                return {
                    "decision": decision,
                    "comments": final_comments,
                    "comment": command.get("comment"),
                }

    async def gsc_gate_callback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Low-GSC-score gate: ask Ink whether to still produce the article.

        Mirrors review_gate_callback's queue mechanics. Auto-proceeds when stdin
        is closed (headless) so the gate can never strand an unattended run.
        """
        await self.drain_pending_commands()
        if self.cancelled:
            return {"decision": "abort"}

        def _emit_closed(decision: str, message: str) -> Dict[str, Any]:
            self.emitter.emit(
                "gsc_gate_closed",
                phase="gsc",
                agent="GSC Gate Agent",
                task_id="gsc-gate-closed",
                message=message,
                data={"decision": decision},
            )
            return {"decision": decision}

        estimated = bool(payload.get("estimated", False))
        self.emitter.emit(
            "gsc_gate_opened",
            phase="gsc",
            agent="GSC Gate Agent",
            task_id="gsc-gate",
            message=(
                f"{'Estimated ' if estimated else ''}GSC score {payload.get('score', 0)}% is below "
                f"{payload.get('threshold', 0)}% for '{payload.get('keyword', '')}'"
            ),
            data={
                "score": payload.get("score"),
                "threshold": payload.get("threshold"),
                "trafficPotential": payload.get("traffic_potential"),
                "primaryQuery": payload.get("primary_query"),
                "impressions": payload.get("impressions"),
                "keyword": payload.get("keyword"),
                "estimated": estimated,
                "neighborCount": payload.get("neighbor_count"),
            },
        )

        if self.pending_gsc_decisions:
            command = self.pending_gsc_decisions.pop(0)
            return _emit_closed(command["decision"], f"GSC gate decision: {command['decision']}")

        if self.stdin_closed:
            return _emit_closed("proceed", "stdin closed; proceeding despite low GSC score")

        while True:
            command = await self.queue.get()
            command_type = command.get("type")
            if command_type == "_warning":
                self.emitter.warning(command.get("message", "Malformed command"))
                continue
            if command_type == "_stdin_closed":
                self.stdin_closed = True
                return _emit_closed("proceed", "stdin closed; proceeding despite low GSC score")
            if command_type == "queue_comment":
                self._handle_background_command(command)
                continue
            if command_type == "cancel":
                self.cancelled = True
                return _emit_closed("abort", "Cancel requested at GSC gate")
            if command_type == "gsc_decision":
                return _emit_closed(command["decision"], f"GSC gate decision: {command['decision']}")
            # Anything else (e.g. an early review_decision) is buffered for later.
            self._handle_background_command(command)


def _fuzzy_phase(text: str) -> str:
    """Best-effort phase for legacy coarse messages that carry no taskId.

    Keys are canonical ``PIPELINE_PHASES`` manifest keys so the TUI can route
    these messages onto the seeded todo rows of the right phase group.
    """
    lower = text.lower()
    if "gsc" in lower or "cannibalization" in lower:
        return "gsc"
    if "review" in lower or "comment" in lower:
        return "review"
    if "research" in lower or "serp" in lower or "reddit" in lower or "quora" in lower:
        return "research"
    if "format" in lower or "mdx" in lower or "slug" in lower:
        return "formatting"
    if "citation" in lower or "polish" in lower or "heading" in lower or "title" in lower or "tldr" in lower:
        return "refinement"
    if "schema" in lower or "quality" in lower:
        return "validation"
    return "draft"


def build_progress_emitter(
    emitter: JsonlEmitter,
    controller: Optional[BridgeController] = None,
) -> Callable[[str, int], None]:
    def _progress(message: str, advance: int = 0) -> None:
        if controller is not None:
            controller.drain_pending_commands_nowait()
            if controller.cancelled:
                raise BridgeRunCancelled("Generation cancelled by TUI")
        text = str(message)
        emitter.emit(
            "task_progress",
            phase=_fuzzy_phase(text),
            agent="ContentGenerator",
            message=text,
            data={"advance": advance},
        )

    return _progress


def build_run_plan(
    *,
    style: str,
    gsc_check: bool = True,
    gsc_keywords: bool = True,
    community_mode: str = "full",
    review_gate: bool = True,
    save_local: bool = False,
    publish: bool = False,
    depth: str = "standard",
) -> List[Dict[str, Any]]:
    """Config-aware phase/task manifest for one bridge generation run.

    Mirrors the phases the bridge path actually executes (``discovery`` is an
    interactive-CLI-only phase — the bridge receives competitors via
    ``--competitors`` — so it is never included here). Task labels match the
    generator's reporter calls exactly; ids are phase-scoped slugs the TUI
    keys its todo rows on. ``depth`` does not change the task list (quick runs
    the same rows with lighter n/m totals) — it is accepted so callers can pass
    the full config and future depths may prune rows.
    """
    from .progress_reporter import task_id

    def phase_entry(phase: str, label: str, tasks: List[str]) -> Dict[str, Any]:
        return {
            "phase": phase,
            "label": label,
            "tasks": [{"id": task_id(phase, name), "label": name} for name in tasks],
        }

    plan: List[Dict[str, Any]] = [
        phase_entry("context", "Loading Context", ["Repo Context"]),
    ]

    if gsc_check or gsc_keywords:
        plan.append(phase_entry("gsc", "GSC Preflight", ["Availability", "Cannibalization", "GSC Score", "Keyword Map"]))

    research_tasks = ["SERP Analysis"]
    if style == "research":
        research_tasks.append("Research Index Search")
    if community_mode != "skip":
        research_tasks += ["Reddit Mining", "Quora Extraction", "Quality Filter"]
    plan.append(phase_entry("research", "Research", research_tasks))

    plan.append(phase_entry("draft", "Draft", ["Part 1/3", "Part 2/3", "Part 3/3"]))
    plan.append(phase_entry("formatting", "Formatting", ["Raw MDX Check", "Astro Format"]))

    refinement_tasks = ["Edit Pass", "Citation Verify", "Citation Fixes", "Editorial Audit"]
    if review_gate:
        refinement_tasks.append("Review Gate")
    refinement_tasks += ["Polish Pass", "Heading SEO", "Title SEO", "TLDR AEO"]
    plan.append(phase_entry("refinement", "Refinement", refinement_tasks))

    plan.append(phase_entry("validation", "Validation", ["Schema Refresh", "Citation Analysis"]))

    if save_local or publish:
        output_tasks = ["Save Local"]
        if publish:
            output_tasks.append("Publish")
        plan.append(phase_entry("output", "Output", output_tasks))

    return plan


class BridgeReporter:
    """PipelineReporter adapter that emits JSONL task_progress events.

    The first-class progress path for the Ink TUI. Every call drains pending
    stdin commands and raises ``BridgeRunCancelled`` on a cancel request so
    cancellation stays as responsive as the old coarse-callback path.

    Event shape per task transition::

        {"type": "task_progress", "phase": "research",
         "taskId": "research:reddit-mining", "message": "<detail>",
         "data": {"status": "running", "label": "Reddit Mining",
                  "progressCurrent": 5, "progressTotal": 10,
                  "query": "…", "elapsed": 12.3}}
    """

    renders_rich_console = False
    renders_live = True
    _AGENT = "ContentGenerator"
    _QUERY_PREVIEW_CHARS = 80

    def __init__(self, emitter: JsonlEmitter, controller: Optional[BridgeController] = None):
        self.emitter = emitter
        self.controller = controller

    def _check_cancel(self) -> None:
        if self.controller is not None:
            self.controller.drain_pending_commands_nowait()
            if self.controller.cancelled:
                raise BridgeRunCancelled("Generation cancelled by TUI")

    def _emit_task(
        self,
        phase: str,
        task: str,
        status: str,
        message: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        from .progress_reporter import task_id

        self._check_cancel()
        data: Dict[str, Any] = {"status": status, "label": task}
        if extra:
            data.update({k: v for k, v in extra.items() if v is not None})
        self.emitter.emit(
            "task_progress",
            phase=phase,
            agent=self._AGENT,
            task_id=task_id(phase, task),
            message=message or None,
            data=data,
        )

    # ── PipelineReporter protocol ────────────────────────────────────────

    def start_phase(self, phase: str, label: str, tasks: Any) -> None:
        from .progress_reporter import task_id

        self._check_cancel()
        self.emitter.emit(
            "phase_started",
            phase=phase,
            agent=self._AGENT,
            message=label,
            data={"tasks": [{"id": task_id(phase, name), "label": name} for name in tasks]},
        )

    def end_phase(self, phase: str) -> None:
        self._check_cancel()

    def start_task(self, phase: str, task: str, detail: str = "") -> None:
        self._emit_task(phase, task, "running", detail)

    def update_task(
        self,
        phase: str,
        task: str,
        status: str,
        detail: str = "",
        elapsed: Optional[float] = None,
    ) -> None:
        self._emit_task(phase, task, status, detail, {"elapsed": elapsed})

    def complete_task(
        self,
        phase: str,
        task: str,
        detail: str = "",
        elapsed: Optional[float] = None,
    ) -> None:
        self._emit_task(phase, task, "complete", detail, {"elapsed": elapsed})

    def fail_task(self, phase: str, task: str, reason: str = "Failed") -> None:
        self._emit_task(phase, task, "failed", reason)

    def skip_task(self, phase: str, task: str, reason: str = "Skipped") -> None:
        self._emit_task(phase, task, "skipped", reason)

    def set_task_progress(
        self,
        phase: str,
        task: str,
        current: int,
        total: int,
        *,
        insights: Optional[int] = None,
        query: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        preview = None
        if query:
            q = str(query)
            preview = q if len(q) <= self._QUERY_PREVIEW_CHARS else q[: self._QUERY_PREVIEW_CHARS - 1] + "…"
        self._emit_task(
            phase,
            task,
            "running",
            note or "",
            {
                "progressCurrent": current,
                "progressTotal": total,
                "insights": insights,
                "query": preview,
            },
        )

    def message(self, text: str, advance: float = 0) -> None:
        self._check_cancel()
        text = str(text)
        self.emitter.emit(
            "task_progress",
            phase=_fuzzy_phase(text),
            agent=self._AGENT,
            message=text,
            data={"advance": advance},
        )


def summarize_generation_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": result.get("success", False),
        "keyword": result.get("keyword"),
        "style": result.get("style"),
        "title": result.get("title"),
        "slug": result.get("slug"),
        "metrics": result.get("metrics", {}),
        "qualityScorecard": result.get("quality_scorecard", {}),
        "modelUsed": result.get("model_used", {}),
        "keywordContext": result.get("keyword_context", ""),
        "cost": result.get("cost", {}),
    }


async def save_local_result(
    result: Dict[str, Any],
    *,
    output_dir: Optional[str] = None,
    save_research: bool = False,
    overwrite: bool = False,
) -> Dict[str, Any]:
    from core.artifacts import save_generation_artifacts

    return save_generation_artifacts(
        result,
        output_dir=output_dir,
        save_research=save_research,
        overwrite=overwrite,
    )


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


__all__ = [
    "BRIDGE_EVENT_TYPES",
    "BridgeController",
    "BridgeEvent",
    "BridgeReporter",
    "BridgeRunCancelled",
    "GSC_DECISIONS",
    "JsonlEmitter",
    "REVIEW_DECISIONS",
    "build_progress_emitter",
    "build_run_plan",
    "detect_setup_status",
    "event_to_jsonl",
    "make_event",
    "new_run_id",
    "parse_command_line",
    "save_setup_changes",
    "save_local_result",
    "summarize_generation_result",
]
