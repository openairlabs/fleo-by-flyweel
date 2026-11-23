import {describe, expect, it} from 'vitest';

import {buildBridgeArgs, buildSuggestArgs, parseBridgeEventLine, replayFixture, stripTerminalEscapes} from '../../tui/bridge.js';
import {bindingsFor} from '../../tui/keymap.js';
import {buildPreviewModel} from '../../tui/preview.js';
import {applyCommand, createInitialState, reduceBridgeEvent, reducer} from '../../tui/state.js';
import type {BridgeEvent} from '../../tui/types.js';

const event = (type: BridgeEvent['type'], patch: Partial<BridgeEvent> = {}): BridgeEvent => ({
  type,
  runId: 'run_test',
  timestamp: '2026-06-22T00:00:00Z',
  ...patch,
});

const finalArtifact = {id: 'final', data: {artifact: 'final_mdx', content: '---\ntitle: T\n---\nBody'}};

describe('TUI reducer bridge events', () => {
  it('handles setup_status without touching progress or the step', () => {
    const state = reduceBridgeEvent(
      createInitialState({step: 'setup'}),
      event('setup_status', {
        phase: 'setup',
        data: {checks: [], canGenerate: true, requiredMissing: [], optionalMissing: []},
      }),
    );
    expect(state.setupStatus?.canGenerate).toBe(true);
    expect(state.step).toBe('setup');
    expect(state.progressRows).toHaveLength(0);
  });

  it('moves config to running on progress events', () => {
    const state = reduceBridgeEvent(createInitialState(), event('phase_started', {phase: 'research', taskId: 'serp', message: 'SERP'}));
    expect(state.step).toBe('running');
    expect(state.progressRows[0]?.phase).toBe('research');
  });

  it('records tool calls in the log', () => {
    const state = reduceBridgeEvent(createInitialState(), event('tool_call', {phase: 'research', taskId: 'reddit', message: 'Reddit mining'}));
    expect(state.log).toHaveLength(1);
    expect(state.log[0]?.level).toBe('tool');
    expect(state.log[0]?.text).toContain('Reddit mining');
  });

  it('routes warnings to notifications + log, never progress rows', () => {
    const state = reduceBridgeEvent(createInitialState(), event('warning', {message: 'Heads up'}));
    expect(state.notifications.at(-1)?.message).toBe('Heads up');
    expect(state.log.at(-1)?.level).toBe('warn');
    expect(state.progressRows).toHaveLength(0);
    expect(state.step).toBe('config');
  });

  it('ends the run at output on a mid-run error', () => {
    let state = createInitialState({step: 'running'});
    state = reduceBridgeEvent(state, event('error', {message: 'Failed'}));
    expect(state.step).toBe('output');
    expect(state.outcome).toBe('error');
    expect(state.lastError).toBe('Failed');
    expect(state.notifications.at(-1)?.message).toBe('Failed');
  });

  it('keeps config-time errors as notifications only', () => {
    const state = reduceBridgeEvent(createInitialState(), event('error', {phase: 'config', message: 'Topic interpretation failed'}));
    expect(state.step).toBe('config');
    expect(state.outcome).toBeUndefined();
    expect(state.notifications.at(-1)?.message).toBe('Topic interpretation failed');
    expect(state.log.at(-1)?.level).toBe('error');
  });

  it('handles artifact and review gate lifecycle', () => {
    const opened = reduceBridgeEvent(
      createInitialState({step: 'running'}),
      event('review_gate_opened', {phase: 'review', taskId: 'gate', data: {queuedComments: ['tighten proof']}}),
    );
    const artifact = reduceBridgeEvent(opened, event('artifact_ready', {taskId: 'pre-polish', data: {content: 'mdx'}}));
    const closed = reduceBridgeEvent(artifact, event('review_gate_closed', {phase: 'review', taskId: 'gate'}));
    expect(opened.step).toBe('review');
    expect(opened.commandMode).toBe('review');
    expect(opened.queuedComments).toEqual(['tighten proof']);
    expect(artifact.artifacts).toHaveLength(1);
    expect(closed.step).toBe('running');
    expect(closed.commandMode).toBe(false);
  });

  it('handles the gsc gate lifecycle as a confirm strip, not a journey step', () => {
    const opened = reduceBridgeEvent(
      createInitialState({step: 'running'}),
      event('gsc_gate_opened', {phase: 'gsc', data: {score: 12, threshold: 25, trafficPotential: 1.4, primaryQuery: 'kw variant'}}),
    );
    expect(opened.step).toBe('running');
    expect(opened.gscGate).toEqual({score: 12, threshold: 25, trafficPotential: 1.4, primaryQuery: 'kw variant'});
    expect(opened.notifications.at(-1)?.tone).toBe('warning');
    const closed = reduceBridgeEvent(opened, event('gsc_gate_closed', {phase: 'gsc', message: 'GSC gate decision: proceed'}));
    expect(closed.gscGate).toBeUndefined();
    expect(closed.log.at(-1)?.text).toContain('proceed');
  });

  it('parses the additive estimated/neighborCount gsc gate fields when present', () => {
    const opened = reduceBridgeEvent(
      createInitialState({step: 'running'}),
      event('gsc_gate_opened', {
        phase: 'gsc',
        data: {score: 8, threshold: 10, estimated: true, neighborCount: 4},
      }),
    );
    expect(opened.gscGate?.score).toBe(8);
    expect(opened.gscGate?.threshold).toBe(10);
    expect(opened.gscGate?.estimated).toBe(true);
    expect(opened.gscGate?.neighborCount).toBe(4);
  });

  it('tolerates old-bridge gsc gate payloads without the additive fields', () => {
    const opened = reduceBridgeEvent(
      createInitialState({step: 'running'}),
      event('gsc_gate_opened', {phase: 'gsc', data: {score: 12, threshold: 25, trafficPotential: 1.4}}),
    );
    expect(opened.gscGate?.score).toBe(12);
    expect(opened.gscGate?.estimated).toBeUndefined();
    expect(opened.gscGate?.neighborCount).toBeUndefined();
    // Bridges may also send neighborCount: null explicitly — never crash, never keep null.
    const nullCount = reduceBridgeEvent(
      createInitialState({step: 'running'}),
      event('gsc_gate_opened', {phase: 'gsc', data: {score: 3, threshold: 10, estimated: true, neighborCount: null}}),
    );
    expect(nullCount.gscGate?.estimated).toBe(true);
    expect(nullCount.gscGate?.neighborCount).toBeUndefined();
  });

  it('clears the gsc gate on bridge exit and new_run', () => {
    const withGate = reduceBridgeEvent(
      createInitialState({step: 'running'}),
      event('gsc_gate_opened', {phase: 'gsc', data: {score: 5, threshold: 25}}),
    );
    expect(reducer(withGate, {type: 'bridge_exit', code: 1, signal: null}).gscGate).toBeUndefined();
    expect(reducer(withGate, {type: 'new_run'}).gscGate).toBeUndefined();
  });

  it('handles generation complete, saved, and published events', () => {
    let state = createInitialState({step: 'running'});
    state = reduceBridgeEvent(state, event('task_progress', {phase: 'draft', taskId: 'gen', message: 'Part 3'}));
    const preview = reduceBridgeEvent(state, event('generation_complete', {data: {qualityScorecard: {composite: 88}}}));
    const saved = reduceBridgeEvent(preview, event('saved', {data: {final: '/tmp/final.mdx'}}));
    const published = reduceBridgeEvent(saved, event('published'));
    expect(preview.step).toBe('preview');
    expect(preview.generationSummary.qualityScorecard).toEqual({composite: 88});
    expect(preview.progressRows.every(row => row.status !== 'running')).toBe(true);
    expect(saved.step).toBe('output');
    expect(saved.outcome).toBe('complete');
    expect(saved.saveState).toBe('done');
    expect(saved.outputPaths.final).toBe('/tmp/final.mdx');
    expect(published.publishState).toBe('done');
  });

  it('keeps setup saves on the setup step', () => {
    const state = reduceBridgeEvent(
      createInitialState({step: 'setup', setupDraft: {OPENROUTER_API_KEY: 'secret'}}),
      event('saved', {phase: 'setup', message: 'Setup changes saved', data: {updatedKeys: ['OPENROUTER_API_KEY']}}),
    );
    expect(state.step).toBe('setup');
    expect(state.setupDraft).toEqual({});
    expect(state.notifications.at(-1)?.message).toBe('Setup changes saved');
  });

  it('replays fixtures without API calls', () => {
    let state = createInitialState();
    const fixture = [
      event('phase_started', {phase: 'research', message: 'Research'}),
      event('task_progress', {phase: 'draft', taskId: 'part-1', message: 'Part 1'}),
      event('review_gate_opened', {phase: 'review'}),
      event('review_gate_closed', {phase: 'review'}),
      event('generation_complete', {phase: 'preview'}),
    ];
    replayFixture(fixture, bridgeEvent => {
      state = reduceBridgeEvent(state, bridgeEvent);
    });
    expect(state.step).toBe('preview');
    expect(state.progressRows.length).toBeGreaterThan(0);
  });

  it('replays review gate fallback fixtures', () => {
    let state = createInitialState({step: 'running', queuedComments: ['Keep citations stable']});
    replayFixture(
      [
        event('review_gate_opened', {phase: 'review', data: {queuedComments: ['Add a proof point']}}),
        event('task_progress', {phase: 'review', taskId: 'comment-edit', message: 'Applying review comments before final polish'}),
        event('warning', {phase: 'review', taskId: 'comment-edit', message: 'Review comment edit broke MDX; kept pre-comment content'}),
        event('review_gate_closed', {phase: 'review', data: {decision: 'apply_comments', commentCount: 2}}),
      ],
      bridgeEvent => {
        state = reduceBridgeEvent(state, bridgeEvent);
      },
    );
    expect(state.queuedComments).toEqual(['Keep citations stable', 'Add a proof point']);
    expect(state.log.some(entry => entry.text.includes('broke MDX'))).toBe(true);
    expect(state.step).toBe('running');
  });

  it('stores topic interpretation artifacts in config state and marks the interpretation done', () => {
    const state = reduceBridgeEvent(
      createInitialState({interpretState: 'running', interpretAttempt: 1}),
      event('artifact_ready', {
        phase: 'config',
        taskId: 'topic-interpretation',
        data: {artifact: 'topic_interpretation', context: 'Confirmed topic context'},
      }),
    );
    expect(state.config.keywordContext).toBe('Confirmed topic context');
    expect(state.interpretState).toBe('done');
  });

  it('marks the interpretation failed on a topic-interpretation error without leaving the context step', () => {
    const state = reduceBridgeEvent(
      createInitialState({step: 'context', interpretState: 'running', interpretAttempt: 1}),
      event('error', {phase: 'config', taskId: 'topic-interpretation', message: 'Topic interpretation failed: boom'}),
    );
    expect(state.step).toBe('context');
    expect(state.interpretState).toBe('failed');
    expect(state.notifications.at(-1)?.message).toContain('boom');
  });

  it('passes keyword context and competitors to bridge args', () => {
    const config = {...createInitialState().config, keyword: 'pipeline attribution', keywordContext: 'Confirmed context', competitors: 'Alpha, Beta'};
    const args = buildBridgeArgs(config, 'run_test');
    expect(args).toContain('--keyword-context');
    expect(args).toContain('Confirmed context');
    expect(args).toContain('--competitors');
    expect(args).toContain('Alpha, Beta');
  });

  it('never auto-publishes from the TUI generation subprocess', () => {
    const config = {...createInitialState().config, keyword: 'pipeline attribution', saveDefault: 'publish' as const};
    expect(buildBridgeArgs(config, 'run_test')).not.toContain('--publish');
  });

  it('maps competitorMentions=false to --no-competitors for non-compare styles only', () => {
    const base = {...createInitialState().config, keyword: 'crm tools'};
    expect(buildBridgeArgs({...base, style: 'guide' as const, competitorMentions: false}, 'run_test')).toContain('--no-competitors');
    expect(buildBridgeArgs({...base, style: 'comparison' as const, competitorMentions: false}, 'run_test')).not.toContain('--no-competitors');
    expect(buildBridgeArgs({...base, style: 'top-compare' as const, competitorMentions: false}, 'run_test')).not.toContain('--no-competitors');
    expect(buildBridgeArgs({...base, style: 'guide' as const, competitorMentions: true}, 'run_test')).not.toContain('--no-competitors');
  });
});

