"""Consolidated unit tests for security, paths, artifact sinks, publisher sinks, and runtime requirements."""
import os
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from core.artifacts import save_generation_artifacts
from core.generator import ContentGenerator
from core.mdx_security import (
    MdxSecurityError,
    assert_safe_mdx,
    escape_markdown_link_text,
    sanitize_content_url,
)
from core.paths import load_repo_env
from core.publisher import AstroPublisher

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Paths & Central .env Precedence
# ---------------------------------------------------------------------------

def test_load_repo_env_preserves_process_precedence(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FLEO_EXISTING_TEST_VAR", "process")
    monkeypatch.delenv("FLEO_NEW_TEST_VAR", raising=False)
    (tmp_path / ".env").write_text(
        "FLEO_EXISTING_TEST_VAR=file\nFLEO_NEW_TEST_VAR='quoted value'\n",
        encoding="utf-8",
    )

    loaded = load_repo_env(tmp_path)

    assert os.environ["FLEO_EXISTING_TEST_VAR"] == "process"
    assert os.environ["FLEO_NEW_TEST_VAR"] == "quoted value"
    assert loaded == {"FLEO_NEW_TEST_VAR": "quoted value"}


def test_load_repo_env_ignores_dangerous_and_non_fleo_keys(tmp_path, monkeypatch) -> None:
    blocked = (
        "HTTPS_PROXY",
        "SSL_CERT_FILE",
        "HTTPLIB2_CA_CERTS",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "FLEO_AUTO_GIT_PULL",
        "FLEO_BRAND_PROFILE",
        "FLEO_WORKDIR",
        "FLEO_PYTHON",
        "FLYWEEL_PYTHON",
        "FLEO_DRAFT_BASE_URL",
        "FLEO_RESEARCH_BASE_URL",
        "FLEO_LIGHT_BASE_URL",
    )
    for k in blocked:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "\n".join([f"{k}=https://evil.example" for k in blocked] + ["OPENROUTER_API_KEY=sk-or-v1-test"]) + "\n",
        encoding="utf-8",
    )
    loaded = load_repo_env(tmp_path)
    assert loaded == {"OPENROUTER_API_KEY": "sk-or-v1-test"}
    for k in blocked:
        assert k not in os.environ


def test_workspace_writes_refuse_leaf_and_directory_symlinks(tmp_path, monkeypatch) -> None:
    from core.brand_profile import BrandProfile, save_brand_profile
    from core.site_adapters import SiteContract, save_site_contract
    import core.site_markdown as sm

    outside_victim = tmp_path / "victim.txt"
    outside_victim.write_text("UNTOUCHED", encoding="utf-8")

    ws = tmp_path / "repo"
    brand_dir = ws / ".fleo" / "brand"
    brand_dir.mkdir(parents=True)
    (brand_dir / "site_snapshot.md").symlink_to(outside_victim)
    (brand_dir / "profile.json").symlink_to(outside_victim)
    (ws / ".fleo" / "site-contract.json").symlink_to(outside_victim)

    monkeypatch.setenv("FLEO_WORKDIR", str(ws))
    monkeypatch.delenv("FLEO_BRAND_PROFILE", raising=False)
    monkeypatch.setattr(sm, "SNAPSHOT_PATH", None)

    sm._write_snapshot([{"markdown": "OVERWRITTEN_BY_CRAWL"}])
    assert outside_victim.read_text(encoding="utf-8") == "UNTOUCHED"

    with pytest.raises(OSError, match="symlink"):
        save_brand_profile(BrandProfile(company_name="Acme", site_url="https://acme.dev"), repo_root=ws)
    with pytest.raises(OSError, match="symlink"):
        save_site_contract(SiteContract(), repo_root=ws)
    assert outside_victim.read_text(encoding="utf-8") == "UNTOUCHED"


# ---------------------------------------------------------------------------
# Runtime Requirements Compatibility
# ---------------------------------------------------------------------------

