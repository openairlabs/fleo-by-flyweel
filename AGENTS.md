# AGENTS.md — Fleo by Flyweel

Compact contributor & coding-agent guide for this repository.

## Architecture Overview

- **Python async content pipeline (`core/`, `generate.py`, `tui_bridge.py`)** + **TypeScript/React 19/Ink 7 TUI (`tui/`, `bin/fleo.js`)**.
- `core/` has **NO `__init__.py`**; top-level scripts and tests import via `sys.path.insert(0, ...)`. Keep `core/` flat — do not add `__init__.py` inside `core/`.
- **Company-agnostic**: Brand context lives in `.fleo/brand/profile.json` (and `.agents/product-marketing.md` via `core/brand_profile.py`). Never hardcode company-specific domains, authors, or compliance copy in `core/`.
- **Multi-framework output**: `core/site_adapters.py` adapts canonical output to `nextjs`, `astro`, `webflow`, `framer`, `nuxt`, `sveltekit`, `remix`, or `markdown`, and generates `agent-handoff.md` + `schema.json` on every save.
- **Per-agent credentials**: `core/agent_credentials.py` resolves `FLEO_<ROLE>_API_KEY` + `FLEO_<ROLE>_MODEL` (`RESEARCH`, `DRAFT`, `POLISH`, `SEO`, `LIGHT`, `EMBED`, `EXTRACTOR`, `DECISIONS`) with automatic fallback to `OPENROUTER_API_KEY`.

## Key Commands

- Run TUI: `node bin/fleo.js` (or `pnpm dev` during development)
- Run Setup Wizard directly: `node bin/fleo.js setup`
- Headless CLI: `./venv/bin/python generate.py --no-int -k "topic" --local`
- Python unit tests: `./venv/bin/pytest tests/ -m unit -v`
- Quality regression harness: `./venv/bin/python eval/run_quality_eval.py --ci`
- TypeScript typecheck & TUI tests: `pnpm typecheck && pnpm test`
