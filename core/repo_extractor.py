"""Company-agnostic Repository Context Extractor.

Parses a connected website repository (Next.js, Astro, Nuxt, SvelteKit, Remix, or Markdown)
plus live `site_markdown` pages and `BrandProfile` to assemble internal links, published posts,
announcements/changelogs, and verified company facts. Never runs git pull on external repos unless
explicitly opted in via `FLEO_AUTO_GIT_PULL=1`.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .brand_profile import BrandProfile, load_brand_profile
from .site_adapters import load_site_contract
from .site_markdown import load_site_pages, page_links, page_summaries, select_company_context

logger = logging.getLogger(__name__)


class RepoContextExtractor:
    """Extract brand context, internal links, and post inventory from any connected site repo."""

    def __init__(
        self,
        repo_path: Optional[Path] = None,
        brand_profile: Optional[BrandProfile] = None,
    ):
        self.brand_profile = brand_profile or load_brand_profile()
        contract = load_site_contract()
        if repo_path is not None:
            self.repo_path: Optional[Path] = Path(repo_path).expanduser().resolve()
        elif contract.repo_root:
            self.repo_path = Path(contract.repo_root).expanduser().resolve()
        else:
            env_repo = os.getenv("FLEO_SITE_REPO_PATH")
            self.repo_path = Path(env_repo).expanduser().resolve() if env_repo else None

    def _sync_repo(self) -> None:
        """Optional fast-forward pull on a clean git checkout (only called when explicitly enabled)."""
        if self.repo_path is None:
            return
        try:
            st = subprocess.run(
                ["git", "-C", str(self.repo_path), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            if st.returncode != 0 or st.stdout.strip():
                logger.warning("Repo has local changes or is not a clean git tree; skipping pull")
                return
            subprocess.run(
                ["git", "-C", str(self.repo_path), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
        except Exception as exc:
            detail = getattr(exc, "stderr", "") or ""
            logger.warning("Repo git pull skipped (%s)", detail.splitlines()[-1][:160] if detail else exc)

    @staticmethod
    def _parse_frontmatter(raw: str) -> Dict[str, Any]:
        if not raw.startswith("---"):
            return {}
        parts = raw.split("---", 2)
        if len(parts) < 3:
            return {}
        try:
            data = yaml.safe_load(parts[1])
            return data if isinstance(data, dict) else {}
        except yaml.YAMLError:
            return {}

    @staticmethod
    def _is_unpublished_frontmatter(fm: Dict[str, Any]) -> bool:
        draft_val = fm.get("draft")
        if draft_val is True or (isinstance(draft_val, str) and draft_val.strip().lower() in {"true", "1", "yes"}):
            return True
        pub_val = fm.get("published")
        if pub_val is False or (isinstance(pub_val, str) and pub_val.strip().lower() in {"false", "0", "no"}):
            return True
        status_val = fm.get("status")
        if isinstance(status_val, str) and status_val.strip().lower() in {"draft", "unpublished", "archived"}:
            return True
        return False

    def _scan_markdown_collection(self, subdir_candidates: List[str], url_prefix: str) -> List[Dict[str, Any]]:
        if self.repo_path is None or not self.repo_path.exists():
            return []
        items: List[Dict[str, Any]] = []
        for rel in subdir_candidates:
            folder = self.repo_path / rel
            if not folder.is_dir():
                continue
            for fpath in sorted(folder.rglob("*")):
                if fpath.suffix.lower() not in {".mdx", ".md", ".svx"} or fpath.name.startswith("_"):
                    continue
                try:
                    raw = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                fm = self._parse_frontmatter(raw)
                if self._is_unpublished_frontmatter(fm):
                    continue
                slug = str(fm.get("slug") or fpath.stem).strip("/")
                title = str(fm.get("title") or slug.replace("-", " ").title()).strip()
                items.append(
                    {
                        "slug": slug,
                        "title": title,
                        "description": str(fm.get("description") or fm.get("summary") or "").strip(),
                        "url": f"{url_prefix.rstrip('/')}/{slug}",
                        "author": str(fm.get("author") or self.brand_profile.resolve_author().name),
                        "tags": fm.get("tags") if isinstance(fm.get("tags"), list) else [],
                    }
                )
            if items:
                break
        return items

    def extract_context(self) -> Dict[str, Any]:
        if os.getenv("FLEO_AUTO_GIT_PULL") == "1":
            self._sync_repo()

        site_pages = load_site_pages(self.brand_profile.site_url or None)
        brand_name = self.brand_profile.company_name
        internal_links: Dict[str, str] = page_links(site_pages, brand_name=brand_name) if site_pages else {}
        if brand_name and brand_name not in internal_links:
            internal_links[brand_name] = "/"

        for prod in self.brand_profile.products:
            if prod.name and prod.url:
                internal_links[prod.name] = prod.url

        blog_prefix = self.brand_profile.blog_base_path or "/blog"
        blog_candidates = ["content/blog", "src/content/blog", "apps/web/src/content/blog", "app/content/blog", "posts", "src/posts", "blog"]
        contract = load_site_contract()
        if contract.content_dir and self.repo_path is not None:
            cand_path = Path(contract.content_dir).expanduser()
            resolved_cand = (cand_path if cand_path.is_absolute() else (self.repo_path / cand_path)).resolve()
            try:
                rel_cand = resolved_cand.relative_to(self.repo_path).as_posix()
            except ValueError:
                rel_cand = ""
            if rel_cand and rel_cand not in blog_candidates:
                blog_candidates.insert(0, rel_cand)
        blog_posts = self._scan_markdown_collection(
            blog_candidates,
            blog_prefix,
        )
        announcements = self._scan_markdown_collection(
            ["content/announcements", "src/content/announcements", "content/changelog", "src/content/changelog"],
            "/announcements",
        )

        for post in blog_posts[:40]:
            internal_links.setdefault(post["title"], post["url"])
        for ann in announcements[:20]:
            internal_links[ann["title"]] = ann["url"]

        features = list(page_summaries(site_pages, core=False, brand_name=brand_name))
        for prod in self.brand_profile.products:
            if prod.description:
                features.append(f"{prod.name}: {prod.description}")

        key_diff = list(page_summaries(site_pages, core=True, brand_name=brand_name))
        if self.brand_profile.one_liner and self.brand_profile.one_liner not in key_diff:
            key_diff.insert(0, self.brand_profile.one_liner)

        company_md = select_company_context({"site_pages": site_pages})
        if not company_md and self.brand_profile.is_configured():
            lines = [f"## {brand_name} ({self.brand_profile.site_url or '/'})", ""]
            if self.brand_profile.one_liner:
                lines.append(self.brand_profile.one_liner)
            for prod in self.brand_profile.products:
                lines.append(f"- **{prod.name}** ({prod.url}): {prod.description} {prod.pricing_facts}".strip())
            company_md = "\n".join(lines) + "\n"

        metadata: Dict[str, Dict[str, Any]] = {}
        for anchor, dest in internal_links.items():
            low = f"{anchor} {dest}".lower()
            prio = 8 if any(k in low for k in ("pricing", "product", "feature", "integration", "doc")) else 5
            metadata[dest] = {
                "title": anchor,
                "description": anchor,
                "tags": [anchor],
                "category": "page",
                "type": "page",
                "source_type": "page",
                "priority": prio,
            }

        return {
            "timestamp": datetime.now().isoformat(),
            "source": "site-repo" if self.repo_path else "brand-profile",
            "base_url": self.brand_profile.site_url,
            "features": features,
            "key_differentiators": key_diff,
            "integrations": {},
            "pricing": {},
            "internal_links": internal_links,
            "internal_link_metadata": metadata,
            "blog_posts": blog_posts[:30],
            "announcements": announcements[:15],
            "site_pages": site_pages,
            "company_context_md": company_md,
        }