describe('bridge_exit outcome mapping', () => {
  it('maps a clean exit during a run to output/complete', () => {
    const state = reducer(createInitialState({step: 'running'}), {type: 'bridge_exit', code: 0, signal: null});
    expect(state.step).toBe('output');
    expect(state.outcome).toBe('complete');
  });

  it('maps 130 and SIGTERM to cancelled', () => {
    const viaCode = reducer(createInitialState({step: 'running'}), {type: 'bridge_exit', code: 130, signal: null});
    const viaSignal = reducer(createInitialState({step: 'review'}), {type: 'bridge_exit', code: null, signal: 'SIGTERM'});
    expect(viaCode.outcome).toBe('cancelled');
    expect(viaSignal.step).toBe('output');
    expect(viaSignal.outcome).toBe('cancelled');
  });

  it('maps non-zero exits to error with a fallback message', () => {
    const state = reducer(createInitialState({step: 'running'}), {type: 'bridge_exit', code: 1, signal: null});
    expect(state.outcome).toBe('error');
    expect(state.lastError).toContain('exited');
  });

  it('leaves settled steps alone on exit', () => {
    const preview = reducer(createInitialState({step: 'preview'}), {type: 'bridge_exit', code: 0, signal: null});
    const setup = reducer(createInitialState({step: 'setup'}), {type: 'bridge_exit', code: 0, signal: null});
    expect(preview.step).toBe('preview');
    expect(setup.step).toBe('setup');
    expect(setup.completedAt).toBeTypeOf('number');
  });
});

