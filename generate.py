#!/usr/bin/env python3
"""
V2 Content Generator - Fast, lean, effective

Usage:
  python generate.py -k "your keyword"              # Original CLI mode
  python generate.py -i                              # Interactive mode with prompts
  python generate.py -i -k "keyword"                 # Interactive with keyword preset
  python generate.py -i --publish                    # Interactive + auto-publish to astro
"""
# --- venv auto-activation -----------------------------------------------------
# Re-exec under ./venv/bin/python if available and not already active, so
# `python generate.py ...` works without manually sourcing venv/bin/activate.
import os as _os
import sys as _sys
from pathlib import Path as _Path
_venv_py = _Path(__file__).resolve().parent / "venv" / "bin" / "python"
# sys.prefix != sys.base_prefix means we're already inside SOME venv; skip re-exec.
# Comparing executable paths via .resolve() doesn't work because venv/bin/python is
# usually a symlink back to the system interpreter -- the venv is detected via
# pyvenv.cfg next to the *unresolved* executable path, not the binary itself.
if _venv_py.exists() and _sys.prefix == _sys.base_prefix:
    _os.execv(str(_venv_py), [str(_venv_py), str(_Path(__file__).resolve()), *_sys.argv[1:]])
# -----------------------------------------------------------------------------
import asyncio
import argparse
import json
import sys
import os
from pathlib import Path
from datetime import datetime

_PKG_ROOT = str(Path(__file__).resolve().parent)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

# Load repository environment without overriding the invoking process.
from core.paths import load_repo_env
load_repo_env()

from core.generator import ContentGenerator


def run_interactive_mode(args):
    """Launch the interactive CLI mode"""
    from core.cli.app import interactive_generate
    import click

    # Build Click args list from argparse args
    cli_args = []

    # Add keyword if provided
    if hasattr(args, 'keyword') and args.keyword:
        cli_args.extend(['-k', args.keyword])

    # Add style if not default
    if hasattr(args, 'style') and args.style and args.style != 'standard':
        cli_args.extend(['--style', args.style])
    if getattr(args, 'solutions', None) is not None and (
        args.solutions != 8 or any(a == '--solutions' or a.startswith('--solutions=') for a in sys.argv[1:])
    ):
        cli_args.extend(['--solutions', str(args.solutions)])

    # Add flags
    if getattr(args, 'no_reddit', False):
        cli_args.append('--nr')
    if getattr(args, 'reddit_limited', False):
        cli_args.append('--nrl')
    if getattr(args, 'no_flyweel', False):
        cli_args.append('--nfw')
    if getattr(args, 'limited_flyweel', False):
        cli_args.append('--lfw')
    if getattr(args, 'skip_icp', False):
        cli_args.append('--no-icp')
    # augment_serp defaults to True, so only add flag if explicitly disabled
    if not getattr(args, 'augment_serp', True):
        cli_args.append('--no-augment-serp')
    if getattr(args, 'publish', False):
        cli_args.append('--publish')
    if getattr(args, 'local', False):
        cli_args.append('--local')
    if getattr(args, 'no_draft', False):
        cli_args.append('--no-draft')
    if getattr(args, 'json_output', False):
        cli_args.append('--json')
    if getattr(args, 'competitors', None):
        cli_args.extend(['--comp', args.competitors])
    if getattr(args, 'no_competitors', False):
        cli_args.append('--nc')
    if getattr(args, 'note', None):
        cli_args.extend(['--note', args.note])

    # Forward new parameters
    if getattr(args, 'use_llama_polish', False):
        cli_args.append('--use-llama-polish')
    if getattr(args, 'save_research', False):
        cli_args.append('--save-research')
    if getattr(args, 'judge', False):
        cli_args.append('--judge')
    if hasattr(args, 'output') and args.output != 'output':
        cli_args.extend(['--output', args.output])
    if hasattr(args, 'depth') and args.depth != 'standard':
        cli_args.extend(['--depth', args.depth])
    if getattr(args, 'min_tools', None) is not None:
        cli_args.extend(['--min-tools', str(args.min_tools)])
    # For gsc_check/gsc_keywords - only pass if disabled (they're on by default)
    if not getattr(args, 'gsc_check', True):
        cli_args.append('--no-gsc-check')
    if not getattr(args, 'gsc_keywords', True):
        cli_args.append('--no-gsc-keywords')
    if getattr(args, 'verbose', False):
        cli_args.append('--verbose')

    # Invoke Click command
    try:
        interactive_generate(cli_args, standalone_mode=False)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

