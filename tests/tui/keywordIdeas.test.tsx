import React from 'react';
import {beforeEach, describe, expect, it, vi} from 'vitest';
import {render} from 'ink-testing-library';

import {App} from '../../tui/App.js';
import {spawnBridge, spawnKeywordInterpretation, spawnKeywordSuggestions, spawnSetupStatus} from '../../tui/bridge.js';
import type {BridgeEvent, KeywordSuggestion} from '../../tui/types.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 40));

// Hand-rolled fake bridge clients (no subprocess, no EventEmitter import — vi.hoisted
// runs before module imports). Tests emit JSONL-shaped events straight into App.
// Cloned from tests/tui/context.test.tsx's harness, with a 'suggest' client kind added.
const fake = vi.hoisted(() => {
  type Listener = (...args: unknown[]) => void;
  interface FakeClient {
    child: {kill: (signal?: string) => void};
    sendCommand: (command: unknown) => void;
    cancel: (runId: string) => void;
    on: (event: string, listener: Listener) => FakeClient;
    emit: (event: string, ...args: unknown[]) => void;
    removeAllListeners: () => FakeClient;
    cleared: boolean;
  }
  const clients: Record<'setup' | 'interpret' | 'run' | 'suggest', FakeClient[]> = {setup: [], interpret: [], run: [], suggest: []};
  const makeClient = (kind: 'setup' | 'interpret' | 'run' | 'suggest'): FakeClient => {
    const listeners = new Map<string, Listener[]>();
    const client: FakeClient = {
      child: {kill: () => {}},
      sendCommand: () => {},
      cancel: () => {},
      cleared: false,
      on(event, listener) {
        listeners.set(event, [...(listeners.get(event) ?? []), listener]);
        return client;
      },
      emit(event, ...args) {
        for (const listener of listeners.get(event) ?? []) listener(...args);
      },
      removeAllListeners() {
        listeners.clear();
        client.cleared = true;
        return client;
      },
    };
    clients[kind].push(client);
    return client;
  };
  const reset = () => {
    clients.setup.length = 0;
    clients.interpret.length = 0;
    clients.run.length = 0;
    clients.suggest.length = 0;
  };
  return {clients, makeClient, reset};
});

vi.mock('../../tui/bridge.js', async importOriginal => {
  const actual = await importOriginal<typeof import('../../tui/bridge.js')>();
  return {
    ...actual,
    spawnSetupStatus: vi.fn(() => fake.makeClient('setup')),
    spawnKeywordInterpretation: vi.fn(() => fake.makeClient('interpret')),
    spawnKeywordSuggestions: vi.fn(() => fake.makeClient('suggest')),
    spawnBridge: vi.fn(() => fake.makeClient('run')),
  };
});

const suggestionsArtifact = (suggestions: KeywordSuggestion[], gscAvailable?: boolean): BridgeEvent => ({
  type: 'artifact_ready',
  runId: 'sugg_test',
  timestamp: '2026-07-02T00:00:00Z',
  phase: 'config',
  taskId: 'keyword-suggestions',
  message: 'Keyword ideas ready',
  data: {artifact: 'keyword_suggestions', suggestions, ...(gscAvailable === undefined ? {} : {gscAvailable})},
});

// Angle-framed fixtures (the bridge now runs --mode angle by default). Each idea carries
// both an `angle` (the headline in angle mode) and a `keyword` (the detail-strip secondary).
const FIVE_SUGGESTIONS: KeywordSuggestion[] = [
  {keyword: 'offline conversion tracking', angle: 'Fix closed-loop attribution', score: 79, grounded: true, estimated: false, trafficPotential: 320, rationale: 'High commercial intent'},
  {keyword: 'ai budget pacing signals', angle: 'Bet on AI budget pacing', score: 41, grounded: false, estimated: true, trafficPotential: 55, rationale: 'Emerging demand'},
  {keyword: 'server-side capi setup', angle: 'Server-side CAPI, end to end', score: null, grounded: false, estimated: false, trafficPotential: null, rationale: 'No GSC data'},
  {keyword: 'closed loop attribution', angle: 'Attribution without guesswork', score: 65, grounded: true, estimated: false, trafficPotential: 210, rationale: 'Strong match'},
  {keyword: 'lead routing automation', angle: 'Lead routing that converts', score: 30, grounded: true, estimated: false, trafficPotential: 40, rationale: 'Adjacent topic'},
];

