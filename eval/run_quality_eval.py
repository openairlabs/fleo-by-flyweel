#!/usr/bin/env python3
"""Output-quality regression harness — "assess output qual from changes".

Scores one or many ``final.mdx`` articles with the deterministic AI-SEO
scorecard (``ContentValidator.build_scorecard``), optionally folding in the
LLM judge (``core.quality_judge``), then diffs each result against a committed
baseline and prints a per-dimension delta table. Exits non-zero when any
article's composite regresses beyond the configured threshold — the signal a
pipeline change made the *output* worse.

Usage:
    # Score the committed golden corpus, diff vs baselines
    python eval/run_quality_eval.py

    # Score specific files / a directory
    python eval/run_quality_eval.py path/to/final.mdx output/generations/

    # Snapshot current scores as the new baseline (after an intended improvement)
    python eval/run_quality_eval.py --update-baseline

    # Add the subjective LLM-judge dimensions (needs OPENROUTER_API_KEY)
    python eval/run_quality_eval.py --judge

    # CI mode: deterministic only, fail the build on any regression
    python eval/run_quality_eval.py --ci

Report/baseline JSON shape:
    {keyword, style, captured_at (ISO 8601), composite, deterministic_overall,
     dimensions: {<name>: {score, max, kind, rationale?}}}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load the repo .env so --judge can reach OPENROUTER_API_KEY when run standalone.
from core.paths import load_repo_env
load_repo_env(ROOT)

from core.content_validator import ContentValidator  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
GOLDEN_DIR = EVAL_DIR / "golden"
BASELINE_DIR = EVAL_DIR / "baselines"
REPORT_DIR = EVAL_DIR / "reports"


def split_frontmatter(text: str) -> Dict[str, Any]:
    """Parse YAML frontmatter into a dict (best-effort, never raises)."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1])
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def has_schema(text: str) -> bool:
    return ('application/ld+json' in text) or ('"@context"' in text)


