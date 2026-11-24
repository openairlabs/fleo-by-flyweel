## Summary

Briefly describe what this PR changes and why.

Closes #

## Verification Checklist

All four offline CI gates must pass before merge:

- [ ] `./venv/bin/pytest tests/ -m unit -q`
- [ ] `./venv/bin/python eval/run_quality_eval.py --ci`
- [ ] `pnpm typecheck`
- [ ] `pnpm test`

## Architecture & Safety Checklist

- [ ] `core/` remains 100% company-agnostic (no hardcoded company domains, vendor lists, or framework-specific imports) and has no `core/__init__.py`
- [ ] Bridge commands (`tui_bridge.py` / `core/tui_bridge.py`) emit strictly JSONL on `stdout` (logs go to `stderr`) and only add fields additively
- [ ] No `.env` files, API keys, or private workspace artifacts are included in this diff