def make_clickable_path(filepath):
    """Create OSC 8 hyperlink for terminals (opens in default Windows editor when clicked)"""
    import subprocess
    import shlex

    # Get absolute path
    abs_path = Path(filepath).resolve()

    # Convert to Windows path using wslpath (WSL environments)
    try:
        result = subprocess.run(
            ['wslpath', '-w', str(abs_path)],
            capture_output=True,
            text=True,
            timeout=2,
            errors='replace'  # Handle unicode errors gracefully
        )
        if result.returncode == 0 and result.stdout.strip():
            windows_path = result.stdout.strip()

            # Create file:// URI using Windows UNC path format
            # Windows Terminal recognizes: file://wsl.localhost/Ubuntu/path/to/file
            # This is the WSL 2 network path format that Windows understands

            # Convert Windows path to WSL UNC format
            # \\wsl.localhost\Ubuntu\home\... becomes file://wsl.localhost/Ubuntu/home/...
            if windows_path.startswith('\\\\wsl.localhost\\'):
                # Remove leading \\ and convert backslashes to forward slashes
                unc_path = windows_path[2:].replace('\\', '/')
                file_uri = f"file://{unc_path}"
            elif windows_path.startswith('\\\\wsl$\\'):
                # Legacy WSL 1 format
                unc_path = windows_path[2:].replace('\\', '/')
                file_uri = f"file://{unc_path}"
            else:
                # Standard Windows path (C:\Users\...)
                # Convert C:\path to file:///C:/path
                drive_path = windows_path.replace('\\', '/')
                file_uri = f"file:///{drive_path}"

            # Create OSC 8 hyperlink (clickable in Windows Terminal, iTerm2, etc.)
            # Format: ESC]8;;URI ESC\TEXT ESC]8;; ESC\
            # \033 is ESC, \007 is BEL (alternative terminator for better compatibility)
            # Add jade/mint color (ANSI 38;2;R;G;B for 24-bit color)
            # Jade/mint: RGB(80, 200, 120) - bright, pleasant green
            jade_color = "\033[38;2;80;200;120m"
            reset_color = "\033[0m"

            hyperlink = f"{jade_color}\033]8;;{file_uri}\007{windows_path}\033]8;;\007{reset_color}"

            return hyperlink

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        # Log error silently and fall through to fallback
        pass
    except Exception:
        # Catch-all for unexpected errors (unicode, permissions, etc.)
        pass

    # Fallback for non-WSL environments or if wslpath fails
    # Use standard file:// URI with Unix path
    jade_color = "\033[38;2;80;200;120m"
    reset_color = "\033[0m"
    file_uri = abs_path.as_uri()
    return f"{jade_color}\033]8;;{file_uri}\007{abs_path}\033]8;;\007{reset_color}"