def collect_mdx(paths: List[str]) -> List[Path]:
    """Resolve CLI paths (files or dirs) to a sorted list of .mdx files."""
    files: List[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files.extend(sorted(path.rglob("*.mdx")))
        elif path.is_file():
            files.append(path)
        else:
            print(f"⚠️  skipping missing path: {p}")
    # De-dup while preserving order.
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


async def _maybe_judge(content: str, keyword: str) -> Optional[Dict[str, Any]]:
    """Run the LLM judge through a SmartAIRouter, or None on failure."""
    from core.ai_router import SmartAIRouter
    from core.quality_judge import judge_quality
    try:
        async with SmartAIRouter() as router:
            return await judge_quality(content, keyword, router)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  judge unavailable ({str(e).splitlines()[0][:120]}); scoring deterministic only")
        return None


def _article_identity(path: Path, fm: Dict[str, Any]) -> str:
    stem = path.stem
    if stem not in {"final", "raw", "article", "index"}:
        return stem
    fm_slug = re.sub(r"[^a-z0-9-]+", "-", str(fm.get("slug") or "").lower()).strip("-")
    if fm_slug:
        return fm_slug
    parent_name = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", path.parent.name)
    cleaned_parent = re.sub(r"[^a-z0-9_-]+", "-", parent_name.lower()).strip("-")
    if cleaned_parent and cleaned_parent not in {"generations", "output", "golden"}:
        return cleaned_parent
    try:
        rel = path.resolve().relative_to(ROOT.resolve()).with_suffix("").as_posix()
    except ValueError:
        rel = path.with_suffix("").as_posix()
    return re.sub(r"[^a-z0-9_-]+", "__", rel.lower()).strip("_") or stem


def score_one(path: Path, validator: ContentValidator, judge: bool) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    fm = split_frontmatter(text)
    judge_result = None
    if judge:
        keyword = fm.get("primaryKeyword") or fm.get("keyword") or ""
        judge_result = asyncio.run(_maybe_judge(text, keyword))
    card = validator.build_scorecard(
        text, fm, has_schema=has_schema(text), judge=judge_result,
    )
    card["slug"] = _article_identity(path, fm)
    card["captured_at"] = datetime.now(timezone.utc).isoformat()
    return card


def load_baseline(slug: str) -> Optional[Dict[str, Any]]:
    bp = BASELINE_DIR / f"{slug}.json"
    if bp.exists():
        try:
            return json.loads(bp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def print_delta_table(card: Dict[str, Any], baseline: Optional[Dict[str, Any]]) -> int:
    """Print a per-dimension comparison. Returns regression count for this file."""
    slug = card["slug"]
    base_dims = (baseline or {}).get("dimensions", {})
    print(f"\n── {slug}  [{card.get('style') or '?'}]  composite={card['composite']}"
          + (f"  (baseline {baseline['composite']}, Δ{card['composite'] - baseline['composite']:+d})"
             if baseline else "  (no baseline)"))
    print(f"   {'dimension':24} {'score':>6} {'base':>6} {'Δ':>5}  kind")
    regressed_dims = 0
    for name, dim in card["dimensions"].items():
        score = dim.get("score")
        base = base_dims.get(name, {}).get("score")
        delta = ""
        if isinstance(score, (int, float)) and isinstance(base, (int, float)):
            d = score - base
            delta = f"{d:+d}" if isinstance(d, int) else f"{d:+.0f}"
            if d < 0:
                regressed_dims += 1
        s = "null" if score is None else str(score)
        b = "—" if base is None else str(base)
        print(f"   {name:24} {s:>6} {b:>6} {delta:>5}  {dim.get('kind','')}")
    return regressed_dims


def main() -> int:
    ap = argparse.ArgumentParser(description="AI-SEO output-quality regression harness")
    ap.add_argument("paths", nargs="*", help="MDX files or dirs (default: eval/golden/)")
    ap.add_argument("--judge", action="store_true", help="add LLM-judge subjective dimensions")
    ap.add_argument("--update-baseline", action="store_true", help="snapshot current scores as baseline")
    ap.add_argument("--ci", action="store_true", help="CI mode: fail build on composite regression")
    ap.add_argument("--threshold", type=int, default=None, help="composite drop that counts as regression")
    args = ap.parse_args()

    validator = ContentValidator()
    threshold = args.threshold
    if threshold is None:
        rubric = getattr(validator, "quality_rubric", {}) or {}
        threshold = rubric.get("regression_threshold", 3)

    paths = args.paths or [str(GOLDEN_DIR)]
    files = collect_mdx(paths)
    if not files:
        print(f"No .mdx files found under: {', '.join(paths)}")
        print("Seed eval/golden/ with reference final.mdx files first.")
        return 1

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    if args.update_baseline:
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Scoring {len(files)} article(s) | threshold={threshold} | "
          f"judge={'on' if args.judge else 'off'} | mode={'CI' if args.ci else 'local'}")

    regressions: List[str] = []
    for path in files:
        card = score_one(path, validator, args.judge)
        (REPORT_DIR / f"{card['slug']}.json").write_text(
            json.dumps(card, indent=2), encoding="utf-8")

        baseline = load_baseline(card["slug"])
        print_delta_table(card, baseline)

        if args.update_baseline:
            (BASELINE_DIR / f"{card['slug']}.json").write_text(
                json.dumps(card, indent=2), encoding="utf-8")
            print(f"   ✓ baseline updated")
        elif baseline is not None:
            drop = baseline["composite"] - card["composite"]
            if drop > threshold:
                regressions.append(f"{card['slug']}: composite {baseline['composite']} → "
                                   f"{card['composite']} (−{drop}, threshold {threshold})")
        elif args.ci:
            msg = f"{card['slug']}: missing or invalid baseline JSON — run --update-baseline and commit it"
            print(f"   ⚠️  {msg}")
            regressions.append(msg)

    print("\n" + "=" * 60)
    if args.update_baseline:
        print(f"✅ Baselines updated for {len(files)} article(s) in {BASELINE_DIR}")
        return 0
    if regressions:
        print(f"❌ QUALITY REGRESSION ({len(regressions)}):")
        for r in regressions:
            print(f"   - {r}")
        return 1
    print("✅ No quality regressions beyond threshold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
