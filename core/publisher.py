"""Multi-Framework Site Publisher — safely writes adapted posts + schema to any connected website repo."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .brand_profile import BrandProfile, load_brand_profile
from .mdx_security import assert_safe_mdx
from .paths import blog_assets_path, blog_content_path, resolve_beneath, validate_artifact_slug
from .site_adapters import FRAMEWORK_LABELS, SiteContract, adapt_article_for_target, load_site_contract

logger = logging.getLogger(__name__)


class SitePublisher:
    """Publish generated content into a connected Next.js, Astro, Nuxt, SvelteKit, Remix, or Markdown repository."""

    REQUIRED_FIELDS = ["title", "description"]

    def __init__(
        self,
        blog_path: Optional[Path] = None,
        assets_path: Optional[Path] = None,
        site_contract: Optional[SiteContract] = None,
        brand_profile: Optional[BrandProfile] = None,
    ):
        self.site_contract = site_contract or load_site_contract()
        self.brand_profile = brand_profile or load_brand_profile()
        self.blog_path = blog_content_path(blog_path)
        self.assets_path = blog_assets_path(assets_path)

    def get_existing_slugs(self) -> List[str]:
        if not self.blog_path.exists():
            return []
        ext = self.site_contract.file_extension if self.site_contract.file_extension in {".mdx", ".md", ".svx"} else ".mdx"
        return sorted(
            {
                p.stem
                for p in self.blog_path.iterdir()
                if p.is_file() and p.suffix.lower() == ext
            }
        )

    def _ensure_draft_status(self, content: str, draft: bool) -> str:
        m = re.match(r"^---\r?\n(.*?\r?\n)---(\r?\n.*|$)", content, re.DOTALL)
        if not m:
            return content
        fm_body, rest = m.group(1), m.group(2)
        try:
            parsed_fm = yaml.safe_load(fm_body)
            if isinstance(parsed_fm, dict):
                updated_fm: Dict[str, Any] = {"draft": bool(draft)}
                for k, v in parsed_fm.items():
                    if k != "draft":
                        updated_fm[k] = v
                dumped = yaml.safe_dump(updated_fm, sort_keys=False, allow_unicode=True)
                return f"---\n{dumped}---{rest}"
        except yaml.YAMLError:
            pass
        draft_str = "true" if draft else "false"
        if re.search(r"(?m)^draft\s*:.*$", fm_body):
            cleaned_lines: List[str] = []
            replaced = False
            for ln in fm_body.splitlines(keepends=True):
                if re.match(r"^draft\s*:.*$", ln):
                    if not replaced:
                        cleaned_lines.append(f"draft: {draft_str}\n")
                        replaced = True
                else:
                    cleaned_lines.append(ln)
            fm_body = "".join(cleaned_lines)
        else:
            fm_body = f"draft: {draft_str}\n{fm_body}"
        return f"---\n{fm_body}---{rest}"

    def _validate_frontmatter(self, content: str) -> Dict[str, Any]:
        errors: List[str] = []
        warnings: List[str] = []
        m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not m:
            return {"valid": False, "errors": ["Missing YAML frontmatter block"], "warnings": []}
        try:
            fm = yaml.safe_load(m.group(1))
        except yaml.YAMLError as exc:
            return {"valid": False, "errors": [f"Invalid YAML frontmatter: {exc}"], "warnings": []}
        if not isinstance(fm, dict):
            return {"valid": False, "errors": ["Frontmatter is not a YAML mapping"], "warnings": []}

        title_k = self.site_contract.title_key
        desc_k = self.site_contract.description_key
        if not str(fm.get(title_k) or fm.get("title") or "").strip():
            errors.append(f"Missing required frontmatter field: {title_k}")
        if not str(fm.get(desc_k) or fm.get("description") or "").strip():
            errors.append(f"Missing required frontmatter field: {desc_k}")
        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}

    def publish(
        self,
        content: str,
        slug: str,
        draft: bool = False,
        overwrite: bool = False,
        strict: bool = False,
    ) -> Dict[str, Any]:
        try:
            if not self.blog_path.exists():
                return {
                    "success": False,
                    "error": f"Blog path not found: {self.blog_path}",
                    "hint": "Run `fleo setup` to connect your website repository or save locally via `agent-handoff.md`.",
                }

            clean_slug = validate_artifact_slug(slug)

            # Ensure untrusted input passes security gate BEFORE any transformation
            assert_safe_mdx(content)

            # Adapt content to target framework unless raw content is a minimal test fixture
            adapted_ext = self.site_contract.file_extension or ".mdx"
            schema_json_str: Optional[str] = None
            if self.site_contract.mode == "repo":
                adapted = adapt_article_for_target(
                    raw_formatted_mdx=content,
                    slug=clean_slug,
                    keyword=clean_slug.replace("-", " "),
                    brand_profile=self.brand_profile,
                    site_contract=self.site_contract,
                )
                out_content = adapted["primary_content"]
                adapted_ext = adapted["primary_extension"]
                schema_json_str = adapted["schema_json_str"]
            else:
                out_content = content
                adapted_ext = ".mdx"

            out_content = self._ensure_draft_status(out_content, draft)
            validation = self._validate_frontmatter(out_content)
            if not validation["valid"] and strict:
                return {
                    "success": False,
                    "error": f"Frontmatter validation failed: {'; '.join(validation['errors'])}",
                    "validation": validation,
                }

            # Final trust boundary check on exact bytes written to disk
            assert_safe_mdx(out_content)

            target_path = resolve_beneath(self.blog_path, f"{clean_slug}{adapted_ext}")
            if target_path.exists() and not overwrite:
                return {
                    "success": False,
                    "conflict": True,
                    "error": f"File already exists: {target_path}",
                    "existing_slug": clean_slug,
                    "hint": "Use overwrite=True to replace, or choose a different slug.",
                }

            schema_out_path: Optional[Path] = (
                resolve_beneath(self.blog_path, f"{clean_slug}.schema.json")
                if (schema_json_str and not self.site_contract.layout_emits_jsonld)
                else None
            )
            if not overwrite and schema_out_path is not None and schema_out_path.exists():
                return {
                    "success": False,
                    "conflict": True,
                    "error": f"File already exists: {schema_out_path}",
                    "existing_slug": clean_slug,
                    "hint": "Use overwrite=True to replace, or choose a different slug.",
                }

            import os
            import tempfile

            prior_target = target_path.read_bytes() if (overwrite and target_path.exists()) else None
            prior_schema = schema_out_path.read_bytes() if (overwrite and schema_out_path and schema_out_path.exists()) else None
            staged_files: List[Path] = []
            committed_target = False
            committed_schema = False
            try:
                fd_t, tmp_t_name = tempfile.mkstemp(prefix=f".{clean_slug}.", suffix=".tmp", dir=str(self.blog_path))
                tmp_t = Path(tmp_t_name)
                staged_files.append(tmp_t)
                with os.fdopen(fd_t, "w", encoding="utf-8") as stream:
                    stream.write(out_content)

                tmp_s: Optional[Path] = None
                if schema_out_path is not None and schema_json_str is not None:
                    fd_s, tmp_s_name = tempfile.mkstemp(prefix=f".{clean_slug}.schema.", suffix=".tmp", dir=str(self.blog_path))
                    tmp_s = Path(tmp_s_name)
                    staged_files.append(tmp_s)
                    with os.fdopen(fd_s, "w", encoding="utf-8") as stream:
                        stream.write(schema_json_str + "\n")

                if overwrite:
                    os.replace(tmp_t, target_path)
                    committed_target = True
                else:
                    try:
                        with target_path.open("x", encoding="utf-8") as stream:
                            committed_target = True
                            stream.write(out_content)
                    except FileExistsError:
                        return {
                            "success": False,
                            "conflict": True,
                            "error": f"File already exists: {target_path}",
                            "existing_slug": clean_slug,
                            "hint": "Use overwrite=True to replace, or choose a different slug.",
                        }
                    finally:
                        tmp_t.unlink(missing_ok=True)

                if schema_out_path is not None and tmp_s is not None:
                    if overwrite:
                        os.replace(tmp_s, schema_out_path)
                        committed_schema = True
                    else:
                        try:
                            with schema_out_path.open("x", encoding="utf-8") as stream:
                                committed_schema = True
                                stream.write((schema_json_str or "") + "\n")
                        except FileExistsError:
                            if committed_target and prior_target is None:
                                target_path.unlink(missing_ok=True)
                                committed_target = False
                            return {
                                "success": False,
                                "conflict": True,
                                "error": f"File already exists: {schema_out_path}",
                                "existing_slug": clean_slug,
                                "hint": "Use overwrite=True to replace, or choose a different slug.",
                            }
                        finally:
                            tmp_s.unlink(missing_ok=True)
            except Exception:
                try:
                    if committed_target:
                        if prior_target is None:
                            target_path.unlink(missing_ok=True)
                        else:
                            target_path.write_bytes(prior_target)
                    if committed_schema and schema_out_path is not None:
                        if prior_schema is None:
                            schema_out_path.unlink(missing_ok=True)
                        else:
                            schema_out_path.write_bytes(prior_schema)
                finally:
                    for sf in staged_files:
                        sf.unlink(missing_ok=True)
                raise
            finally:
                for sf in staged_files:
                    sf.unlink(missing_ok=True)

            fw_label = FRAMEWORK_LABELS.get(self.site_contract.framework, self.site_contract.framework)
            logger.info("Published (%s) to: %s", fw_label, target_path)
            return {
                "success": True,
                "path": str(target_path),
                "schema_path": str(schema_out_path) if schema_out_path else None,
                "framework": self.site_contract.framework,
                "slug": clean_slug,
                "draft": draft,
                "validation": validation,
                "hero_image_path": str(self.assets_path / f"{clean_slug}-hero.webp"),
                "hero_image_needed": not (self.assets_path / f"{clean_slug}-hero.webp").exists(),
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "error": f"Publish failed: {exc}"}

    @staticmethod
    def verify_rendered_html(
        html_text: str,
        slug: str,
        *,
        brand_profile: Optional[BrandProfile] = None,
    ) -> Dict[str, Any]:
        """Verify rendered HTML SEO/AEO invariants for any brand's site."""
        from bs4 import BeautifulSoup

        profile = brand_profile or load_brand_profile()
        errors: List[str] = []
        soup = BeautifulSoup(html_text or "", "html.parser")

        title_tag = soup.find("title")
        if not title_tag or not title_tag.get_text().strip():
            errors.append("Missing <title> tag in rendered HTML")
        else:
            title_text = re.sub(r"\s+", " ", title_tag.get_text()).strip()
            if not (30 <= len(title_text) <= 70):
                errors.append(f"Rendered <title> length is {len(title_text)} chars (target 30-70): {title_text!r}")

        desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        if not desc_tag or not str(desc_tag.get("content") or "").strip():
            errors.append("Missing <meta name='description'> in rendered HTML")

        if profile.site_url:
            expected_canon = profile.canonical_blog_url(slug)
            canon_tag = soup.find("link", attrs={"rel": lambda v: bool(v) and "canonical" in (v if isinstance(v, list) else [str(v)])})
            canon_href = str(canon_tag.get("href") or "").strip() if canon_tag else ""
            if not canon_href:
                errors.append("Missing <link rel='canonical'> in rendered HTML")
            elif canon_href.rstrip("/") != expected_canon.rstrip("/"):
                errors.append(f"Canonical mismatch: expected {expected_canon!r}, got {canon_href!r}")

        for img_tag in soup.find_all("img"):
            if str(img_tag.get("aria-hidden") or "").lower() == "true":
                continue
            if not str(img_tag.get("alt") or "").strip():
                errors.append(f"<img> missing non-empty alt attribute: {str(img_tag)[:80]}")

        return {"valid": len(errors) == 0, "errors": errors}


# Backwards-compatible alias for existing tests and imports
AstroPublisher = SitePublisher


def publish_to_site(
    content: str,
    slug: str,
    draft: bool = False,
    overwrite: bool = False,
) -> Dict[str, Any]:
    return SitePublisher().publish(content, slug, draft=draft, overwrite=overwrite)


publish_to_astro = publish_to_site
