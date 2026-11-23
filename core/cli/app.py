"""Click CLI Application for V2 Flyweel Content Engine

Provides interactive mode with beautiful Rich output and
Questionary prompts for content generation.
"""
import asyncio
import sys
from functools import wraps
from pathlib import Path
from typing import Any, Optional
import click

# Allow nested event loops (questionary uses asyncio internally)
import nest_asyncio
nest_asyncio.apply()

from .output import OutputManager, OutputMode
from .progress import ProgressManager
from ..user_note import NoteBundle
from ..formatter import build_seo_slug, restamp_slug
from .prompts import (
    prompt_keyword,
    prompt_style,
    prompt_publish_decision,
    prompt_draft_status,
    prompt_overwrite,
    prompt_continue_generation,
    prompt_research_options,
    prompt_brand_mentions,
    prompt_flyweel_mode,
    prompt_custom_slug,
    prompt_solution_count,
    prompt_icp_injection,
    prompt_solution_selection,
    prompt_competitor_strategy,
    prompt_manual_competitors,
    prompt_confirm_competitors,
    prompt_interpretation_feedback,
)


def async_command(f):
    """Decorator to run async Click commands with proper cleanup"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            result = asyncio.run(f(*args, **kwargs))
            if isinstance(result, int):
                sys.exit(result)
            return result
        except KeyboardInterrupt:
            click.echo("\n[Cancelled]")
            sys.exit(130)
        except SystemExit:
            raise
        except Exception as e:
            click.echo(f"[Error] {e}", err=True)
            sys.exit(1)
    return wrapper


async def _handle_competitor_selection(
    keyword: str,
    style: str,
    competitors_flag: Optional[str],
    output: 'OutputManager',
    progress: 'ProgressManager',
) -> list:
    """Handle competitor selection with multiple input methods.

    This function GUARANTEES that:
    1. User always sees what competitors will be used
    2. User can always modify the list
    3. Failures are visible, not silent

    Args:
        keyword: Target keyword
        style: Content style
        competitors_flag: Value of --comp flag (comma-separated)
        output: Output manager for display
        progress: Progress manager for spinners

    Returns:
        List of approved competitor names
    """
    from ..ai_router import SmartAIRouter

    async with SmartAIRouter() as ai_router:
        return await _run_competitor_selection_flow(
            ai_router=ai_router,
            keyword=keyword,
            style=style,
            competitors_flag=competitors_flag,
            output=output,
            progress=progress,
        )


async def _run_competitor_selection_flow(
    *,
    ai_router: Any,
    keyword: str,
    style: str,
    competitors_flag: Optional[str],
    output: OutputManager,
    progress: ProgressManager,
) -> list:
    competitors = []

    # ─────────────────────────────────────────────────────────────────────
    # Path 1: --comp flag provided (explicit competitors)
    # ─────────────────────────────────────────────────────────────────────
    if competitors_flag:
        # Parse comma-separated
        competitors = [
            {'name': c.strip(), 'source': 'Manual (--comp)', 'confidence': 1.0}
            for c in competitors_flag.split(',')
            if c.strip()
        ]
        output.success(f"Using {len(competitors)} competitors from --comp flag")

    # ─────────────────────────────────────────────────────────────────────
    # Path 2: Interactive mode - ask user how to specify competitors
    # ─────────────────────────────────────────────────────────────────────
    else:
        strategy = prompt_competitor_strategy()

        if strategy is None:
            raise KeyboardInterrupt("User cancelled competitor selection")

        if strategy == 'manual':
            # User types competitors directly
            names = prompt_manual_competitors()
            if not names:
                output.competitor_error("No competitors entered", recoverable=True)
                names = prompt_manual_competitors()
            competitors = [
                {'name': name, 'source': 'Manual', 'confidence': 1.0}
                for name in names
            ]

        elif strategy == 'ai':
            # AI discovers competitors
            with progress.phase('discovery', 'Finding competitors via AI...'):
                competitors = await ai_router.discover_competitors_intelligent(
                    keyword=keyword,
                    count=10
                )
            if not competitors:
                output.competitor_error("AI couldn't find competitors", recoverable=True)
                names = prompt_manual_competitors()
                competitors = [
                    {'name': name, 'source': 'Manual (fallback)', 'confidence': 1.0}
                    for name in names
                ]

        elif strategy == 'both':
            # AI first, then user can add more
            with progress.phase('discovery', 'Finding competitors via AI...'):
                competitors = await ai_router.discover_competitors_intelligent(
                    keyword=keyword,
                    count=8
                )

            # Show what AI found and let user add more
            existing_names = [c['name'] for c in competitors]
            output.info(f"AI found {len(competitors)} competitors. You can add more:")
            added_names = prompt_manual_competitors(existing_names)

            # Merge manual additions (new ones only)
            for name in added_names:
                if name not in existing_names:
                    competitors.append({
                        'name': name,
                        'source': 'Manual',
                        'confidence': 1.0
                    })

    # ─────────────────────────────────────────────────────────────────────
    # MANDATORY: Confirmation loop (user can refine until satisfied)
    # ─────────────────────────────────────────────────────────────────────
    while True:
        if not competitors:
            output.error("No competitors specified. Cannot proceed.")
            names = prompt_manual_competitors()
            if not names:
                raise KeyboardInterrupt("No competitors provided")
            competitors = [
                {'name': name, 'source': 'Manual', 'confidence': 1.0}
                for name in names
            ]
            continue

        confirmation = prompt_confirm_competitors(competitors, keyword, output=output)

        if confirmation['cancel']:
            raise KeyboardInterrupt("User cancelled competitor selection")

        if confirmation['proceed']:
            break

        if confirmation['add_more']:
            existing_names = [c['name'] for c in competitors]
            added = prompt_manual_competitors(existing_names)
            for name in added:
                if name not in existing_names:
                    competitors.append({
                        'name': name,
                        'source': 'Manual',
                        'confidence': 1.0
                    })

        if confirmation.get('override'):
            output.info("Overriding competitor list — enter new entries (current list discarded):")
            new_names = prompt_manual_competitors()
            if new_names:
                competitors = [
                    {'name': name, 'source': 'Manual (override)', 'confidence': 1.0}
                    for name in new_names
                ]
            else:
                output.competitor_error("No competitors entered; keeping existing list.", recoverable=True)

        if confirmation['find_alternatives']:
            with progress.phase('discovery', 'Finding alternative competitors...'):
                existing_names = [c['name'] for c in competitors]
                new_competitors = await ai_router.discover_competitors_intelligent(
                    keyword=keyword,
                    existing_competitors=existing_names,
                    count=5
                )
                competitors.extend(new_competitors)
                if new_competitors:
                    output.success(f"Found {len(new_competitors)} additional competitors")
                else:
                    output.warning("Couldn't find more alternatives")

    # Log final selection
    final_names = [c['name'] for c in competitors]
    output.success(f"Proceeding with {len(final_names)} competitors")

    return final_names


@click.command()
@click.option('-k', '--keyword', help='Keyword to generate content for')
@click.option('--style', type=click.Choice([
    'standard', 'guide', 'comparison', 'research', 'news', 'category', 'top-compare', 'feature'
]), help='Content style')
@click.option('--solutions', type=click.Choice(['5', '8', '10', '15']), default='8',
              help='Number of solutions for top-compare style (5/8/10/15)')
@click.option('--nr', '--no-reddit', 'skip_community', is_flag=True,
              help='Skip Reddit/Quora research')
@click.option('--nrl', '--limited', 'limit_community', is_flag=True,
              help='Limited: 3 Reddit + 1 Quora')
@click.option('--nfw', '--no-flyweel', 'no_flyweel', is_flag=True,
              help='No Flyweel brand mentions (educational only)')
@click.option('--lfw', '--limited-flyweel', 'limited_flyweel', is_flag=True,
              help='Limited Flyweel mentions (2-3 highly natural mentions only)')
@click.option('--no-icp', '--skip-icp', 'skip_icp', is_flag=True,
              help='Skip ICP context (no industry-specific audience targeting)')
@click.option('--augment-serp/--no-augment-serp', 'augment_serp', default=True,
              help='Augment with SERP analysis (enabled by default, uses deep research for research style)')
@click.option('--gsc-check/--no-gsc-check', 'gsc_check', default=True,
              help='GSC cannibalization check (enabled by default)')
@click.option('--gsc-keywords/--no-gsc-keywords', 'gsc_keywords', default=True,
              help='GSC-derived keywords for title/H2 (enabled by default)')
@click.option('--min-tools', 'min_tools', type=int, default=12,
              help='Minimum tools for comparison content (default: 12)')
@click.option('--depth', type=click.Choice(['quick', 'standard', 'comprehensive']),
              default='standard', help='Content depth: quick/standard/comprehensive')
@click.option('--publish', 'auto_publish', is_flag=True,
              help='Auto-publish to connected site repository (skip prompt)')
@click.option('--local', 'save_local', is_flag=True,
              help='Save locally only (skip prompt)')
@click.option('--no-draft', 'no_draft', is_flag=True,
              help='Publish without draft status')
@click.option('--json', 'json_output', is_flag=True,
              help='Output as JSON (for automation)')
@click.option('--comp', '--competitors', 'competitors',
              help='Comma-separated competitor names (e.g., "Windsor MCP, Pipeboard MCP")')
@click.option('--nc', '--no-competitors', 'no_competitors', is_flag=True,
              help='No competitors: skip competitor selection entirely (no discovery, no prompt)')
@click.option('--use-llama-polish', 'use_llama_polish', is_flag=True,
              help='Polish with the creative model instead of the tier polish model (legacy)')
@click.option('--save-research', 'save_research', is_flag=True,
              help='Save research data to output folder')
@click.option('--judge', 'run_judge', is_flag=True,
              help='Add LLM-judge subjective quality dimensions to the scorecard')
@click.option('--output', 'output_dir', default='output',
              help='Output directory for local saves')
@click.option('--verbose', '-v', 'verbose', is_flag=True,
              help='Show detailed debug logs')
@click.option('--note', 'note', default=None,
              help='Freeform note/context to bias generation (non-interactive global note; '
                   'in interactive mode press Tab on a step to attach a per-step note)')
@click.pass_context
@async_command
async def interactive_generate(
    ctx,
    keyword: Optional[str],
    style: Optional[str],
    solutions: str,
    skip_community: bool,
    limit_community: bool,
    no_flyweel: bool,
    limited_flyweel: bool,
    skip_icp: bool,
    augment_serp: bool,
    gsc_check: bool,
    gsc_keywords: bool,
    min_tools: int,
    depth: str,
    auto_publish: bool,
    save_local: bool,
    no_draft: bool,
    json_output: bool,
    competitors: Optional[str],
    no_competitors: bool,
    use_llama_polish: bool,
    save_research: bool,
    run_judge: bool,
    output_dir: str,
    verbose: bool,
    note: Optional[str],
):
    """Interactive content generation with prompts and progress

    Run without arguments for full interactive mode, or provide
    options to skip specific prompts.
    """
    # Configure logging (suppress noise unless --verbose)
    from ..generator import configure_logging
    configure_logging(verbose=verbose)

    # Initialize output manager
    mode = OutputMode.JSON if json_output else OutputMode.RICH
    output = OutputManager(mode=mode)

    # Note channel: --note seeds the global note; interactive Tab adds
    # per-step tagged notes onto the same bundle as prompts run.
    note_bundle = NoteBundle.from_global(note)

    # ─────────────────────────────────────────────────────────────────
    # Step 1: Gather inputs interactively
    # ─────────────────────────────────────────────────────────────────

    # Keyword
    if not keyword:
        keyword = prompt_keyword(bundle=note_bundle)
        if not keyword:
            output.error("No keyword provided. Exiting.")
            return 1

    # Style
    if not style:
        style = prompt_style(bundle=note_bundle)
        if not style:
            output.error("No style selected. Exiting.")
            return 1

    # Solution count for top-compare style
    solution_count = int(solutions)  # Convert from string
    if style == 'top-compare':
        solution_count = prompt_solution_count(default=solution_count)

    # Research options (if not already set)
    if not skip_community and not limit_community:
        research_opts = prompt_research_options(bundle=note_bundle)
        skip_community = research_opts.get('skip_community', False)
        limit_community = research_opts.get('limit_community', False)

    # Brand mentions - convert flags to flyweel_mode string
    # Priority: --nfw > --lfw > interactive prompt > default (full)
    if no_flyweel:
        flyweel_mode = 'none'
    elif limited_flyweel:
        flyweel_mode = 'limited'
    else:
        # If no flag provided, ask interactively with new three-tier prompt
        flyweel_mode = prompt_flyweel_mode(bundle=note_bundle)

    # ICP context injection (if not already skipped via flag)
    if not skip_icp:
        include_icp = prompt_icp_injection(bundle=note_bundle)
        skip_icp = not include_icp

    # Human-confirmed topic interpretation — one authoritative framing for the
    # whole pipeline, confirmed (or corrected) by the user before generation.
    keyword_context = await _confirm_keyword_context(keyword, style, output)

    # ─────────────────────────────────────────────────────────────────
    # Step 2: Show config panel
    # ─────────────────────────────────────────────────────────────────

    # Map depth to solution count while respecting an explicit --min-tools override
    depth_to_solutions = {'quick': 8, 'standard': 12, 'comprehensive': 15}
    min_tools_explicit = (
        ctx is not None
        and hasattr(ctx, 'get_parameter_source')
        and ctx.get_parameter_source('min_tools') == click.core.ParameterSource.COMMANDLINE
    )
    depth_count = depth_to_solutions.get(depth, min_tools)
    effective_solution_count = max(min_tools, depth_count) if min_tools_explicit else depth_count
    if style == 'top-compare':
        effective_solution_count = solution_count

    config = {
        'keyword': keyword,
        'style': style,
        'context_source': 'site context',
        'skip_community': skip_community,
        'limit_community': limit_community,
        'flyweel_mode': flyweel_mode,
        'skip_icp': skip_icp,
        'gsc_check': gsc_check,
        'gsc_keywords': gsc_keywords,
        'depth': depth,
        'keyword_context': keyword_context,
        'publish_target': 'astro' if auto_publish else ('local' if save_local else 'prompt'),
    }
    if style in ['top-compare', 'comparison', 'category']:
        config['solution_count'] = effective_solution_count
    output.config_panel(config)

    # Confirm before starting
    if not auto_publish and not save_local:
        if not prompt_continue_generation():
            output.info("Generation cancelled.")
            return 0

    # ─────────────────────────────────────────────────────────────────
    # Step 3: Run generation with progress tracking
    # ─────────────────────────────────────────────────────────────────

    progress = ProgressManager(output=output)

    with progress.full_pipeline():
        try:
            result = await _run_generation(
                keyword=keyword,
                style=style,
                skip_community=skip_community,
                limit_community=limit_community,
                flyweel_mode=flyweel_mode,
                skip_icp=skip_icp,
                augment_serp=augment_serp,
                solution_count=effective_solution_count,
                gsc_check=gsc_check,
                gsc_keywords=gsc_keywords,
                progress=progress,
                output=output,
                competitors=competitors,
                no_competitors=no_competitors,
                use_llama_polish=use_llama_polish,
                note_bundle=note_bundle,
                keyword_context=keyword_context,
                depth=depth,
                run_judge=run_judge,
            )
        except Exception as e:
            err_text = str(e) or type(e).__name__
            progress.fail_pipeline(err_text)
            output.error(f"Generation failed: {err_text}")
            output.final_result({'error': err_text, 'keyword': keyword, 'style': style}, success=False)
            return 1

        if not result.get('success'):
            err_text = result.get('error', 'Unknown error')
            progress.fail_pipeline(err_text)
            output.error(f"Generation failed: {err_text}")
            output.final_result({'error': err_text, 'keyword': keyword, 'style': style}, success=False)
            return 1

        # ─────────────────────────────────────────────────────────────────
        # Step 4: Show result
        # ─────────────────────────────────────────────────────────────────

        output.generation_result(
            title=result.get('title', 'Untitled'),
            word_count=result.get('metrics', {}).get('word_count', 0),
            generation_time=result.get('metrics', {}).get('generation_time', 0),
            slug=result.get('slug', ''),
        )

        # Let the user confirm/edit the proposed slug before it is locked into
        # the URL. Only in interactive mode (auto-publish / save-local skip it).
        # Restamps the canonical URL + hero path in content so everything stays
        # consistent with the publish filename and output folder.
        if not auto_publish and not save_local:
            _apply_slug_choice(result, prompt_custom_slug(result.get('slug', '')))

        # Display full article content for review
        output.article_preview(
            content=result.get('content', ''),
            title=result.get('title', 'Generated Article')
        )

        # ─────────────────────────────────────────────────────────────────
        # Step 5: Handle output decision
        # ─────────────────────────────────────────────────────────────────

        # Determine publish decision
        if auto_publish:
            decision = 'publish'
        elif save_local:
            decision = 'local'
        else:
            decision = prompt_publish_decision()
            if not decision:
                progress.update("Output skipped: preview only", advance=1)
                output.info("No output selected. Content discarded.")
                return 0

        publish_failed_error: Optional[str] = None
        save_failed_error: Optional[str] = None
        local_saved_paths: Optional[dict] = None

        async def _try_save_local(error_prefix: str, done_msg: Optional[str] = None) -> None:
            nonlocal local_saved_paths, save_failed_error
            try:
                local_saved_paths = await _save_local(result, output, output_dir=output_dir, save_research=save_research)
                if done_msg:
                    progress.update(done_msg, advance=1)
            except Exception as save_exc:
                save_failed_error = str(save_exc) or type(save_exc).__name__
                progress.fail_pipeline(save_failed_error)
                output.error(f"{error_prefix}: {save_failed_error}")

        if decision == 'publish':
            if no_draft:
                draft_flag = False
            elif auto_publish:
                draft_flag = True
            else:
                draft_ans = prompt_draft_status()
                draft_flag = True if draft_ans is None else bool(draft_ans)
            progress.update("Publishing to connected site...", advance=0)
            try:
                publish_result = await _publish_to_astro(
                    result=result,
                    draft=draft_flag,
                    output=output,
                )
            except (KeyboardInterrupt, click.Abort):
                raise
            except Exception as publish_exc:
                publish_result = {
                    'success': False,
                    'error': str(publish_exc) or type(publish_exc).__name__,
                }

            if publish_result.get('success'):
                output.publish_result(publish_result)
                progress.update("✓ Published to connected site", advance=1)
                output.success("Content published successfully!")
                await _try_save_local("Local archive save failed")
            else:
                publish_failed_error = str(publish_result.get('error') or 'Publish failed')
                progress.fail_pipeline(publish_failed_error)
                output.error(f"Publish failed: {publish_failed_error}")
                output.info("Falling back to local save...")
                progress.update("Publish failed, saving locally...", advance=0)
                await _try_save_local("Fallback local save failed", "✓ Saved locally after publish fallback")
                decision = 'local_fallback'

        elif decision == 'local':
            progress.update("Saving generated content locally...", advance=0)
            await _try_save_local("Local save failed", "✓ Saved generated content locally")

        else:  # preview
            progress.update("Output skipped: preview only", advance=1)
            output.info("Content discarded (preview only).")

        # Final JSON output if needed
        final_payload: dict = {
            'title': result.get('title'),
            'slug': result.get('slug'),
            'word_count': result.get('metrics', {}).get('word_count'),
            'decision': decision,
        }
        if result.get('content') is not None:
            final_payload['content'] = result.get('content')
        if local_saved_paths:
            final_payload['local_paths'] = local_saved_paths
            final_payload['output_directory'] = local_saved_paths.get('directory')
        if publish_failed_error:
            final_payload['published'] = False
            final_payload['publish_error'] = publish_failed_error
        if save_failed_error:
            final_payload['saved'] = False
            final_payload['save_error'] = save_failed_error
        if publish_failed_error or save_failed_error:
            output.final_result(final_payload, success=False)
            return 1

        output.final_result(final_payload, success=True)
        return 0


async def _confirm_keyword_context(keyword, style, output) -> Optional[str]:
    """Generate a two-sentence topic interpretation and let the user confirm/correct it.

    Loops: shows the AI's interpretation, asks "is this the right context?". If the
    user adds a correction, regenerates honouring it and asks again, until they
    accept (returns the accepted interpretation) or it cannot be produced (None).
    Non-interactive sessions accept the first interpretation without prompting.
    """
    from ..ai_router import SmartAIRouter

    try:
        async with SmartAIRouter() as ai:
            interp = await ai.interpret_keyword_context(keyword, style)
            while interp:
                output.context_interpretation(interp)
                feedback = prompt_interpretation_feedback(interp)
                if feedback is None:
                    return interp  # accepted (or non-interactive)
                interp = await ai.interpret_keyword_context(
                    keyword, style,
                    user_feedback=feedback,
                    previous_interpretation=interp,
                )
            return interp  # None → skip silently, generation continues
    except KeyboardInterrupt:
        raise
    except Exception as e:
        output.warning(f"Context interpretation skipped: {e}")
        return None


async def _run_generation(
    keyword: str,
    style: str,
    skip_community: bool,
    limit_community: bool,
    flyweel_mode: str,
    progress: ProgressManager,
    output: OutputManager,
    skip_icp: bool = False,
    augment_serp: bool = True,
    solution_count: Optional[int] = None,
    gsc_check: bool = False,
    gsc_keywords: bool = False,
    competitors: Optional[str] = None,
    no_competitors: bool = False,
    use_llama_polish: bool = False,
    note_bundle: Optional["NoteBundle"] = None,
    keyword_context: Optional[str] = None,
    depth: str = "standard",
    run_judge: bool = False,
) -> dict:
    """Run the content generation pipeline

    Args:
        keyword: Target keyword
        style: Content style
        skip_community: Skip Reddit/Quora
        limit_community: Limit community research
        flyweel_mode: Flyweel mention level - 'none' (--nfw), 'limited' (--lfw), 'full' (default)
        progress: ProgressManager for tracking
        output: OutputManager for messages
        skip_icp: Skip ICP context injection (generic audience)
        augment_serp: Augment with SERP analysis (uses deep research for research style)
        solution_count: Number of solutions for comparison content
        gsc_check: Run GSC cannibalization check before generation
        gsc_keywords: Use GSC-derived keywords for title/H2 optimization
        competitors: Comma-separated competitor names from --comp flag

    Returns:
        Result dict from ContentGenerator
    """
    from ..repo_extractor import RepoContextExtractor
    from ..generator import ContentGenerator

    # Phase 1: Load context from repo
    with progress.phase('context', 'Loading brand context from repo'):
        extractor = RepoContextExtractor()
        site_context = extractor.extract_context()
        output.info(f"Loaded {len(site_context.get('internal_links', {}))} internal links")

    # Initialize approved_solutions (will be set if comparison-style with interactive selection)
    approved_solutions = None

    # ─────────────────────────────────────────────────────────────────
    # COMPETITOR SELECTION for comparison/top-compare/category styles
    # Uses the new v2 bulletproof flow with --comp flag support
    # ─────────────────────────────────────────────────────────────────
    if style in ['comparison', 'top-compare', 'category']:
        if no_competitors:
            output.info("--nc set: skipping competitor selection entirely")
            approved_solutions = []
        else:
            approved_solutions = await _handle_competitor_selection(
                keyword=keyword,
                style=style,
                competitors_flag=competitors,
                output=output,
                progress=progress,
            )

    # Phase 2-5: Generation (handled by ContentGenerator)
    with progress.phase('generation', 'Generating content with AI'):
        # Create generator with injected context
        generator = ContentGenerator()

        # Inject repo context
        generator._injected_context = site_context

        # Build generation kwargs
        gen_kwargs = {
            'skip_community': skip_community,
            'limit_community': limit_community,
            'flyweel_mode': flyweel_mode,
            'skip_icp': skip_icp,
            'augment_serp': augment_serp,
            'gsc_check': gsc_check,
            'gsc_keywords': gsc_keywords,
            'use_llama_polish': use_llama_polish,
            'depth': depth,
            'run_judge': run_judge,
            'competitor_mentions': not no_competitors,
        }

        # Add solution_count for comparison-style content
        if solution_count and style in ['top-compare', 'comparison', 'category']:
            gen_kwargs['solution_count'] = solution_count

        # Inject approved solutions if available
        if approved_solutions:
            gen_kwargs['approved_solutions'] = approved_solutions

        # Thread user note bundle (interactive per-step or --note global)
        if note_bundle is not None and not note_bundle.is_empty():
            gen_kwargs['note_bundle'] = note_bundle

        # Thread the human-confirmed topic interpretation into generation.
        if keyword_context:
            gen_kwargs['keyword_context'] = keyword_context

        # Pass progress callback to generator for real-time updates
        from .progress import ProgressCallback
        progress_cb = ProgressCallback(progress)
        gen_kwargs['progress_callback'] = progress_cb

        # Run generation
        result = await generator.generate(keyword, style, **gen_kwargs)

    return result


def _apply_slug_choice(result: dict, chosen: Optional[str]) -> None:
    """Apply a user-chosen slug to ``result`` in place, keeping everything consistent.

    Sanitises ``chosen`` through ``build_seo_slug`` and, when it differs from the
    current slug, restamps the canonical URL + hero path inside ``result['content']``
    so the publish filename, output folder, and in-file URLs never diverge.
    No-op when the choice is empty or unchanged.
    """
    if not chosen:
        return
    clean = build_seo_slug(chosen)
    current = result.get('slug', '')
    if not clean or clean == current:
        return
    result['content'] = restamp_slug(result.get('content', ''), current, clean)
    result['slug'] = clean


async def _publish_to_astro(
    result: dict,
    draft: bool,
    output: OutputManager,
) -> dict:
    """Publish generated content to connected site repository

    Args:
        result: Generation result with content
        draft: Set draft status
        output: OutputManager for messages

    Returns:
        Publisher result dict
    """
    from ..publisher import AstroPublisher

    publisher = AstroPublisher()
    slug = result.get('slug', '')

    # Check for existing file
    existing = publisher.get_existing_slugs()
    overwrite = False
    if slug in existing:
        overwrite = prompt_overwrite(slug)
        if not overwrite:
            # Let user choose a different slug; restamp content so the canonical
            # URL + hero path match the new publish filename.
            _apply_slug_choice(result, prompt_custom_slug(slug))
            slug = result.get('slug', '')

    return publisher.publish(
        content=result.get('content', ''),
        slug=slug,
        draft=draft,
        overwrite=overwrite,
    )


async def _save_local(
    result: dict,
    output: OutputManager,
    output_dir: str = 'output',
    save_research: bool = False,
) -> dict:
    """Save content to local output directory and return saved artifact paths."""
    from ..artifacts import save_generation_artifacts

    paths = save_generation_artifacts(
        result, output_dir=output_dir, save_research=save_research, overwrite=False)
    generation_dir = Path(paths['directory'])
    research_file = Path(paths['metadata'])
    _composite = (result.get('quality_scorecard') or {}).get('composite')
    output.info(f"Metadata saved: {research_file}" + (f" (quality {_composite}/100)" if _composite is not None else ""))

    output.success(f"Saved to: {generation_dir}/")
    return paths


# CLI entry point
cli = interactive_generate


if __name__ == "__main__":
    cli()