describe('journey actions', () => {
  it('toggles help and log overlays exclusively', () => {
    let state = reducer(createInitialState(), {type: 'toggle_help'});
    expect(state.helpOpen).toBe(true);
    state = reducer(state, {type: 'toggle_log'});
    expect(state.logOpen).toBe(true);
    expect(state.helpOpen).toBe(false);
    state = reducer(state, {type: 'escape'});
    expect(state.logOpen).toBe(false);
  });

  it('opens and submits comments through semantic actions', () => {
    let state = reducer(createInitialState({step: 'running'}), {type: 'open_comment'});
    expect(state.commandMode).toBe('comment');
    state = reducer(state, {type: 'comment_submitted', text: 'Add source detail'});
    expect(state.queuedComments).toEqual(['Add source detail']);
    expect(state.commandMode).toBe(false);
    state = reducer(state, {type: 'open_comment'});
    state = reducer(state, {type: 'comment_submitted', text: '   '});
    expect(state.queuedComments).toEqual(['Add source detail']);
  });

  it('guards goto_preview and goto_output by step and artifacts', () => {
    const noArtifacts = reducer(createInitialState({step: 'output'}), {type: 'goto_preview'});
    expect(noArtifacts.step).toBe('output');

    const withArtifacts = reducer(createInitialState({step: 'output', artifacts: [finalArtifact]}), {type: 'goto_preview'});
    expect(withArtifacts.step).toBe('preview');

    const fromPreview = reducer(createInitialState({step: 'preview'}), {type: 'goto_output'});
    expect(fromPreview.step).toBe('output');
    expect(fromPreview.outcome).toBe('complete');

    const fromRunning = reducer(createInitialState({step: 'running'}), {type: 'goto_output'});
    expect(fromRunning.step).toBe('running');
  });

  it('arms publish only when a final artifact exists and disarms on escape', () => {
    const unarmed = reducer(createInitialState({step: 'output'}), {type: 'arm_publish'});
    expect(unarmed.publishArmed).toBe(false);

    let armed = reducer(createInitialState({step: 'output', artifacts: [finalArtifact]}), {type: 'arm_publish'});
    expect(armed.publishArmed).toBe(true);
    armed = reducer(armed, {type: 'escape'});
    expect(armed.publishArmed).toBe(false);
  });

  it('resets run-scoped state (but not config or counters) on new_run', () => {
    let state = createInitialState({
      step: 'output',
      outcome: 'complete',
      runId: 'run_1',
      artifacts: [finalArtifact],
      queuedComments: ['note'],
      outputPaths: {final: '/tmp/final.mdx'},
      runRequestId: 3,
      config: {keyword: 'pipeline attribution', style: 'research'},
    });
    state = reducer(state, {type: 'log', level: 'info', text: 'stderr line'});
    state = reducer(state, {type: 'new_run'});
    expect(state.step).toBe('config');
    expect(state.configStage).toBe('settings');
    expect(state.outcome).toBeUndefined();
    expect(state.runId).toBeUndefined();
    expect(state.artifacts).toHaveLength(0);
    expect(state.queuedComments).toHaveLength(0);
    expect(state.log).toHaveLength(0);
    expect(state.outputPaths).toEqual({});
    expect(state.config.keyword).toBe('pipeline attribution');
    expect(state.runRequestId).toBe(3);
  });
});