def main():
    """Main entry point with mode detection

    Default: Interactive mode with Rich prompts and progress
    Use --no-int for classic argparse CLI mode
    """
    parser = argparse.ArgumentParser(
        description="V2 - Lean content generator (Interactive mode by default)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python generate.py                                    # Interactive mode (default)
  python generate.py -k "keyword"                       # Interactive with keyword preset
  python generate.py --publish                          # Interactive + auto-publish
  python generate.py --no-int -k "lead gen"             # Classic CLI mode
        """
    )

    # Mode selection: interactive is default
    parser.add_argument('--no-int', '--no-interactive', action='store_true', dest='no_interactive',
                       help='Disable interactive mode, use classic CLI (requires -k)')
    parser.add_argument('-i', '--interactive', action='store_true',
                       help='(Redundant) Force interactive mode - already default')

    # Original arguments
    parser.add_argument('-k', '--keyword', help='Keyword to generate content for')
    parser.add_argument('--style', default='standard',
                       choices=['standard', 'guide', 'comparison', 'research', 'news', 'category', 'top-compare', 'feature'],
                       help='Content style (standard/guide/comparison/research/news/category/top-compare/feature)')
    parser.add_argument('--solutions', type=int, default=8, choices=[5, 8, 10, 15],
                       help='Number of solutions for top-compare style (5/8/10/15)')
    parser.add_argument('--output', default='output', help='Output directory')
    parser.add_argument('--save-research', action='store_true', help='Save research data')
    parser.add_argument('--judge', action='store_true', help='Add LLM-judge subjective quality dimensions (clarity/fluency/tone) to the scorecard')
    parser.add_argument('--augment-serp', action='store_true', default=True,
                       help='Augment with SERP and GSC data (enabled by default)')
    parser.add_argument('--nr', '--no-reddit', action='store_true', dest='no_reddit',
                       help='Skip Reddit and Quora research entirely')
    parser.add_argument('--nrl', '--no-reddit-limited', action='store_true', dest='reddit_limited',
                       help='Limited insights: 3 Reddit + 1 Quora (highest relevance only)')
    parser.add_argument('--use-llama-polish', action='store_true', dest='use_llama_polish',
                       help='Polish with the tier creative model instead of the polish model')
    parser.add_argument('--nfw', '--no-flyweel', action='store_true', dest='no_flyweel',
                       help='Remove Flyweel brand mentions (keep only final CTA)')
    parser.add_argument('--lfw', '--limited-flyweel', action='store_true', dest='limited_flyweel',
                       help='Limited Flyweel mentions (2-3 highly natural mentions only)')
    parser.add_argument('--no-icp', '--skip-icp', action='store_true', dest='skip_icp',
                       help='Skip ICP context injection (no industry-specific audience targeting)')

    # GSC/SEO optimization flags (Phase 7.1-7.4) - ENABLED BY DEFAULT
    parser.add_argument('--no-gsc-check', action='store_false', dest='gsc_check', default=True,
                       help='Disable GSC cannibalization check (enabled by default)')
    parser.add_argument('--no-gsc-keywords', action='store_false', dest='gsc_keywords', default=True,
                       help='Disable GSC-derived keywords for title/H2 (enabled by default)')
    parser.add_argument('--min-tools', type=int, default=None, dest='min_tools',
                       help='Minimum number of tools/solutions for comparison content (default: 12)')
    parser.add_argument('--depth', default='standard', choices=['quick', 'standard', 'comprehensive'],
                       help='Content depth: quick (1500w), standard (2500w), comprehensive (4000w+)')

    # New interactive mode flags
    parser.add_argument('--publish', action='store_true',
                       help='Auto-publish to connected site repository (interactive mode)')
    parser.add_argument('--local', action='store_true',
                       help='Save locally only (interactive mode)')
    parser.add_argument('--no-draft', action='store_true', dest='no_draft',
                       help='Publish without draft status (interactive mode)')
    parser.add_argument('--json', action='store_true', dest='json_output',
                       help='Output as JSON (for automation)')
    parser.add_argument('--comp', '--competitors', dest='competitors',
                       help='Comma-separated competitor names (e.g., "Windsor MCP, Pipeboard MCP")')
    parser.add_argument('--nc', '--no-competitors', action='store_true', dest='no_competitors',
                       help='No competitors: skip competitor selection entirely (no discovery, no prompt)')
    parser.add_argument('-v', '--verbose', action='store_true', dest='verbose',
                       help='Show detailed debug logs')
    parser.add_argument('--note', dest='note', default=None,
                       help='Freeform note/context to bias generation '
                            '(non-interactive global note; interactive: Tab on a step)')

    args = parser.parse_args()

    # Route to appropriate mode
    # Interactive is now the DEFAULT
    if args.no_interactive:
        # Classic CLI mode (requires keyword)
        if not args.keyword:
            parser.error("Classic mode requires -k/--keyword (or remove --no-int for interactive mode)")
        asyncio.run(original_main(args))
    else:
        # Default: Interactive mode
        run_interactive_mode(args)


async def original_main(args):
    """Original main function for backward compatibility"""
    from core.generator import configure_logging
    is_json = bool(getattr(args, "json_output", False))
    configure_logging(
        verbose=getattr(args, "verbose", False)
        or os.getenv("LOG_LEVEL", "").upper() == "DEBUG"
    )

    if not is_json:
        print(f"""
🚀 V2 MODE ACTIVATED
━━━━━━━━━━━━━━━━━━━
Keyword: {args.keyword}
Style: {args.style}
━━━━━━━━━━━━━━━━━━━
    """)

    # Check API keys
    from core.agent_credentials import can_generate_with_credentials
    keys_ok, missing_keys = can_generate_with_credentials()

    if not keys_ok and not is_json:
        print(f"⚠️  Warning: Missing API keys: {', '.join(missing_keys)}")
        print("   Some features may be limited\n")
    
    try:
        # Create output directory and initialize generator inside JSON-protected error boundary
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        generator = ContentGenerator()

        # Map depth to solution count for comparison content
        depth_to_solutions = {
            'quick': 8,
            'standard': 12,
            'comprehensive': 15
        }
        # top-compare honours --solutions (as tui_bridge does); other styles size by depth + explicit --min-tools.
        depth_count = depth_to_solutions.get(args.depth, 12)
        solution_count = (
            args.solutions
            if args.style == 'top-compare'
            else (max(args.min_tools, depth_count) if getattr(args, 'min_tools', None) is not None else depth_count)
        )

        # Convert flags to flyweel_mode string
        if args.no_flyweel:
            flyweel_mode = 'none'
        elif getattr(args, 'limited_flyweel', False):
            flyweel_mode = 'limited'
        else:
            flyweel_mode = 'full'

        # Non-interactive --note: single global note transformed once for
        # generation + refine (research consumes the raw form).
        from core.user_note import NoteBundle
        note_bundle = NoteBundle.from_global(getattr(args, 'note', None))
        is_compare_style = args.style in ('comparison', 'top-compare')
        competitor_mentions = True if is_compare_style else (not getattr(args, 'no_competitors', False))
        raw_comp_arg = getattr(args, 'competitors', None)
        parsed_competitors = (
            [c.strip() for c in raw_comp_arg.split(',') if c.strip()]
            if raw_comp_arg
            else ([] if getattr(args, 'no_competitors', False) and not is_compare_style else None)
        )

        result = await generator.generate(
            args.keyword,
            args.style,
            augment_serp=args.augment_serp,
            skip_community=args.no_reddit,
            limit_community=args.reddit_limited,
            use_llama_polish=args.use_llama_polish,
            flyweel_mode=flyweel_mode,
            skip_icp=args.skip_icp,
            solution_count=solution_count,
            gsc_check=args.gsc_check,
            gsc_keywords=args.gsc_keywords,
            approved_solutions=parsed_competitors,
            competitor_mentions=competitor_mentions,
            note_bundle=note_bundle if not note_bundle.is_empty() else None,
            run_judge=getattr(args, 'judge', False),
            depth=args.depth,
        )
        
        if result['success']:
            from core.artifacts import save_generation_artifacts

            saved = save_generation_artifacts(
                result,
                output_dir=args.output,
                save_research=args.save_research,
                overwrite=False,
            )
            if is_json:
                print(json.dumps({
                    "success": True,
                    "result": {
                        "title": result.get("title"),
                        "slug": result.get("slug"),
                        "content": result.get("content"),
                        "word_count": (result.get("metrics") or {}).get("word_count"),
                        "generation_time": (result.get("metrics") or {}).get("generation_time"),
                        "quality_scorecard": result.get("quality_scorecard"),
                        "paths": saved,
                    },
                }, indent=2, default=str))
                return
            generation_folder = Path(saved['directory'])
            final_filepath = Path(saved['final'])
            raw_filepath = Path(saved['raw']) if saved.get('raw') else None

            print(f"📄 Final polished output saved: {make_clickable_path(final_filepath)}")
            if raw_filepath:
                print(f"📄 Raw draft saved: {make_clickable_path(raw_filepath)}")
            
            research_file = Path(saved['metadata'])
            _composite = (result.get('quality_scorecard') or {}).get('composite')
            print(f"📊 Metadata saved: {make_clickable_path(research_file)}"
                  + (f" (quality {_composite}/100)" if _composite is not None else ""))
            
            # Print results
            polish_model = next(
                (v.get('model') for k, v in (result.get('model_used') or {}).items() if 'polish' in k),
                'n/a',
            )
            mode_labels = {
                'none': 'No Flyweel Mode (Educational Only)',
                'limited': 'Limited Flyweel Mode (2-3 mentions)',
                'full': 'Standard Mode (Brand Integrated)'
            }
            mode_label = mode_labels.get(flyweel_mode, 'Standard Mode (Brand Integrated)')
            print(f"""
✅ GENERATION COMPLETE
━━━━━━━━━━━━━━━━━━━━
📝 Title: {result['title']}
📁 Output Folder: {make_clickable_path(generation_folder)}
📄 Final: {make_clickable_path(final_filepath)}
📄 Raw: {make_clickable_path(raw_filepath) if raw_filepath else 'n/a'}
✨ Polish Model: {polish_model}
🎭 Mode: {mode_label}
📏 Words: {result['metrics']['word_count']:,}
⏱️  Time: {result['metrics']['generation_time']:.1f}s
🎯 SEO Score:
   - PAA Coverage: {result['metrics']['paa_questions_answered']}/{len(result['research_data']['serp'].get('paa_questions', []))}
   - Community Insights: {result['metrics']['reddit_insights_used']}
   - Content Gaps Filled: {result['metrics']['content_gaps_addressed']}
━━━━━━━━━━━━━━━━━━━━
            """)
            
            # Show preview - safely extract content after frontmatter
            try:
                lines = result['content'].split('\n')
                # Find the second --- (end of frontmatter)
                frontmatter_end = 0
                dash_count = 0
                for i, line in enumerate(lines):
                    if line.strip() == '---':
                        dash_count += 1
                        if dash_count == 2:
                            frontmatter_end = i + 1
                            break

                # Extract preview from content body (skip empty lines)
                preview_lines = []
                for line in lines[frontmatter_end:]:
                    if line.strip() and not line.strip().startswith('import ') and not line.strip().startswith('<script'):
                        preview_lines.append(line)
                        if len('\n'.join(preview_lines)) > 200:
                            break

                content_preview = '\n'.join(preview_lines)
                if len(content_preview) > 200:
                    content_preview = content_preview[:200] + "..."

                if content_preview:
                    print(f"Preview:\n{content_preview}\n")
                else:
                    print("Preview: [Content body is empty or could not be extracted]\n")
            except Exception as preview_error:
                print(f"Preview: [Could not generate preview: {preview_error}]\n")
            
        else:
            err_msg = result.get('error', 'Unknown error')
            if is_json:
                print(json.dumps({"success": False, "error": err_msg}, indent=2))
            else:
                print(f"❌ Generation failed: {err_msg}")
            sys.exit(1)
            
    except Exception as e:
        if is_json:
            print(json.dumps({"success": False, "error": str(e)}, indent=2))
        else:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
