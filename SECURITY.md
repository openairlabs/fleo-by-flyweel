# Security Policy

## Reporting a Vulnerability

Please **do not** open public GitHub issues for security vulnerabilities. Report security issues privately via [GitHub Private Vulnerability Reporting](https://github.com/openairlabs/fleo-by-flyweel/security/advisories/new) on `openairlabs/fleo-by-flyweel`.

## Built-In Security Boundaries

1. **Fail-Closed Markup Security (`core/mdx_security.py`)**: All LLM-generated Markdown/MDX is treated as untrusted input. `<script>`, `<iframe>`, event handlers (`on*=`), `dangerouslySetInnerHTML`, Svelte `{@html}`, unapproved ESM `import`/`export` lines, arbitrary `{...}` expressions (including inside malformed Markdown link titles or reference definitions), and non-`http(s)`/`mailto` URLs are rejected before any disk write.
2. **SSRF Protection (`core/url_safety.py`)**: Website scanning resolves DNS and blocks loopback, RFC1918 private networks, link-local, and cloud metadata IPs on every redirect hop.
3. **Indirect Prompt Injection Fencing**: Crawled company pages (`llms.txt` external links excluded), SERP summaries, community snippets, and profile compliance rules are wrapped in sanitized `<untrusted_external_data>` fences with system-level untrusted-data instructions; `agent-handoff.md` fences generated article prose inside `<untrusted_article_content>...</untrusted_article_content>` with paired begin/end markers; `BrandProfile` extraction validates structured JSON via Pydantic; and generated article prose passes through fail-closed markup validation (`core/mdx_security.py`).
4. **Secret Isolation**: API keys are stored exclusively in `.env` with `0600` permissions via atomic temp-file replacement — never in `.fleo/brand/profile.json`, `.agents/product-marketing.md`, or output artifacts. Provider-native keys (`OPENROUTER_API_KEY`, `PERPLEXITY_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`) are pinned to their native endpoints; `*_BASE_URL` overrides are blocked from workspace `.env` files and apply only when exported in the shell/process environment alongside a dedicated `FLEO_<ROLE>_API_KEY`. Unknown or ambiguous key prefixes (such as bare `sk-` keys) are refused unless paired with an explicit HTTPS base URL. Do not run Fleo inside a repository you do not trust without inspecting its `.env` and `.fleo/` files first.