describe('keymap bindings', () => {
  it('filters bindings by step and availability', () => {
    const running = bindingsFor(createInitialState({step: 'running'}));
    expect(running.some(binding => binding.action === 'cancel_run')).toBe(true);
    expect(running.some(binding => binding.action === 'save')).toBe(false);

    const previewNoArtifact = bindingsFor(createInitialState({step: 'preview'}));
    expect(previewNoArtifact.some(binding => binding.action === 'save')).toBe(false);

    const previewReady = bindingsFor(createInitialState({step: 'preview', artifacts: [finalArtifact]}));
    expect(previewReady.some(binding => binding.action === 'save')).toBe(true);
    expect(previewReady.some(binding => binding.action === 'publish')).toBe(true);
  });

  it('returns overlay bindings while help or logs are open', () => {
    const bindings = bindingsFor(createInitialState({step: 'running', helpOpen: true}));
    expect(bindings.some(binding => binding.action === 'cancel_run')).toBe(false);
    expect(bindings.some(binding => binding.label === 'close')).toBe(true);
  });
});

describe('bridge terminal escape sanitization', () => {
  it('strips OSC 52 clipboard writes, OSC 8 links, titles, hidden SGR, and C1 controls while keeping tabs and newlines', () => {
    const raw = JSON.stringify({
      type: 'artifact_ready',
      runId: 'run_1',
      timestamp: '2026-11-15T00:00:00Z',
      message: 'Done \u001b]52;c;cHduZWQ=\u0007\u001b]2;EvilTitle\u0007\u001b[8mhidden\u001b[0m',
      data: {
        artifact: 'final_mdx',
        content: 'Line 1\tcol\n\u001b]8;;https://evil.example\u0007Safe Label\u001b]8;;\u0007\u009d',
      },
    });
    const parsed = parseBridgeEventLine(raw);
    expect(parsed.event?.message).toBe('Done hidden');
    expect((parsed.event?.data as {content: string}).content).toBe('Line 1\tcol\nSafe Label');
    expect(stripTerminalEscapes('not json \u001b]52;c;YWJj\u0007ok')).toBe('not json ok');
  });
});

