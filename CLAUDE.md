# CLAUDE.md: Fleo by Flyweel

See `AGENTS.md` for full repository architecture, commands, and testing gates. Key invariants referenced in inline code comments:

1. **Nebius / OpenRouter Fast Reasoning Rule**: Long-output editing, polishing, and rubric-judging calls on Nebius-hosted models (`Qwen`, `Hermes`) must disable reasoning (`OR_PROVIDER_NEBIUS_FAST` / `reasoning: off`) so reasoning tokens never exhaust `max_tokens` and truncate output.
2. **GSC & Optional Services Degrade Gracefully**: Missing or unreachable Google Search Console, Perplexity, or Interfaze credentials must log and degrade cleanly to unscored/fallback paths; never raise uncaught exceptions out of research or ideation.
3. **Frozen JSONL Bridge Contract**: `tui_bridge.py` and `core/tui_bridge.py` must emit strictly JSONL on `stdout` (all diagnostics/logs go to `stderr`). Add new event types/fields additively without breaking existing consumers.
4. **Flat `core/` Module Layout**: `core/` intentionally has no `__init__.py` (scripts/tests add the repo root to `sys.path`). Keep `core/` company-agnostic; all brand facts come from `BrandProfile` (`.fleo/brand/profile.json` / `.agents/product-marketing.md`).
