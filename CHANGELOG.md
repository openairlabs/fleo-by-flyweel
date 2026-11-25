# Changelog

## [0.1.0] - 2026-11-25

### Initial Public Release (`flyweel-fleo`)
- **Company-Agnostic `BrandProfile` Engine**: Full decoupling for any industry or startup, with live URL brand extraction (`llms.txt` + `sitemap.xml` + clean HTML to Markdown), 5-question TUI interview, and bidirectional `.agents/product-marketing.md` sync.
- **Zero-Touch `npx flyweel-fleo` Onboarding Wizard**: Ink 7 full-screen TUI setup wizard with automatic Python runtime provisioning (`uv` / `~/.fleo/venv`).
- **1-Key Quickstart + Per-Agent API Keys**: Run the entire multi-model pipeline on a single `OPENROUTER_API_KEY`, or configure separate `FLEO_<ROLE>_API_KEY` and `FLEO_<ROLE>_MODEL` overrides across 8 agent functions.
- **Multi-Framework Output & Site Learner**: Native adaptation for **Next.js**, **Astro**, **Webflow**, **Framer**, **Nuxt**, **SvelteKit**, **Remix**, and **Universal Markdown**, plus restored `@graph` JSON-LD (`Organization`, `Author`, `BlogPosting`, `BreadcrumbList`, `FAQPage`) and visible body FAQs. Direct local repo publishing is included as an **experimental** option with custom publish folder path support (Step 4, `--content-dir`, or `FLEO_BLOG_CONTENT_PATH`) alongside the recommended `agent-handoff.md` workflow.
- **Universal `agent-handoff.md` Packet**: Self-contained prompt-plus-content artifact for publishing SEO/AEO-optimized articles via Claude Code, Cursor, OpenCode, or Lovable without connecting Fleo to your repo.
- **Deterministic Citation Ledger Verifier & Security Hardening**: Strips hallucinated URLs against the run's research ledger, enforces SSRF-safe crawling, pins provider-native API endpoints, and validates all generated markup fail-closed.
- **Apache-2.0 License**: Released under Apache License 2.0 with `NOTICE`.