// The mode-primary strings the view shows as rows and stages as `previous` on regen — the
// angle in angle mode (the default), falling back to the keyword.
const ANGLES = FIVE_SUGGESTIONS.map(suggestion => suggestion.angle ?? suggestion.keyword);

// Emits the artifact and lets the resulting state update/re-render flush. (Unlike the
// context step, landing a keyword_suggestions artifact never touches state.config, so
// the suggest effect's dependencies never change and its cleanup never runs here —
// there is no 'cleared' signal to wait on; a couple of ticks is enough.)
const emitSuggestions = async (suggestions: KeywordSuggestion[], gscAvailable?: boolean) => {
  const client = fake.clients.suggest.at(-1);
  client?.emit('events', [suggestionsArtifact(suggestions, gscAvailable)]);
  await tick();
  await tick();
};

const lineCount = (frame: string | undefined) => (frame ?? '').split('\n').length;

describe('keyword-ideas journey side-trip', () => {
  beforeEach(() => {
    fake.reset();
    vi.mocked(spawnKeywordSuggestions).mockClear();
    vi.mocked(spawnKeywordInterpretation).mockClear();
    vi.mocked(spawnBridge).mockClear();
  });

  it('walks ^G › spinner › angle rows + detail › steer › ^N › ^T › pick › settings', async () => {
    const {lastFrame, stdin, unmount} = render(<App config={{}} />);
    await tick();
    // Angle-driven framing is the default, so the keyword prompt reads "angle / topic".
    expect(lastFrame()).toContain('What angle / topic');

    // ^G on the blank keyword stage: no --seed since the keyword is empty. Trends default
    // fast now, and the ideation bridge runs with mode: 'angle'.
    stdin.write('\x07');
    await vi.waitFor(() => expect(spawnKeywordSuggestions).toHaveBeenCalledTimes(1));
    await tick();
    expect(lastFrame()).toContain('Finding high-opportunity ideas');
    const firstConfig = vi.mocked(spawnKeywordSuggestions).mock.calls[0]?.[0];
    expect(firstConfig?.keyword.trim()).toBe('');
    expect(vi.mocked(spawnKeywordSuggestions).mock.calls[0]?.[2]).toMatchObject({steer: undefined, previous: [], trendsFresh: false, target: 60, mode: 'angle'});

    // Suggestions land: rows render ANGLES (angle mode default) with score badges, and the
    // focused row's detail strip shows its keyword + traffic + rationale.
    await emitSuggestions(FIVE_SUGGESTIONS);
    expect(lastFrame()).toContain('Fix closed-loop attribution');
    expect(lastFrame()).toContain('79% ✓');
    expect(lastFrame()).toContain('~41% est');
    expect(lastFrame()).toContain('unscored');
    // Detail strip for the focused (first) idea: its underlying keyword + rationale.
    expect(lastFrame()).toContain('kw: offline conversion tracking');
    expect(lastFrame()).toContain('High commercial intent');
    expect(lastFrame()).not.toContain('Finding high-opportunity ideas');

    // Typed steer text + Enter: second spawn carries the steer and the prior 5 ANGLES.
    for (const char of 'attribution') {
      stdin.write(char);
      await tick();
    }
    stdin.write('\r');
    await vi.waitFor(() => expect(spawnKeywordSuggestions).toHaveBeenCalledTimes(2));
    await tick();
    expect(vi.mocked(spawnKeywordSuggestions).mock.calls[1]?.[2]).toMatchObject({
      steer: 'attribution',
      previous: ANGLES,
      trendsFresh: false,
    });
    expect(lastFrame()).toContain('take 2');

    await emitSuggestions(FIVE_SUGGESTIONS);

    // Ctrl+N: fresh regenerate — no steer, but previous ideas (angles) are still carried.
    stdin.write('\x0e');
    await vi.waitFor(() => expect(spawnKeywordSuggestions).toHaveBeenCalledTimes(3));
    await tick();
    expect(vi.mocked(spawnKeywordSuggestions).mock.calls[2]?.[2]).toMatchObject({
      steer: undefined,
      previous: ANGLES,
    });
    expect(lastFrame()).toContain('take 3');

    await emitSuggestions(FIVE_SUGGESTIONS);

    // Ctrl+T toggles trends on (default is now fast); the next spawn carries trendsFresh: true.
    expect(lastFrame()).toContain('trends: fast');
    stdin.write('\x14');
    await tick();
    expect(lastFrame()).toContain('trends: fresh');
    stdin.write('\x0e');
    await vi.waitFor(() => expect(spawnKeywordSuggestions).toHaveBeenCalledTimes(4));
    await tick();
    expect(vi.mocked(spawnKeywordSuggestions).mock.calls[3]?.[2]).toMatchObject({trendsFresh: true});

    await emitSuggestions(FIVE_SUGGESTIONS);

    // Enter selects the highlighted (first) row: the ANGLE string is dispatched into
    // config.keyword and the config stage advances off keyword-ideas onto style.
    stdin.write('\r');
    await tick();
    expect(lastFrame()).toContain('Pick a content style');

    unmount();
  });

  it('picks the highlighted angle into config.keyword', async () => {
    const {lastFrame, stdin, unmount} = render(<App config={{}} />);
    await tick();
    stdin.write('\x07');
    await vi.waitFor(() => expect(fake.clients.suggest.length).toBeGreaterThan(0));
    await tick();
    await emitSuggestions(FIVE_SUGGESTIONS);

    // Enter picks the highlighted (first) row: its ANGLE lands in config.keyword (shown in
    // the masthead as "<angle> | <style>") and the stage advances off keyword-ideas onto style.
    stdin.write('\r');
    await tick();
    const frame = lastFrame() ?? '';
    expect(frame).toContain('Pick a content style');
    // The masthead now carries the picked ANGLE as config.keyword (not the raw keyword).
    expect(frame).toContain(ANGLES[0]);
    expect(frame).not.toContain('offline conversion tracking');
    unmount();
  });

  it('renders angle rows and a focused-idea detail strip', async () => {
    const {lastFrame, stdin, unmount} = render(<App config={{}} />);
    await tick();
    stdin.write('\x07');
    await vi.waitFor(() => expect(fake.clients.suggest.length).toBeGreaterThan(0));
    await tick();
    await emitSuggestions(FIVE_SUGGESTIONS);

    const frame = lastFrame() ?? '';
    // Every idea's angle is a row headline.
    for (const angle of ANGLES) expect(frame).toContain(angle);
    // Down-arrow moves focus; the detail strip follows to the second idea.
    stdin.write('\x1b[B');
    await tick();
    expect(lastFrame()).toContain('kw: ai budget pacing signals');
    expect(lastFrame()).toContain('Emerging demand');
    unmount();
  });

  it('shows the GSC-unavailable note when the artifact reports gscAvailable:false', async () => {
    const {lastFrame, stdin, unmount} = render(<App config={{}} />);
    await tick();
    stdin.write('\x07');
    await vi.waitFor(() => expect(fake.clients.suggest.length).toBeGreaterThan(0));
    await tick();
    await emitSuggestions(FIVE_SUGGESTIONS, false);
    expect(lastFrame()).toContain('GSC unavailable — ideas unscored');
    unmount();
  });

  it('keeps a constant frame height across running / loaded / failed states', async () => {
    const {lastFrame, stdin, unmount} = render(<App config={{}} />);
    await tick();
    stdin.write('\x07');
    await vi.waitFor(() => expect(spawnKeywordSuggestions).toHaveBeenCalledTimes(1));
    await tick();
    // Running: spinner line + the always-mounted steer/hint block are both present.
    const runningFrame = lastFrame() ?? '';
    expect(runningFrame).toContain('Finding high-opportunity ideas');
    expect(runningFrame).toContain('Ctrl+N regenerate');
    const runningLines = lineCount(runningFrame);

    // Loaded: rows replace the spinner; the steer/hint block is still there.
    await emitSuggestions(FIVE_SUGGESTIONS);
    const loadedFrame = lastFrame() ?? '';
    expect(loadedFrame).toContain('79% ✓');
    expect(loadedFrame).toContain('Ctrl+N regenerate');
    expect(lineCount(loadedFrame)).toBe(runningLines);

    unmount();

    // Failed: message line + the steer/hint block, same overall height.
    const failed = render(<App config={{}} />);
    await tick();
    failed.stdin.write('\x07');
    await vi.waitFor(() => expect(fake.clients.suggest.length).toBeGreaterThan(0));
    await tick();
    fake.clients.suggest.at(-1)?.emit('exit', 1, null);
    await tick();
    const failedFrame = failed.lastFrame() ?? '';
    expect(failedFrame).toContain('Keyword ideas failed');
    expect(failedFrame).toContain('Ctrl+N regenerate');
    expect(lineCount(failedFrame)).toBe(runningLines);
    failed.unmount();
  });

  it('restarts steered when steer text is submitted while a fetch is running', async () => {
    // Submitting mid-run must behave like Ctrl+N mid-run: restart the fetch (steered)
    // and clear the draft. A guard-and-ignore would strand a stale draft that keeps
    // the ideas list permanently inactive once results land.
    const {lastFrame, stdin, unmount} = render(<App config={{}} />);
    await tick();
    stdin.write('\x07');
    await vi.waitFor(() => expect(spawnKeywordSuggestions).toHaveBeenCalledTimes(1));
    await tick();
    expect(lastFrame()).toContain('Finding high-opportunity ideas');

    // Type + submit while the spinner is still running (no artifact emitted yet).
    for (const char of 'revops') {
      stdin.write(char);
      await tick();
    }
    stdin.write('\r');
    await vi.waitFor(() => expect(spawnKeywordSuggestions).toHaveBeenCalledTimes(2));
    await tick();
    expect(vi.mocked(spawnKeywordSuggestions).mock.calls[1]?.[2]).toMatchObject({steer: 'revops', previous: []});
    expect(lastFrame()).toContain('take 2');

    // The restarted fetch's results land normally and the list is active (Enter picks —
    // proving the mid-run submit didn't strand a stale draft that disables the list).
    await emitSuggestions(FIVE_SUGGESTIONS);
    expect(lastFrame()).toContain('Fix closed-loop attribution');
    stdin.write('\r');
    await tick();
    expect(lastFrame()).toContain('Pick a content style');
    unmount();
  });

  it('spawns with --seed from the settings stage when the keyword is prefilled', async () => {
    // A prefilled keyword defaults configStage to 'settings' — Ctrl+G must work from
    // any config stage, not just the keyword prompt.
    const {stdin, unmount} = render(<App config={{keyword: 'lead routing'}} />);
    await tick();
    stdin.write('\x07');
    await vi.waitFor(() => expect(spawnKeywordSuggestions).toHaveBeenCalledTimes(1));
    const config = vi.mocked(spawnKeywordSuggestions).mock.calls[0]?.[0];
    expect(config?.keyword).toBe('lead routing');
    unmount();
  });

  it('returns to the keyword stage on Esc without picking anything', async () => {
    const {lastFrame, stdin, unmount} = render(<App config={{}} />);
    await tick();
    stdin.write('\x07');
    await vi.waitFor(() => expect(fake.clients.suggest.length).toBeGreaterThan(0));
    await tick();
    await emitSuggestions(FIVE_SUGGESTIONS);
    stdin.write('\x1b'); // Esc
    await tick();
    expect(lastFrame()).toContain('What angle / topic');
    unmount();
  });

  it('shows a failed line and retries on ^N', async () => {
    const {lastFrame, stdin, unmount} = render(<App config={{}} />);
    await tick();
    stdin.write('\x07');
    await vi.waitFor(() => expect(fake.clients.suggest.length).toBeGreaterThan(0));
    await tick();
    fake.clients.suggest.at(-1)?.emit('exit', 1, null);
    await tick();
    expect(lastFrame()).toContain('Keyword ideas failed');

    stdin.write('\x0e');
    await vi.waitFor(() => expect(spawnKeywordSuggestions).toHaveBeenCalledTimes(2));
    unmount();
  });

  it('detaches and terminates an in-flight suggestion child on Esc so stale errors cannot clobber state', async () => {
    const {lastFrame, stdin, unmount} = render(<App config={{keyword: 'lead routing'}} />);
    await tick();
    stdin.write('\x07'); // Ctrl+G -> keyword-ideas
    await vi.waitFor(() => expect(fake.clients.suggest.length).toBe(1));
    const inflight = fake.clients.suggest[0]!;
    expect(inflight.cleared).toBe(false);

    // Press Esc while suggestion child is still running
    stdin.write('\x1b');
    await tick();
    expect(inflight.cleared).toBe(true);

    // Emitting a late error on the detached child has no effect on the current screen
    inflight.emit('events', [
      {
        type: 'error',
        runId: 'sugg_stale',
        timestamp: '2026-07-02T00:00:05Z',
        phase: 'config',
        taskId: 'keyword-suggestions',
        message: 'stale error',
      },
    ]);
    await tick();
    expect(lastFrame()).toContain('Generation Config');
    expect(lastFrame()).not.toContain('stale error');
    unmount();
  });
});
