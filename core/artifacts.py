"""Safe filesystem sink for generated article bundles, schema JSON-LD, and Agent Handoff packets."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .brand_profile import load_brand_profile
from .mdx_security import assert_safe_mdx
from .paths import output_root, resolve_beneath, validate_artifact_slug
from .site_adapters import adapt_article_for_target, load_site_contract


def save_generation_artifacts(
    result: Dict[str, Any],
    *,
    output_dir: Optional[str] = None,
    save_research: bool = False,
    overwrite: bool = False,
) -> Dict[str, str]:
    """Validate and write one generation bundle (framework file + `agent-handoff.md` + `schema.json` + `metadata.json`)."""
    slug = validate_artifact_slug(result.get("slug"))
    content = assert_safe_mdx(str(result.get("content", "")))
    raw_content = str(result.get("raw_content", ""))
    if raw_content:
        raw_content = assert_safe_mdx(raw_content)

    root = output_root(output_dir)
    today = datetime.now().strftime("%Y-%m-%d")
    generation_dir = resolve_beneath(root, "generations", f"{today}_{slug}")
    existed_before = generation_dir.exists()
    if existed_before and not overwrite:
        raise FileExistsError(f"Artifact conflict for slug {slug}: {generation_dir}")

    profile = load_brand_profile()
    contract = load_site_contract()
    adapted = adapt_article_for_target(
        raw_formatted_mdx=content,
        slug=slug,
        keyword=str(result.get("keyword") or slug.replace("-", " ")),
        brand_profile=profile,
        site_contract=contract,
        quality_scorecard=result.get("quality_scorecard"),
    )

    adapted_primary = assert_safe_mdx(adapted["primary_content"])
    ext = adapted["primary_extension"]

    generations_parent = resolve_beneath(root, "generations")
    generations_parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix=f".{today}_{slug}.", dir=str(generations_parent)))
    try:
        # Always write `final.mdx` as the canonical primary path expected by callers,
        # plus `adapted<ext>` when targeting pure `.md` / `.svx` stacks.
        final_path = resolve_beneath(generation_dir, "final.mdx")
        resolve_beneath(stage_dir, "final.mdx").write_text(
            adapted_primary if ext == ".mdx" else content, encoding="utf-8"
        )

        extra_written: Dict[str, str] = {}
        if ext != ".mdx":
            adapted_file = resolve_beneath(generation_dir, f"article{ext}")
            resolve_beneath(stage_dir, f"article{ext}").write_text(adapted_primary, encoding="utf-8")
            extra_written["adapted"] = str(adapted_file)

        raw_path = resolve_beneath(generation_dir, "raw.mdx")
        if raw_content:
            resolve_beneath(stage_dir, "raw.mdx").write_text(raw_content, encoding="utf-8")

        handoff_path = resolve_beneath(generation_dir, "agent-handoff.md")
        resolve_beneath(stage_dir, "agent-handoff.md").write_text(adapted["agent_handoff_md"], encoding="utf-8")

        schema_path = resolve_beneath(generation_dir, "schema.json")
        resolve_beneath(stage_dir, "schema.json").write_text(adapted["schema_json_str"] + "\n", encoding="utf-8")

        for filename, file_body in (adapted.get("extra_files") or {}).items():
            final_ep = resolve_beneath(generation_dir, filename)
            stage_ep = resolve_beneath(stage_dir, filename)
            stage_ep.parent.mkdir(parents=True, exist_ok=True)
            stage_ep.write_text(file_body, encoding="utf-8")
            extra_written[filename] = str(final_ep)

        metadata_path = resolve_beneath(generation_dir, "metadata.json")
        metadata: Dict[str, Any] = {
            "keyword": result.get("keyword", ""),
            "style": result.get("style", ""),
            "framework": adapted["framework"],
            "brand": profile.company_name or "unbranded",
            "generated_at": datetime.now().isoformat(),
            "slug": slug,
            "title": result.get("title", ""),
            "quality_scorecard": result.get("quality_scorecard", {}),
            "model_used": result.get("model_used", {}),
            "cost": result.get("cost", {}),
            "editorial_audit": result.get("editorial_audit"),
            "keyword_context": result.get("keyword_context", ""),
            "agent_handoff": str(handoff_path),
            "schema_json": str(schema_path),
        }
        if save_research:
            metadata["research_data"] = result.get("research_data", {})
        resolve_beneath(stage_dir, "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        if generation_dir.exists():
            if not overwrite:
                raise FileExistsError(f"Artifact conflict for slug {slug}: {generation_dir}")
            backup_dir = generations_parent / f".bak_{today}_{slug}_{uuid.uuid4().hex[:8]}"
            os.replace(generation_dir, backup_dir)
            try:
                os.replace(stage_dir, generation_dir)
            except Exception:
                if backup_dir.exists() and not generation_dir.exists():
                    os.replace(backup_dir, generation_dir)
                raise
            shutil.rmtree(backup_dir, ignore_errors=True)
        else:
            os.replace(stage_dir, generation_dir)
    finally:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)

    out_paths = {
        "directory": str(generation_dir),
        "final": str(final_path),
        "agentHandoff": str(handoff_path),
        "schema": str(schema_path),
        "metadata": str(metadata_path),
    }
    if raw_content:
        out_paths["raw"] = str(raw_path)
    out_paths.update(extra_written)
    return out_paths


__all__ = ["save_generation_artifacts"]
