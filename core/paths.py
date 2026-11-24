"""Central path and environment resolution for Fleo workspace & artifacts."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Optional


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
# Backwards-compatible constant for modules expecting REPO_ROOT to point at code/config root.
REPO_ROOT = PACKAGE_ROOT
_SAFE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ENV_ALLOW = re.compile(
    r"(?:FLEO|OPENROUTER|PERPLEXITY|GEMINI|INTERFAZE|GSC|INDEXNOW|ASTRO)_[A-Z0-9_]+"
    r"|GOOGLE_(?:API_KEY|SERVICE_ACCOUNT_PATH)"
    r"|MODEL_TIER|OUTPUT_DIR|GENERATION_MODEL|LIGHT_MODEL|NEBIUS_CHAT_MODEL"
    r"|DEFAULT_AUTHOR|AUTHOR_EMAIL|CONTENT_SCRUB_STRICT|LOG_LEVEL|JEV"
)
_ENV_DENY = {
    "FLEO_AUTO_GIT_PULL",
    "FLEO_BRAND_PROFILE",
    "FLEO_WORKDIR",
    "FLEO_PYTHON",
    "FLYWEEL_PYTHON",
}
_ENV_DENY_SUFFIXES = ("_BASE_URL",)


def workspace_root(repo_root: Optional[Path] = None) -> Path:
    """Active user workspace directory (where .env, .fleo/, and output/ live)."""
    if repo_root is not None:
        return Path(repo_root).expanduser().resolve()
    env_workdir = os.getenv("FLEO_WORKDIR")
    if env_workdir:
        return Path(env_workdir).expanduser().resolve()
    return PACKAGE_ROOT


def fleo_dir(repo_root: Optional[Path] = None) -> Path:
    return workspace_root(repo_root) / ".fleo"


def brand_profile_path(repo_root: Optional[Path] = None) -> Path:
    explicit = os.getenv("FLEO_BRAND_PROFILE")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return fleo_dir(repo_root) / "brand" / "profile.json"


def site_snapshot_path(repo_root: Optional[Path] = None) -> Path:
    return fleo_dir(repo_root) / "brand" / "site_snapshot.md"


def site_contract_path(repo_root: Optional[Path] = None) -> Path:
    return fleo_dir(repo_root) / "site-contract.json"


def _read_env_file(env_path: Path, loaded: Dict[str, str]) -> None:
    if env_path.is_symlink() or not env_path.exists() or not env_path.is_file():
        return
    try:
        if env_path.stat().st_size > 256 * 1024:
            return
        text = env_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise OSError(f"{env_path} is not valid UTF-8: {exc}") from exc
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if (
            not key
            or key in os.environ
            or key in _ENV_DENY
            or key.endswith(_ENV_DENY_SUFFIXES)
            or not _ENV_ALLOW.fullmatch(key)
        ):
            continue
        normalized = value.strip().strip("'\"")
        os.environ[key] = normalized
        loaded[key] = normalized


def load_repo_env(repo_root: Optional[Path] = None) -> Dict[str, str]:
    """Load workspace ``.env`` (falling back to package root ``.env``) without overriding process env."""
    loaded: Dict[str, str] = {}
    ws = workspace_root(repo_root)
    _read_env_file(ws / ".env", loaded)
    if ws != PACKAGE_ROOT:
        _read_env_file(PACKAGE_ROOT / ".env", loaded)
    return loaded


def output_root(value: Optional[str] = None, repo_root: Optional[Path] = None) -> Path:
    root = workspace_root(repo_root)
    configured = value or os.getenv("OUTPUT_DIR") or "output"
    path = Path(configured).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _configured_site_contract(repo_root: Optional[Path] = None) -> Dict[str, object]:
    sc_path = site_contract_path(repo_root)
    if sc_path.exists():
        try:
            data = json.loads(sc_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {}


def resolve_blog_content_target(
    value: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> tuple[Path, Optional[str]]:
    if value is not None:
        return Path(value).expanduser().resolve(), None
    source_env = os.environ if env is None else env
    for key in ("FLEO_BLOG_CONTENT_PATH", "ASTRO_BLOG_PATH"):
        raw = str(source_env.get(key) or "").strip()
        if raw:
            return Path(raw).expanduser().resolve(), key
    contract = _configured_site_contract(repo_root)
    contract_blog = contract.get("content_dir")
    if isinstance(contract_blog, str) and contract_blog.strip():
        return Path(contract_blog).expanduser().resolve(), None
    return (output_root(repo_root=repo_root) / "site-blog").resolve(), None


def blog_content_path(value: Optional[Path] = None, repo_root: Optional[Path] = None) -> Path:
    return resolve_blog_content_target(value=value, repo_root=repo_root)[0]


def blog_assets_path(value: Optional[Path] = None, repo_root: Optional[Path] = None) -> Path:
    if value is not None:
        return Path(value).expanduser().resolve()
    env_val = os.getenv("FLEO_BLOG_ASSETS_PATH") or os.getenv("ASTRO_ASSETS_PATH")
    if env_val:
        return Path(env_val).expanduser().resolve()
    contract = _configured_site_contract(repo_root)
    contract_assets = contract.get("assets_dir")
    if isinstance(contract_assets, str) and contract_assets.strip():
        return Path(contract_assets).expanduser().resolve()
    return (output_root(repo_root=repo_root) / "site-assets").resolve()


# Legacy aliases retained so existing tests and callers continue working.
astro_blog_path = blog_content_path
astro_assets_path = blog_assets_path


def validate_artifact_slug(slug: object) -> str:
    value = str(slug) if slug is not None else ""
    if len(value) > 100 or not _SAFE_SLUG.fullmatch(value):
        raise ValueError("Invalid artifact slug; expected lowercase words separated by hyphens")
    return value


def resolve_beneath(root: Path, *parts: str) -> Path:
    resolved_root = root.expanduser().resolve()
    target = resolved_root.joinpath(*parts).resolve()
    try:
        common = Path(os.path.commonpath([str(resolved_root), str(target)]))
    except ValueError as exc:
        raise ValueError("Artifact path escapes the selected output root") from exc
    if common != resolved_root:
        raise ValueError("Artifact path escapes the selected output root")
    return target


def atomic_write_text(
    target: Path,
    content: str,
    *,
    root: Optional[Path] = None,
    mode: int = 0o644,
) -> None:
    """Atomically write UTF-8 text without following leaf or workspace-internal symlinks."""
    raw_target = Path(target).expanduser()
    if raw_target.is_symlink():
        raise OSError(f"Refusing to write through symlink: {raw_target}")
    if root is not None:
        raw_root = Path(root).expanduser()
        try:
            rel_parts = raw_target.relative_to(raw_root).parts
        except ValueError:
            resolved_root = raw_root.resolve()
            rel_parts = raw_target.resolve().relative_to(resolved_root).parts
            cur = resolved_root
        else:
            cur = raw_root.resolve()
        for part in rel_parts:
            cur = cur / part
            if cur.is_symlink():
                raise OSError(f"Refusing to traverse symlink inside workspace: {cur}")
        write_target = cur
    else:
        if raw_target.parent.is_symlink():
            raise OSError(f"Refusing to write inside symlinked directory: {raw_target.parent}")
        write_target = raw_target

    write_target.parent.mkdir(parents=True, exist_ok=True)
    if write_target.is_symlink() or (write_target.exists() and not write_target.is_file()):
        raise OSError(f"Refusing to overwrite non-regular file: {write_target}")

    tmp_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{write_target.name}.",
            suffix=".tmp",
            dir=str(write_target.parent),
        )
        tmp_path = Path(tmp_name)
        os.chmod(tmp_path, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, write_target)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


__all__ = [
    "PACKAGE_ROOT",
    "REPO_ROOT",
    "astro_assets_path",
    "astro_blog_path",
    "atomic_write_text",
    "blog_assets_path",
    "blog_content_path",
    "brand_profile_path",
    "fleo_dir",
    "load_repo_env",
    "output_root",
    "resolve_beneath",
    "resolve_blog_content_target",
    "site_contract_path",
    "site_snapshot_path",
    "validate_artifact_slug",
    "workspace_root",
]
