"""Company-agnostic JSON-LD Schema Markup Generator (`@graph`: Organization, Author, BlogPosting, BreadcrumbList, FAQPage)."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .brand_profile import BrandProfile, load_brand_profile

logger = logging.getLogger(__name__)


class SchemaGenerator:
    """Generate Schema.org compliant JSON-LD for blog & research content for any brand."""

    def __init__(
        self,
        author_profiles_path: Optional[Path] = None,
        brand_profile: Optional[BrandProfile] = None,
    ):
        self.brand_profile = brand_profile or load_brand_profile()
        self.author_profiles = self._load_author_profiles(author_profiles_path)

    def _load_author_profiles(self, path: Optional[Path]) -> Dict[str, Any]:
        if path is not None and Path(path).exists():
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed reading author_profiles at %s: %s", path, exc)
        profiles: Dict[str, Any] = {}
        for author in self.brand_profile.authors:
            profiles[author.id] = {"name": author.name, "schema": author.to_jsonld(self.brand_profile.site_url)}
            profiles[author.name] = {"name": author.name, "schema": author.to_jsonld(self.brand_profile.site_url)}
        default_author = self.brand_profile.resolve_author()
        profiles["_default_fallback"] = {
            "name": default_author.name,
            "schema": default_author.to_jsonld(self.brand_profile.site_url),
        }
        return profiles

    def _site_origin(self, canonical_url: str = "") -> str:
        if self.brand_profile.site_url:
            return self.brand_profile.site_url.rstrip("/")
        if canonical_url and canonical_url.startswith(("http://", "https://")):
            p = urlsplit(canonical_url)
            return f"{p.scheme}://{p.netloc}"
        return "https://example.com"

    def _publisher_entity(self, origin: str) -> Dict[str, Any]:
        org_name = self.brand_profile.company_name or "Publisher"
        org: Dict[str, Any] = {
            "@type": "Organization",
            "@id": f"{origin}/#organization",
            "name": org_name,
            "url": origin,
        }
        if self.brand_profile.logo_url:
            org["logo"] = {"@type": "ImageObject", "url": self.brand_profile.logo_url}
        if self.brand_profile.organization_same_as:
            org["sameAs"] = list(self.brand_profile.organization_same_as)
        return org

    def _get_author_schema(self, author_name: str, origin: str) -> Dict[str, Any]:
        if author_name in self.author_profiles:
            return self.author_profiles[author_name].get("schema", {})
        resolved = self.brand_profile.resolve_author(author_name)
        if resolved.name != "Editorial Team" or not author_name:
            return resolved.to_jsonld(origin)
        return {"@type": "Person", "name": author_name}

    def generate_blog_posting_schema(
        self,
        frontmatter: Dict[str, Any],
        content: str,
        canonical_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = canonical_url or frontmatter.get("canonical", "")
        origin = self._site_origin(url)

        title = (frontmatter.get("seo") or {}).get("title") or frontmatter.get("title", "")
        description = (frontmatter.get("seo") or {}).get("description") or frontmatter.get("description", "")

        image_data = frontmatter.get("image", {})
        image_alt = title
        if isinstance(image_data, dict):
            image_src = image_data.get("src", "")
            image_alt = image_data.get("alt") or title
        elif isinstance(image_data, str):
            image_src = image_data
        else:
            image_src = ""
        if not image_src:
            image_src = (frontmatter.get("seo") or {}).get("ogImage", "")

        if image_src and not image_src.startswith(("http://", "https://")):
            image_src = f"{origin}/{image_src.lstrip('/')}"

        published = str(frontmatter.get("publishDate") or datetime.now().strftime("%Y-%m-%d"))
        modified = str(frontmatter.get("updatedDate") or published)
        author_name = str(frontmatter.get("author") or self.brand_profile.resolve_author().name)
        author_schema = self._get_author_schema(author_name, origin)
        word_count = len((content or "").split())
        category = str(frontmatter.get("category") or self.brand_profile.category or "General")

        schema: Dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "BlogPosting",
            "@id": f"{url}#article" if url else None,
            "headline": str(title)[:110],
            "description": description,
            "image": {"@type": "ImageObject", "url": image_src, "alt": image_alt} if image_src else None,
            "datePublished": published,
            "dateModified": modified,
            "author": author_schema,
            "publisher": self._publisher_entity(origin),
            "mainEntityOfPage": {"@type": "WebPage", "@id": url} if url else None,
            "wordCount": word_count,
            "articleSection": category,
            "inLanguage": self.brand_profile.spelling_locale,
        }
        return {k: v for k, v in schema.items() if v is not None}

    def generate_breadcrumb_schema(self, title: str, canonical_url: str) -> Optional[Dict[str, Any]]:
        if not canonical_url:
            return None
        origin = self._site_origin(canonical_url)
        blog_prefix = "/" + (self.brand_profile.blog_base_path or "/blog").strip("/")
        return {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "@id": f"{canonical_url}#breadcrumb",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Home", "item": origin},
                {"@type": "ListItem", "position": 2, "name": "Blog", "item": f"{origin}{blog_prefix}"},
                {"@type": "ListItem", "position": 3, "name": title, "item": canonical_url},
            ],
        }

    def generate_faq_page_schema(
        self,
        faq_list: List[Dict[str, str]],
        canonical_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        valid = [f for f in (faq_list or []) if f.get("question") and f.get("answer")]
        if not valid:
            return None
        url = canonical_url or ""
        schema: Dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "@id": f"{url}#faqpage" if url else None,
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": faq["question"],
                    "acceptedAnswer": {"@type": "Answer", "text": faq["answer"]},
                }
                for faq in valid[:15]
            ],
        }
        return {k: v for k, v in schema.items() if v is not None}

    def generate_claim_review_schema(
        self,
        claims: List[Dict[str, Any]],
        canonical_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not claims:
            return []
        origin = self._site_origin(canonical_url or "")
        reviewer_name = f"{self.brand_profile.company_name} Research" if self.brand_profile.company_name else "Editorial Research"
        confidence_map = {"RESEARCH": "True", "INDUSTRY": "Mostly True", "OBSERVATIONAL": "Unverified"}
        schemas: List[Dict[str, Any]] = []
        for i, claim_data in enumerate(claims[:10], 1):
            claim_text = claim_data.get("claim", "")
            source = claim_data.get("source", "research")
            confidence = claim_data.get("confidence", "OBSERVATIONAL")
            date_pub = claim_data.get("date_published", datetime.now().isoformat())
            rating = confidence_map.get(confidence, "Unverified")
            item = {
                "@context": "https://schema.org",
                "@type": "ClaimReview",
                "@id": f"{canonical_url}#claim-{i}" if canonical_url else None,
                "datePublished": date_pub,
                "url": canonical_url,
                "claimReviewed": claim_text,
                "itemReviewed": {
                    "@type": "Claim",
                    "author": {"@type": "Organization", "name": source},
                    "datePublished": date_pub,
                    "text": claim_text,
                },
                "reviewRating": {
                    "@type": "Rating",
                    "ratingValue": rating,
                    "bestRating": "True",
                    "worstRating": "False",
                    "alternateName": confidence,
                },
                "author": {"@type": "Organization", "name": reviewer_name, "url": origin},
            }
            schemas.append({k: v for k, v in item.items() if v is not None})
        return schemas

    def generate_how_to_schema(
        self,
        title: str,
        description: str,
        steps: List[Dict[str, str]],
        canonical_url: Optional[str] = None,
        total_time: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not steps or len(steps) < 3:
            return None
        schema: Dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "HowTo",
            "@id": f"{canonical_url}#howto" if canonical_url else None,
            "name": title,
            "description": description,
            "step": [
                {
                    k: v
                    for k, v in {
                        "@type": "HowToStep",
                        "position": i,
                        "name": step["name"],
                        "text": step["text"],
                        "image": step.get("image"),
                    }.items()
                    if v is not None
                }
                for i, step in enumerate(steps, 1)
            ],
        }
        if total_time:
            schema["totalTime"] = total_time
        return {k: v for k, v in schema.items() if v is not None}

    def generate_complete_schema_markup(
        self,
        frontmatter: Dict[str, Any],
        content: str,
        verified_claims: Optional[List[Dict[str, Any]]] = None,
        guide_steps: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        canonical_url = frontmatter.get("canonical", "")
        origin = self._site_origin(canonical_url)
        graph: List[Dict[str, Any]] = []

        org = self._publisher_entity(origin)
        graph.append(org)

        blog_schema = self.generate_blog_posting_schema(frontmatter, content, canonical_url)
        blog_schema.pop("@context", None)
        graph.append(blog_schema)

        breadcrumb = self.generate_breadcrumb_schema(str(frontmatter.get("title") or "Article"), canonical_url)
        if breadcrumb:
            breadcrumb.pop("@context", None)
            graph.append(breadcrumb)

        faq_list = frontmatter.get("faq") or []
        faq_schema = self.generate_faq_page_schema(faq_list, canonical_url)
        if faq_schema:
            faq_schema.pop("@context", None)
            graph.append(faq_schema)

        if verified_claims:
            for cs in self.generate_claim_review_schema(verified_claims, canonical_url):
                cs.pop("@context", None)
                graph.append(cs)

        if guide_steps:
            howto = self.generate_how_to_schema(
                str(frontmatter.get("title", "")),
                str(frontmatter.get("description", "")),
                guide_steps,
                canonical_url,
            )
            if howto:
                howto.pop("@context", None)
                graph.append(howto)

        return {"@context": "https://schema.org", "@graph": graph}

    def generate_combined_graph(
        self,
        frontmatter: Dict[str, Any],
        content: str,
        canonical_url: str = "",
        faq_entries: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        fm = dict(frontmatter or {})
        if canonical_url:
            fm["canonical"] = canonical_url
        if faq_entries is not None:
            fm["faq"] = faq_entries
        return self.generate_complete_schema_markup(fm, content)
