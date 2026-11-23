# Contributing to Fleo by Flyweel

Contributions, framework adapter improvements, and bug reports are welcome!

## Local Setup

1. **Node.js ≥ 22** and **Python ≥ 3.10**
2. Install dependencies:
   ```bash
   python3 -m venv venv
   ./venv/bin/pip install -r requirements-dev.txt
   pnpm install
   pnpm build
   ```
3. Copy `.env.example` to `.env` and add `OPENROUTER_API_KEY` (never commit `.env` or real brand credentials).

## Running Tests Before Opening a PR

All four offline checks must pass:

```bash
./venv/bin/pytest tests/ -m unit -v
./venv/bin/python eval/run_quality_eval.py --ci
pnpm typecheck
pnpm test
```

## Architecture Notes

- `core/` intentionally has **no `__init__.py`**; top-level runners (`generate.py`, `tui_bridge.py`) add repo root to `sys.path`.
- The Ink TUI (`tui/`) communicates with Python exclusively via JSONL over stdio (`tui_bridge.py`). Keep `stdout` in bridge helpers strictly JSONL-only (log to `stderr`).
- Keep `core/` 100% company-agnostic — never hardcode brand names, domains, or framework-proprietary MDX imports in core modules.