def _active_requirements(package: str, python_version: str) -> list[Requirement]:
    environment = default_environment()
    environment["python_version"] = python_version
    active: list[Requirement] = []

    for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        requirement_text = line.split("#", 1)[0].strip()
        if not requirement_text:
            continue
        requirement = Requirement(requirement_text)
        if canonicalize_name(requirement.name) != canonicalize_name(package):
            continue
        if requirement.marker is None or requirement.marker.evaluate(environment):
            active.append(requirement)

    return active


@pytest.mark.parametrize("python_version", ["3.9", "3.12"])
def test_python_dotenv_is_not_a_runtime_dependency(python_version: str) -> None:
    assert _active_requirements("python-dotenv", python_version) == []
    assert _active_requirements("pytest", python_version) == []
    assert _active_requirements("pytest-asyncio", python_version) == []
    # Every shipped runtime requirement is bounded (< or ==)
    for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        req_text = line.split("#", 1)[0].strip()
        if not req_text:
            continue
        req = Requirement(req_text)
        assert any(spec.operator in {"==", "<", "<="} for spec in req.specifier), req_text


def test_runtime_uses_the_central_environment_loader() -> None:
    runtime_files = [
        "generate.py",
        "core/research.py",
        "core/gsc_analyzer.py",
        "core/indexnow.py",
        "eval/run_quality_eval.py",
    ]
    for relative_path in runtime_files:
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "from dotenv import" not in source, relative_path
    assert "parent.parent" not in (REPO_ROOT / "generate.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Artifact Filesystem Sink
# ---------------------------------------------------------------------------

SAFE_ARTIFACT_MDX = """---
title: Safe article
description: Safe description
---

# Safe article

Plain text.
"""


def _artifact_result(slug: str = "safe-article", content: str = SAFE_ARTIFACT_MDX):
    return {
        "slug": slug,
        "content": content,
        "raw_content": SAFE_ARTIFACT_MDX,
        "keyword": "safe",
        "style": "standard",
        "title": "Safe article",
    }


def test_artifact_sink_rejects_traversal_slug(tmp_path: Path):
    with pytest.raises(ValueError, match="Invalid artifact slug"):
        save_generation_artifacts(_artifact_result("../escape"), output_dir=str(tmp_path))
    assert not (tmp_path.parent / "escape").exists()


def test_artifact_sink_rejects_active_mdx_before_writing(tmp_path: Path):
    malicious = SAFE_ARTIFACT_MDX + "\n<script>alert(1)</script>\n"
    with pytest.raises(MdxSecurityError):
        save_generation_artifacts(_artifact_result(content=malicious), output_dir=str(tmp_path))
    assert not (tmp_path / "generations").exists()


def test_artifact_sink_defaults_to_no_overwrite(tmp_path: Path):
    first = save_generation_artifacts(_artifact_result(), output_dir=str(tmp_path))
    with pytest.raises(FileExistsError, match="Artifact conflict"):
        save_generation_artifacts(_artifact_result(), output_dir=str(tmp_path))

    save_generation_artifacts(
        _artifact_result(content=SAFE_ARTIFACT_MDX.replace("Plain text.", "Replacement.")),
        output_dir=str(tmp_path),
        overwrite=True,
    )
    assert "Replacement." in Path(first["final"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Publisher Filesystem Sink
# ---------------------------------------------------------------------------

def _pub_content(body: str = "## Safe") -> str:
    return (
        "---\n"
        "title: 'Secure publish'\n"
        "description: 'A sufficiently descriptive article for publisher tests.'\n"
        "---\n"
        f"{body}\n"
    )


def test_publisher_rejects_active_mdx_before_writing(tmp_path):
    publisher = AstroPublisher(blog_path=tmp_path, assets_path=tmp_path)
    result = publisher.publish(_pub_content("<script>alert(1)</script>"), "unsafe-post")
    assert result["success"] is False
    assert "MDX security" in result["error"]
    assert not (tmp_path / "unsafe-post.mdx").exists()


@pytest.mark.parametrize("slug", ["../escape", "safe/../../escape", "", "/absolute"])
def test_publisher_rejects_unsafe_slug_at_filesystem_sink(tmp_path, slug):
    publisher = AstroPublisher(blog_path=tmp_path, assets_path=tmp_path)
    result = publisher.publish(_pub_content(), slug)
    assert result["success"] is False
    assert "slug" in result["error"]
    assert list(tmp_path.glob("*.mdx")) == []


def test_publisher_defaults_to_no_overwrite_and_allows_explicit_retry(tmp_path):
    publisher = AstroPublisher(blog_path=tmp_path, assets_path=tmp_path)
    target = tmp_path / "collision.mdx"
    target.write_text(_pub_content("## Original"), encoding="utf-8")

    conflict = publisher.publish(_pub_content("## Replacement"), "collision")
    replaced = publisher.publish(_pub_content("## Replacement"), "collision", overwrite=True)

    assert conflict["success"] is False
    assert conflict["conflict"] is True
    assert conflict["existing_slug"] == "collision"
    assert replaced["success"] is True
    assert "Replacement" in target.read_text(encoding="utf-8")


def test_publisher_resolves_environment_paths_at_instance_creation(tmp_path, monkeypatch):
    blog = tmp_path / "blog"
    assets = tmp_path / "assets"
    monkeypatch.setenv("ASTRO_BLOG_PATH", str(blog))
    monkeypatch.setenv("ASTRO_ASSETS_PATH", str(assets))

    publisher = AstroPublisher()
    assert publisher.blog_path == blog.resolve()
    assert publisher.assets_path == assets.resolve()


# ---------------------------------------------------------------------------
# Fail-Closed MDX Security Validator
# ---------------------------------------------------------------------------

SAFE_RICH_MDX = """---
title: 'Safe article'
description: 'A safe article used by the MDX security tests.'
---
import { Image } from 'astro:assets';
import CitationsSection from '@/components/content/CitationsSection.astro';

## Safe body

[Acme Cloud](https://acme.dev/pricing)

<Image src="/src/assets/blog/safe-hero.webp" alt="Safe hero" />

<CitationsSection>
<span id="cite-1">[1]</span> <a href="https://example.com/report" target="_blank" rel="nofollow">Report</a>
</CitationsSection>
"""


def test_safe_generated_mdx_is_accepted():
    assert assert_safe_mdx(SAFE_RICH_MDX) == SAFE_RICH_MDX


@pytest.mark.parametrize(
    "payload, issue",
    [
        ("export const prerender = false;", "ESM"),
        ("import Evil from 'evil-package';", "import"),
        ("{process.env.SECRET}", "expression"),
        ("<script>alert(1)</script>", "script"),
        ('<iframe src="https://evil.example" />', "element"),
        ("<UnknownWidget />", "component"),
        ('<a href="https://example.com" onClick={() => alert(1)}>x</a>', "attribute"),
        ("<div set:html={payload} />", "attribute"),
        ("[click](javascript:alert(1))", "URL"),
        ('<a href="&#x6a;avascript:alert(1)">click</a>', "URL"),
        ("<a href=javascript:alert(1)>Open</a>", "unsafe URL scheme"),
        ('<img alt="`" onerror="alert(1)" data-note="`" src="https://example.com/x.png">', "attribute"),
        ('<base href="https://attacker.example/">', "element"),
        ('<svg><a><animate attributeName="href" values="javascript:alert(1)"/></a></svg>', "attribute|element"),
        ('<button formaction="javascript:alert(1)">x</button>', "attribute|element"),
        ('[Open](<javascript:alert(1)> "title")', "URL"),
        ("[Open](<javascript:alert(1))", "URL"),
        ("[Open][target]\n\n[target]: javascript:alert(1)", "URL"),
        ("[safe](https://example.com) [open](javascript:alert(1))", "URL"),
        ("[{globalThis.alert(1)}](https://example.com)", "expression"),
    ],
)
def test_active_mdx_payloads_are_rejected(payload, issue):
    with pytest.raises(MdxSecurityError, match=issue):
        assert_safe_mdx(f"---\ntitle: 'x'\ndescription: 'y'\n---\n\n{payload}\n")


def test_code_fences_with_safe_commands_pass_while_active_markup_inside_fences_is_rejected():
    safe_shell = """---
title: 'CLI example'
description: 'Documents safe shell commands.'
---

```bash
pg_restore --clean --no-owner -d "$DATABASE_URL" nightly.dump
```
"""
    assert assert_safe_mdx(safe_shell) == safe_shell

    unsafe_fence = """---
title: 'Code example'
description: 'Active syntax inside fences is rejected fail-closed without masking.'
---

```jsx
<script>alert(1)</script>
{process.env.SECRET}
```
"""
    with pytest.raises(MdxSecurityError):
        assert_safe_mdx(unsafe_fence)


def test_longer_closing_fence_does_not_mask_subsequent_active_markup():
    smuggled = """---
title: 'Fence breakout'
description: 'Longer closing fence closes CommonMark code block.'
---

~~~text
safe example
~~~~
<script>alert(1)</script>
~~~
"""
    with pytest.raises(MdxSecurityError, match="script"):
        assert_safe_mdx(smuggled)

    unclosed = """---
title: 'Unclosed fence'
description: 'Unclosed fence must still be validated.'
---

```text
<script>alert(1)</script>
"""
    with pytest.raises(MdxSecurityError, match="script"):
        assert_safe_mdx(unclosed)


def test_abrupt_comment_or_html_block_fence_cannot_mask_script():
    abrupt_comment = "---\ntitle: 'x'\ndescription: 'y'\n---\n\n<!-->\n<script>alert(1)</script>\n<!-- safe -->\n"
    with pytest.raises(MdxSecurityError, match="forbidden control character or markup|script"):
        assert_safe_mdx(abrupt_comment)

    html_block_fence = "---\ntitle: 'x'\ndescription: 'y'\n---\n\n<div>\n```\n<script>alert(1)</script>\n```\n</div>\n"
    with pytest.raises(MdxSecurityError, match="script"):
        assert_safe_mdx(html_block_fence)


def test_static_comparison_table_expression_is_the_only_allowed_expression_shape():
    content = """---
title: 'Comparison'
description: 'Static comparison data.'
---
import ComparisonTable from '@/components/sections/ComparisonTable.astro';

<ComparisonTable
  title="Feature comparison"
  description="Static values"
  competitorName="Alternative"
  items={[
    {feature: "Ease of use", primary: true, competitor: "Moderate", highlight: true}
  ]}
/>
"""
    assert assert_safe_mdx(content) == content


def test_generated_markdown_link_parts_are_escaped_and_scheme_checked():
    assert escape_markdown_link_text("Report ](javascript:alert(1))") == r"Report \]\(javascript:alert\(1\)\)"
    assert sanitize_content_url("https://example.com/a path?q=one two") == "https://example.com/a%20path?q=one%20two"
    with pytest.raises(MdxSecurityError, match="URL"):
        sanitize_content_url("java%73cript:alert(1)")


def test_generator_rejects_unsafe_first_transform_and_falls_back_afterwards():
    generator = ContentGenerator()
    unsafe = "---\ntitle: 'x'\ndescription: 'y'\n---\n<script>alert(1)</script>"
    safe = "---\ntitle: 'x'\ndescription: 'y'\n---\n## Safe"

    with pytest.raises(MdxSecurityError, match="raw generation"):
        generator._accept_safe_mdx(unsafe, stage="raw generation")
    assert generator._accept_safe_mdx(unsafe, fallback=safe, stage="edit pass") == safe


def test_prepare_raw_draft_escapes_benign_prose_placeholder_without_touching_frontmatter():
    generator = ContentGenerator.__new__(ContentGenerator)
    filler = " ".join(["practical"] * 1020)
    draft = f"---\ntitle: 'Guide {{v1}}'\ndescription: 'Valid description for draft.'\n---\n\n## Cost formula\n\nTrack {{cost}} across {filler}.\n"
    cleaned, words, _detail = generator._prepare_raw_draft(draft)
    assert "title: 'Guide {v1}'" in cleaned
    assert "&#123;cost&#125;" in cleaned
    assert words >= 1000
    assert_safe_mdx(cleaned)


def test_escaped_backtick_does_not_open_inline_code_span_or_mask_event_handler():
    escaped_payload = '---\ntitle: "x"\ndescription: "y"\n---\n\n\\`<img src="missing" onerror="alert(1)">`\n'
    with pytest.raises(MdxSecurityError, match="unsafe JSX attribute"):
        assert_safe_mdx(escaped_payload)

    even_backslash_payload = '---\ntitle: "x"\ndescription: "y"\n---\n\n\\\\`<img src="missing" onerror="alert(1)">`\n'
    with pytest.raises(MdxSecurityError, match="unsafe JSX attribute"):
        assert_safe_mdx(even_backslash_payload)


def test_pseudo_frontmatter_prefix_does_not_hide_script_payload():
    payload = "---not-frontmatter\n<script>alert(1)</script>\n---\nSafe body\n"
    with pytest.raises(MdxSecurityError, match="script elements are forbidden"):
        assert_safe_mdx(payload)


@pytest.mark.parametrize(
    "block_line",
    [
        "# <img src=x onerror=alert(1)>`",
        "- <img src=x onerror=alert(1)>`",
        "> <img src=x onerror=alert(1)>`",
    ],
)
def test_inline_backtick_cannot_cross_block_boundary_to_mask_xss(block_line):
    payload = f'---\ntitle: "x"\ndescription: "y"\n---\n\n`safe\n{block_line}\n'
    with pytest.raises(MdxSecurityError):
        assert_safe_mdx(payload)

    same_para_safe = '---\ntitle: "x"\ndescription: "y"\n---\n\n`const a = 1;\nconst b = 2;`\n'
    assert assert_safe_mdx(same_para_safe) == same_para_safe


@pytest.mark.parametrize(
    "payload",
    [
        "[a [b] c](javascript:alert(1))",
        "[a\nb](javascript:alert(1))",
        "[a\\]b](javascript:alert(1))",
        "![a [b] c](javascript:alert(1))",
        "[*a* ![i](https://x.example/i.png)](javascript:alert(1))",
        "[x][a\\]b]\n\n[a\\]b]: javascript:alert(1)",
        "[x][a\nb]\n\n[a\nb]: javascript:alert(1)",
    ],
)
def test_destination_anchored_link_checks_reject_nested_or_multiline_link_text(payload):
    with pytest.raises(MdxSecurityError, match="URL"):
        assert_safe_mdx(f"---\ntitle: 'x'\ndescription: 'y'\n---\n\n{payload}\n")
    # Legitimate reference-like prose without a dangerous scheme still passes.
    safe_prose = "---\ntitle: 'x'\ndescription: 'y'\n---\n\nSee [1]: details here and [docs](https://acme.dev/docs(v2)).\n"
    assert assert_safe_mdx(safe_prose) == safe_prose


@pytest.mark.parametrize(
    "esm_line",
    [
        'import é from "data:text/javascript,export default 1"',
        'import"data:text/javascript,export default 1"',
        'export async function loader() { return 1; }',
    ],
)
def test_non_ascii_and_spaceless_esm_imports_are_rejected(esm_line):
    with pytest.raises(MdxSecurityError, match="ESM"):
        assert_safe_mdx(f"---\ntitle: 'x'\ndescription: 'y'\n---\n\n{esm_line}\n\ntext\n")
    prose = "---\ntitle: 'x'\ndescription: 'y'\n---\n\nImport your data or\nexport to CSV with an\nimportant note.\n"
    assert assert_safe_mdx(prose) == prose


@pytest.mark.parametrize(
    "payload",
    [
        '<\ufeffscript src="https://evil.example/x.js"><\ufeff/script>',
        '<\u00a0base href="https://evil.example/" />',
        '<\u00a0iframe src="https://evil.example/"><\u00a0/iframe>',
        '<\u00a0object data="data:text/html;base64,AAAA"><\u00a0/object>',
        '<\x0bbase href="https://evil.example/" />',
        '\\<a title="x <base href=\'https://evil.example/\' /> y">z',
        '\\<a title="x <iframe src=\'https://evil.example/\'></iframe> y">z',
    ],
)
def test_unicode_whitespace_tags_and_escaped_bracket_swallowing_are_rejected(payload):
    with pytest.raises(MdxSecurityError):
        assert_safe_mdx(f"---\ntitle: 'x'\ndescription: 'y'\n---\n\n{payload}\n")
    # Benign inequalities and dollar thresholds still pass.
    benign = "---\ntitle: 'x'\ndescription: 'y'\n---\n\nCosts stay <$500 while latency a < b holds.\n"
    assert assert_safe_mdx(benign) == benign


@pytest.mark.parametrize(
    "differential",
    [
        "---\ntitle: 'x'\n---\n- item\n  ```\n{1+1}\n  ```\n",
        "---\ntitle: 'x'\n---\n```\nfoo\n    ```\n{1+1}\n```\n",
        "---\ntitle: 'x'\n---\ntext\x0c~~~\n{1+1}\n\x0c~~~\n",
        "---\ntitle: 'x'\n---\n- item\n  ```\n    ```\n  {1+1}\n  ```\n",
        "---\ntitle: 'x'\n---\n- item\n  ```\n  x\n```\nfoo\n```\n{1+1}\n```\n",
        "---\ntitle: 'x'\n---\ntext `a\n===\n{1+1}` tail\n",
        "---\ntitle: 'x'\n---\na `b\n{1+1}\n` c\n",
        "---\ntitle: 'x'\n---\n| a | b | c |\n|---|---|---|\n| `x | {1+1} | ` |\n",
        "---\ntitle: 'x'\n---\n```\n<!--\n```\n{1+1}\n```\n-->\n```\n",
        "---\ntitle: x\n---\r\n{1+1}\n\n---\n\nbody\n",
        "---\ntitle: 'x'\n---\n[`]`](javascript:alert(1))\n",
    ],
)
def test_mdx_parser_differential_payloads_and_crlf_frontmatter(differential):
    with pytest.raises(MdxSecurityError):
        assert_safe_mdx(differential)
    crlf_safe = "---\r\ntitle: 'Safe CRLF'\r\ndescription: 'Valid CRLF frontmatter'\r\n---\r\n\r\n## Clean body\r\n\r\nParagraph text.\r\n"
    assert assert_safe_mdx(crlf_safe) == crlf_safe


@pytest.mark.parametrize(
    "payload",
    [
        r"See [our guide](/pricing `{process.env.OPENROUTER_API_KEY}`) now.",
        r"See [our guide](/pricing \`{process.env.OPENROUTER_API_KEY}`) now.",
        "[a](/x {alert(1)})",
        "[a](/x/{alert(1)})",
        "[ref]: /x {alert(1)}",
        r"[ref]: /pricing `{process.env.OPENROUTER_API_KEY}`",
        "[a](/x <script>alert(1)</script>)",
        "[a](/x<script>alert(1)</script>)",
        "[ref]: /x <img src=x onerror=alert(1)>",
    ],
)
def test_markdown_links_and_ref_defs_cannot_smuggle_expressions_or_raw_markup(payload):
    with pytest.raises(MdxSecurityError):
        assert_safe_mdx(f"---\ntitle: 'x'\ndescription: 'y'\n---\n\n{payload}\n")


def test_publisher_handoff_mode_rejects_malformed_link_expression_payloads(tmp_path):
    from core.site_adapters import SiteContract

    publisher = AstroPublisher(
        blog_path=tmp_path,
        assets_path=tmp_path,
        site_contract=SiteContract(mode="handoff", framework="nextjs"),
    )
    for payload in (
        r"See [our guide](/pricing \`{process.env.OPENROUTER_API_KEY}`) now.",
        "[a](/x {alert(1)})",
    ):
        res = publisher.publish(_pub_content(payload), "smuggled-link")
        assert res["success"] is False
        assert "MDX security" in res["error"]
        assert not (tmp_path / "smuggled-link.mdx").exists()

    valid_titled = (
        "---\ntitle: 'x'\ndescription: 'y'\n---\n\n"
        'See [our guide](/pricing "Pricing Guide") and [docs][d].\n\n'
        '[d]: <https://acme.dev/docs> "Docs"\n'
    )
    assert assert_safe_mdx(valid_titled) == valid_titled