describe('milestone A fixes', () => {
  it('folds repeated task_progress messages into one row, updating detail in place', () => {
    let state = createInitialState();
    for (const message of ['Part 1', 'Part 2', 'Part 3']) {
      state = reduceBridgeEvent(state, event('task_progress', {phase: 'draft', taskId: 'gen', message}));
    }
    expect(state.progressRows).toHaveLength(1);
    expect(state.progressRows[0]?.detail).toBe('Part 3');
  });

  it('keys taskless progress by phase and settles prior phases on phase_started', () => {
    let state = createInitialState();
    state = reduceBridgeEvent(state, event('phase_started', {phase: 'research', message: 'Researching'}));
    state = reduceBridgeEvent(state, event('task_progress', {phase: 'research', message: 'SERP pass'}));
    state = reduceBridgeEvent(state, event('phase_started', {phase: 'draft', message: 'Drafting'}));
    expect(state.progressRows).toHaveLength(2);
    expect(state.progressRows[0]?.status).toBe('complete');
    expect(state.progressRows[1]?.status).toBe('running');
  });

  it('records log lines without creating notifications', () => {
    const state = reducer(createInitialState(), {type: 'log', level: 'info', text: 'INFO stderr noise'});
    expect(state.log.at(-1)?.text).toBe('INFO stderr noise');
    expect(state.notifications).toHaveLength(0);
  });

  it('notify creates a notification and a log line', () => {
    const state = reducer(createInitialState(), {type: 'notify', tone: 'error', message: 'Save failed'});
    expect(state.notifications.at(-1)?.message).toBe('Save failed');
    expect(state.log.at(-1)?.level).toBe('error');
  });

  it('keeps notification ids unique after the list is trimmed', () => {
    let state = createInitialState();
    for (let index = 0; index < 9; index += 1) {
      state = reducer(state, {type: 'notify', tone: 'info', message: `note ${index}`});
    }
    const ids = state.notifications.map(notification => notification.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(state.notifications.length).toBeLessThanOrEqual(6);
  });

  it('gates request_save/request_publish on a final artifact', () => {
    let empty = createInitialState();
    empty = reducer(empty, {type: 'request_save'});
    expect(empty.saveRequestId).toBe(0);
    expect(empty.notifications.at(-1)?.message).toContain('No final artifact');

    let ready = createInitialState({artifacts: [finalArtifact]});
    ready = reducer(ready, {type: 'request_save'});
    expect(ready.saveRequestId).toBe(1);
    expect(ready.saveState).toBe('running');
    // Output actions are serialized; publish becomes available after save settles.
    ready = reducer(ready, {type: 'action_finished', kind: 'save', ok: true});
    ready = reducer(ready, {type: 'request_publish'});
    expect(ready.publishRequestId).toBe(1);
    expect(ready.publishState).toBe('running');
  });

  it('marks failed actions and notifies', () => {
    let state = createInitialState({artifacts: [finalArtifact]});
    state = reducer(state, {type: 'request_save'});
    state = reducer(state, {type: 'action_finished', kind: 'save', ok: false, message: 'Disk full'});
    expect(state.saveState).toBe('failed');
    expect(state.notifications.at(-1)?.message).toBe('Disk full');
    state = reducer(state, {type: 'request_publish'});
    state = reducer(state, {type: 'action_finished', kind: 'publish', ok: true});
    expect(state.publishState).toBe('done');
  });

  it('opens the cancel confirm only while a run is active', () => {
    const idle = reducer(createInitialState(), {type: 'request_cancel'});
    expect(idle.cancelPending).toBe(false);

    let running = reducer(createInitialState({step: 'running'}), {type: 'request_cancel'});
    expect(running.cancelPending).toBe(true);
    running = reducer(running, {type: 'dismiss_cancel'});
    expect(running.cancelPending).toBe(false);
  });
});

describe('context step lifecycle', () => {
  it('request_context enters the context step and auto-interprets only when no context exists', () => {
    let state = createInitialState({config: {keyword: 'pipeline attribution'}});
    state = reducer(state, {type: 'request_context'});
    expect(state.step).toBe('context');
    expect(state.interpretState).toBe('running');
    expect(state.interpretAttempt).toBe(1);
    expect(state.interpretRequestId).toBe(1);

    let confirmed = createInitialState({config: {keyword: 'pipeline attribution', keywordContext: 'Already confirmed'}});
    confirmed = reducer(confirmed, {type: 'request_context'});
    expect(confirmed.step).toBe('context');
    expect(confirmed.interpretState).toBe('idle');
    expect(confirmed.interpretRequestId).toBe(0);
  });

  it('request_context keeps the same keyword/setup gates as request_run', () => {
    const noKeyword = reducer(createInitialState(), {type: 'request_context'});
    expect(noKeyword.step).toBe('config');
    expect(noKeyword.log.at(-1)?.text).toContain('Set a keyword');

    const blocked = reducer(
      createInitialState({
        config: {keyword: 'pipeline attribution'},
        setupStatus: {checks: [], canGenerate: false, requiredMissing: ['openrouter'], optionalMissing: []},
      }),
      {type: 'request_context'},
    );
    expect(blocked.step).toBe('config');
    expect(blocked.notifications.at(-1)?.message).toContain('missing required values');
  });

  it('request_interpret stages feedback with the previous take for steered regens', () => {
    let state = createInitialState({config: {keyword: 'pipeline attribution', keywordContext: 'First take'}});
    state = reducer(state, {type: 'request_interpret', feedback: 'make it sharper'});
    expect(state.step).toBe('context');
    expect(state.interpretState).toBe('running');
    expect(state.interpretAttempt).toBe(1);
    expect(state.interpretFeedback).toBe('make it sharper');
    expect(state.interpretPrevious).toBe('First take');

    // Fresh regenerate (Ctrl+N) carries no feedback but DOES carry the previous
    // take — without it the bridge prompt is byte-identical and the regen can
    // come back as the exact same interpretation.
    state = reducer(state, {type: 'request_interpret'});
    expect(state.interpretAttempt).toBe(2);
    expect(state.interpretFeedback).toBeUndefined();
    expect(state.interpretPrevious).toBe('First take');
  });

  it('request_interpret with no prior take stages no previous', () => {
    let state = createInitialState({config: {keyword: 'pipeline attribution'}});
    state = reducer(state, {type: 'request_interpret'});
    expect(state.interpretState).toBe('running');
    expect(state.interpretPrevious).toBeUndefined();
  });

  it('interpret_finished settles only an in-flight interpretation', () => {
    const running = createInitialState({interpretState: 'running'});
    expect(reducer(running, {type: 'interpret_finished', ok: true}).interpretState).toBe('done');
    expect(reducer(running, {type: 'interpret_finished', ok: false}).interpretState).toBe('failed');

    const done = createInitialState({interpretState: 'done'});
    expect(reducer(done, {type: 'interpret_finished', ok: false}).interpretState).toBe('done');
  });

  it('resets the interpretation lifecycle on keyword change and on new_run', () => {
    let state = createInitialState({
      config: {keyword: 'pipeline attribution'},
      interpretState: 'done',
      interpretAttempt: 3,
      interpretFeedback: 'sharper',
    });
    state = reducer(state, {type: 'update_config', patch: {keyword: 'lead routing'}});
    expect(state.interpretState).toBe('idle');
    expect(state.interpretAttempt).toBe(0);
    expect(state.interpretFeedback).toBeUndefined();

    // Non-keyword patches leave the lifecycle alone.
    let untouched = createInitialState({interpretState: 'done', interpretAttempt: 2});
    untouched = reducer(untouched, {type: 'update_config', patch: {depth: 'quick'}});
    expect(untouched.interpretAttempt).toBe(2);

    let reset = createInitialState({step: 'output', interpretState: 'done', interpretAttempt: 4});
    reset = reducer(reset, {type: 'new_run'});
    expect(reset.interpretState).toBe('idle');
    expect(reset.interpretAttempt).toBe(0);
  });
});

describe('keyword-ideas lifecycle', () => {
  it('request_suggest enters the keyword-ideas stage, bumps counters, and stages previous ideas', () => {
    let state = createInitialState({
      keywordSuggestions: [
        {keyword: 'old idea one', score: 40, grounded: true, estimated: false, trafficPotential: 10},
        {keyword: 'old idea two', score: null, grounded: false, estimated: false, trafficPotential: null},
      ],
    });
    state = reducer(state, {type: 'request_suggest'});
    expect(state.configStage).toBe('keyword-ideas');
    expect(state.suggestState).toBe('running');
    expect(state.suggestAttempt).toBe(1);
    expect(state.suggestRequestId).toBe(1);
    expect(state.suggestSteer).toBeUndefined();
    expect(state.suggestPrevious).toEqual(['old idea one', 'old idea two']);

    // A steered regen still bumps both counters and carries the steer text.
    state = reducer(state, {type: 'request_suggest', steer: 'closed loop attribution'});
    expect(state.suggestAttempt).toBe(2);
    expect(state.suggestRequestId).toBe(2);
    expect(state.suggestSteer).toBe('closed loop attribution');
  });

  it('suggest_finished settles only an in-flight run', () => {
    const running = createInitialState({suggestState: 'running'});
    expect(reducer(running, {type: 'suggest_finished', ok: true}).suggestState).toBe('done');
    expect(reducer(running, {type: 'suggest_finished', ok: false}).suggestState).toBe('failed');

    const done = createInitialState({suggestState: 'done'});
    expect(reducer(done, {type: 'suggest_finished', ok: false}).suggestState).toBe('done');
  });

  it('toggle_suggest_trends flips the standing preference', () => {
    let state = createInitialState({suggestTrendsFresh: false});
    state = reducer(state, {type: 'toggle_suggest_trends'});
    expect(state.suggestTrendsFresh).toBe(true);
    state = reducer(state, {type: 'toggle_suggest_trends'});
    expect(state.suggestTrendsFresh).toBe(false);
  });

  it('defaults suggestTrendsFresh to fast and topicMode to angle', () => {
    const state = createInitialState();
    expect(state.suggestTrendsFresh).toBe(false);
    expect(state.topicMode).toBe('angle');
  });

  it('toggle_topic_mode flips angle ↔ keyword', () => {
    let state = createInitialState();
    expect(state.topicMode).toBe('angle');
    state = reducer(state, {type: 'toggle_topic_mode'});
    expect(state.topicMode).toBe('keyword');
    state = reducer(state, {type: 'toggle_topic_mode'});
    expect(state.topicMode).toBe('angle');
  });

  it('stages previous ideas from the mode-primary field', () => {
    const suggestions = [
      {keyword: 'kw one', angle: 'angle one', score: 40, grounded: true, estimated: false, trafficPotential: 10},
      {keyword: 'kw two', angle: null, score: null, grounded: false, estimated: false, trafficPotential: null},
    ];
    // Angle mode: angle wins, falling back to the keyword when the angle is null.
    const angleState = reducer(createInitialState({topicMode: 'angle', keywordSuggestions: suggestions}), {type: 'request_suggest'});
    expect(angleState.suggestPrevious).toEqual(['angle one', 'kw two']);
    // Keyword mode: always the keyword.
    const keywordState = reducer(createInitialState({topicMode: 'keyword', keywordSuggestions: suggestions}), {type: 'request_suggest'});
    expect(keywordState.suggestPrevious).toEqual(['kw one', 'kw two']);
  });

  it('stores suggestGscAvailable from the keyword_suggestions artifact when it is a boolean', () => {
    const withFlag = reduceBridgeEvent(
      createInitialState({suggestState: 'running'}),
      event('artifact_ready', {taskId: 'keyword-suggestions', data: {artifact: 'keyword_suggestions', suggestions: [], gscAvailable: true, mode: 'angle'}}),
    );
    expect(withFlag.suggestGscAvailable).toBe(true);

    // Non-boolean / absent leaves it undefined rather than coercing.
    const withoutFlag = reduceBridgeEvent(
      createInitialState({suggestState: 'running'}),
      event('artifact_ready', {taskId: 'keyword-suggestions', data: {artifact: 'keyword_suggestions', suggestions: []}}),
    );
    expect(withoutFlag.suggestGscAvailable).toBeUndefined();
  });

  it('passes topicMode as --mode to the suggest bridge args', () => {
    const config = createInitialState().config;
    const angleArgs = buildSuggestArgs(config, 'run_test', {mode: 'angle'});
    expect(angleArgs).toContain('--mode');
    expect(angleArgs).toContain('angle');
    expect(buildSuggestArgs(config, 'run_test', {mode: 'keyword'})).toContain('keyword');
    // No mode ⇒ no --mode flag (old-bridge compatible).
    expect(buildSuggestArgs(config, 'run_test')).not.toContain('--mode');
  });

  it('pick_suggestion sets the keyword through update_config semantics and lands on style', () => {
    let state = createInitialState({
      configStage: 'keyword-ideas',
      config: {keyword: 'old keyword'},
      interpretState: 'done',
      interpretAttempt: 2,
      keywordSuggestions: [{keyword: 'offline conversion tracking', score: 79, grounded: true, estimated: false, trafficPotential: 320}],
    });
    state = reducer(state, {type: 'pick_suggestion', keyword: 'offline conversion tracking'});
    expect(state.config.keyword).toBe('offline conversion tracking');
    expect(state.configStage).toBe('style');
    // The same reset update_config applies on a keyword change fires here too.
    expect(state.interpretState).toBe('idle');
    expect(state.interpretAttempt).toBe(0);
    expect(state.suggestState).toBe('idle');
    expect(state.suggestAttempt).toBe(0);
    expect(state.keywordSuggestions).toBeUndefined();
  });

  it('fills keywordSuggestions and marks done on the keyword_suggestions artifact, tolerating malformed data', () => {
    const state = reduceBridgeEvent(
      createInitialState({suggestState: 'running'}),
      event('artifact_ready', {
        taskId: 'keyword-suggestions',
        data: {
          artifact: 'keyword_suggestions',
          suggestions: [
            {keyword: 'offline conversion tracking', score: 79, grounded: true, estimated: false, trafficPotential: 320, rationale: 'High intent'},
          ],
        },
      }),
    );
    expect(state.suggestState).toBe('done');
    expect(state.keywordSuggestions).toHaveLength(1);
    expect(state.keywordSuggestions?.[0]?.keyword).toBe('offline conversion tracking');

    // Malformed/missing suggestions default to an empty array rather than crashing.
    const malformed = reduceBridgeEvent(
      createInitialState({suggestState: 'running'}),
      event('artifact_ready', {taskId: 'keyword-suggestions', data: {artifact: 'keyword_suggestions', suggestions: 'not-an-array'}}),
    );
    expect(malformed.keywordSuggestions).toEqual([]);
    expect(malformed.suggestState).toBe('done');
  });

  it('marks keyword-suggestions failed on a matching error event', () => {
    const state = reduceBridgeEvent(
      createInitialState({suggestState: 'running'}),
      event('error', {taskId: 'keyword-suggestions', message: 'suggest-keywords failed'}),
    );
    expect(state.suggestState).toBe('failed');
  });

  it('resetForNewRun clears suggest state but keeps suggestRequestId monotonic, suggestTrendsFresh, and topicMode', () => {
    let state = createInitialState({
      step: 'output',
      suggestState: 'done',
      suggestAttempt: 3,
      suggestRequestId: 5,
      suggestTrendsFresh: true,
      suggestGscAvailable: true,
      topicMode: 'keyword',
      keywordSuggestions: [{keyword: 'idea', score: 10, grounded: false, estimated: false, trafficPotential: 1}],
    });
    state = reducer(state, {type: 'new_run'});
    expect(state.suggestState).toBe('idle');
    expect(state.suggestAttempt).toBe(0);
    expect(state.keywordSuggestions).toBeUndefined();
    expect(state.suggestRequestId).toBe(5);
    // Standing preferences persist across runs; the per-run availability flag resets.
    expect(state.suggestTrendsFresh).toBe(true);
    expect(state.topicMode).toBe('keyword');
    expect(state.suggestGscAvailable).toBeUndefined();
  });

  it('clears keyword ideas on a keyword change but leaves them alone for non-keyword patches', () => {
    let state = createInitialState({
      config: {keyword: 'old keyword'},
      suggestState: 'done',
      suggestAttempt: 2,
      keywordSuggestions: [{keyword: 'idea', score: 10, grounded: false, estimated: false, trafficPotential: 1}],
    });
    state = reducer(state, {type: 'update_config', patch: {keyword: 'new keyword'}});
    expect(state.suggestState).toBe('idle');
    expect(state.suggestAttempt).toBe(0);
    expect(state.keywordSuggestions).toBeUndefined();

    let untouched = createInitialState({
      suggestState: 'done',
      suggestAttempt: 2,
      keywordSuggestions: [{keyword: 'idea', score: 10, grounded: false, estimated: false, trafficPotential: 1}],
    });
    untouched = reducer(untouched, {type: 'update_config', patch: {depth: 'quick'}});
    expect(untouched.suggestState).toBe('done');
    expect(untouched.keywordSuggestions).toHaveLength(1);
  });
});

describe('colon command grammar', () => {
  it('applies idle config commands', () => {
    let state = createInitialState();
    state = applyCommand(state, 'keyword pipeline attribution');
    state = applyCommand(state, 'style research');
    state = applyCommand(state, 'depth comprehensive');
    state = applyCommand(state, 'community limited');
    state = applyCommand(state, 'flyweel none');
    state = applyCommand(state, 'icp off');
    state = applyCommand(state, 'gsc off');
    state = applyCommand(state, 'gsc-keywords off');
    state = applyCommand(state, 'solutions 15');
    state = applyCommand(state, 'competitors Alpha, Beta');
    state = applyCommand(state, 'output local');
    state = applyCommand(state, 'note use the CFO angle');
    state = applyCommand(state, 'context interpret as pipeline attribution for home services');

    expect(state.config).toMatchObject({
      keyword: 'pipeline attribution',
      style: 'research',
      depth: 'comprehensive',
      communityMode: 'limited',
      flyweelMode: 'none',
      icpEnabled: false,
      gscCheck: false,
      gscKeywords: false,
      solutionCount: 15,
      competitors: 'Alpha, Beta',
      saveDefault: 'local',
      note: 'use the CFO angle',
      keywordContext: 'interpret as pipeline attribution for home services',
    });
  });

  it('applies the competitor-mentions command and routes interpret to the context step', () => {
    let state = createInitialState({config: {keyword: 'pipeline attribution'}});
    state = applyCommand(state, 'competitor-mentions off');
    expect(state.config.competitorMentions).toBe(false);
    state = applyCommand(state, 'competitor-mentions on');
    expect(state.config.competitorMentions).toBe(true);
    state = applyCommand(state, 'competitor-mentions maybe');
    expect(state.log.at(-1)?.text).toContain('Usage: competitor-mentions');

    state = applyCommand(state, 'interpret');
    expect(state.step).toBe('context');
    expect(state.interpretState).toBe('running');
    expect(state.interpretRequestId).toBe(1);
  });

  it('requests setup and gates start', () => {
    let state = createInitialState();
    state = applyCommand(state, 'setup');
    expect(state.step).toBe('setup');
    expect(state.setupRequestId).toBe(1);

    state = applyCommand(state, 'start');
    expect(state.log.at(-1)?.text).toContain('Set a keyword');

    state = applyCommand(state, 'keyword pipeline attribution');
    state = applyCommand(state, 'start');
    expect(state.step).toBe('running');
    expect(state.runRequestId).toBe(1);
  });

  it('blocks start when loaded setup status is missing required keys', () => {
    let state = createInitialState({
      setupStatus: {
        checks: [],
        canGenerate: false,
        requiredMissing: ['openrouter'],
        optionalMissing: [],
      },
    });
    state = applyCommand(state, 'keyword pipeline attribution');
    state = applyCommand(state, 'start');
    expect(state.runRequestId).toBe(0);
    expect(state.log.at(-1)?.text).toContain('Setup is missing required values');
  });

  it('stages setup changes and requires confirmation before save', () => {
    let state = createInitialState({step: 'setup'});
    state = applyCommand(state, 'setup-set openrouter sk-test-123');
    expect(state.setupDraft.OPENROUTER_API_KEY).toBe('sk-test-123');
    state = applyCommand(state, 'setup-set google-api-key google-test-123');
    expect(state.setupDraft.GOOGLE_API_KEY).toBe('google-test-123');
    expect(state.setupConfirmPending).toBe(false);
    state = applyCommand(state, 'save-setup');
    expect(state.setupConfirmPending).toBe(true);
    state = applyCommand(state, 'confirm-setup');
    expect(state.setupSaveRequestId).toBe(1);
  });

  it('cancels staged setup changes', () => {
    let state = createInitialState({step: 'setup'});
    state = applyCommand(state, 'setup-set astro-blog-path /tmp/blog');
    state = applyCommand(state, 'cancel-setup');
    expect(state.setupDraft).toEqual({});
    expect(state.setupConfirmPending).toBe(false);
  });

  it('submits command input through the reducer', () => {
    let state = createInitialState({commandMode: 'command'});
    state = reducer(state, {type: 'set_input', value: 'keyword closed loop ads'});
    state = reducer(state, {type: 'submit_input'});
    expect(state.config.keyword).toBe('closed loop ads');
    expect(state.commandMode).toBe(false);
  });
});

describe('preview model', () => {
  it('extracts article, frontmatter, metadata, citations, quality, and raw draft', () => {
    const state = createInitialState({
      artifacts: [
        {
          id: 'final-mdx',
          data: {
            artifact: 'final_mdx',
            content: '---\ntitle: Test\n---\n# Body\nSee [source](https://example.com/source).',
            rawContent: '# Raw draft',
            metadata: {title: 'Test', slug: 'test', qualityScorecard: {composite: 91}},
          },
        },
      ],
    });
    const preview = buildPreviewModel(state);
    expect(preview.frontmatter).toContain('title: Test');
    expect(preview.article).toContain('# Body');
    expect(preview.citations[0]).toContain('https://example.com/source');
    expect(preview.quality.composite).toBe(91);
    expect(preview.rawDraft).toBe('# Raw draft');
  });
});

describe('config flow state', () => {
  it('defaults to the keyword stage and jumps to settings when prefilled', () => {
    expect(createInitialState().configStage).toBe('keyword');
    expect(createInitialState({config: {keyword: 'pipeline attribution'}}).configStage).toBe('settings');
  });

  it('advances config stage via set_config_stage', () => {
    let state = createInitialState();
    state = reducer(state, {type: 'set_config_stage', stage: 'style'});
    expect(state.configStage).toBe('style');
    state = reducer(state, {type: 'set_config_stage', stage: 'settings'});
    expect(state.configStage).toBe('settings');
  });

  it('updates config fields via update_config', () => {
    let state = createInitialState();
    state = reducer(state, {type: 'update_config', patch: {style: 'research', solutionCount: 15}});
    expect(state.config.style).toBe('research');
    expect(state.config.solutionCount).toBe(15);
  });

  it('gates request_run until a keyword and setup readiness are present', () => {
    let blocked = createInitialState({setupStatus: {checks: [], canGenerate: false, requiredMissing: ['openrouter'], optionalMissing: []}});
    blocked = reducer(blocked, {type: 'update_config', patch: {keyword: 'pipeline attribution'}});
    blocked = reducer(blocked, {type: 'request_run'});
    expect(blocked.runRequestId).toBe(0);
    expect(blocked.step).not.toBe('running');

    let ready = createInitialState();
    ready = reducer(ready, {type: 'update_config', patch: {keyword: 'pipeline attribution'}});
    ready = reducer(ready, {type: 'request_run'});
    expect(ready.runRequestId).toBe(1);
    expect(ready.step).toBe('running');
  });

  it('tracks the return step for setup side-trips', () => {
    let state = createInitialState({step: 'preview', artifacts: [finalArtifact]});
    state = reducer(state, {type: 'request_setup'});
    expect(state.step).toBe('setup');
    expect(state.returnStep).toBe('preview');
  });

  it('requests topic interpretation only with a keyword', () => {
    let state = createInitialState();
    state = reducer(state, {type: 'request_interpret'});
    expect(state.interpretRequestId).toBe(0);
    state = reducer(state, {type: 'update_config', patch: {keyword: 'pipeline attribution'}});
    state = reducer(state, {type: 'request_interpret'});
    expect(state.interpretRequestId).toBe(1);
  });

  it('suspends comments at the GSC gate and serializes save/publish actions', () => {
    const artifact = {id: 'final', data: {artifact: 'final_mdx', content: '# final'}};
    const gated = createInitialState({step: 'running', gscGate: {score: 1, threshold: 25}});
    expect(reducer(gated, {type: 'open_comment'}).commandMode).toBe(false);

    const saving = reducer(createInitialState({step: 'output', artifacts: [artifact]}), {type: 'request_save'});
    const publishWhileSaving = reducer(saving, {type: 'request_publish'});
    expect(publishWhileSaving.publishRequestId).toBe(0);
    expect(publishWhileSaving.saveState).toBe('running');

    const publishing = reducer(createInitialState({step: 'output', artifacts: [artifact]}), {type: 'request_publish'});
    const saveWhilePublishing = reducer(publishing, {type: 'request_save'});
    expect(saveWhilePublishing.saveRequestId).toBe(0);
    expect(saveWhilePublishing.publishState).toBe('running');
  });

  it('surfaces artifact conflicts without treating them as generic errors', () => {
    const state = reduceBridgeEvent(
      createInitialState({step: 'output', publishState: 'running'}),
      event('artifact_conflict' as never, {
        phase: 'output',
        data: {slug: 'existing-post', action: 'publish', overwriteAllowed: true},
      }),
    );
    expect(state.publishState).toBe('failed');
    expect(state.notifications.at(-1)?.message).toContain('existing-post');
  });

  it('folds a batch of bridge events in a single dispatch', () => {
    const batched = reducer(createInitialState(), {
      type: 'bridge_events',
      events: [
        event('phase_started', {phase: 'research', taskId: 'serp', message: 'SERP'}),
        event('generation_complete', {data: {qualityScorecard: {composite: 88}}}),
      ],
    });
    expect(batched.step).toBe('preview');
    expect(batched.progressRows.length).toBeGreaterThan(0);
  });
});
