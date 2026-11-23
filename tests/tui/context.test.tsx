import React from 'react';
import {beforeEach, describe, expect, it, vi} from 'vitest';
import {render} from 'ink-testing-library';

import {App} from '../../tui/App.js';
import {spawnBridge, spawnKeywordInterpretation} from '../../tui/bridge.js';
import type {BridgeEvent} from '../../tui/types.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 40));

// Hand-rolled fake bridge clients (no subprocess, no EventEmitter import — vi.hoisted
// runs before module imports). Tests emit JSONL-shaped events straight into App.
const fake = vi.hoisted(() => {
  type Listener = (...args: unknown[]) => void;
  interface FakeClient {
    child: {kill: (signal?: string) => void; killed: boolean};
    sendCommand: (command: unknown) => void;
    cancel: (runId: string) => void;
    on: (event: string, listener: Listener) => FakeClient;
    emit: (event: string, ...args: unknown[]) => void;
    removeAllListeners: () => FakeClient;
    // Set when App's interpret-effect cleanup detaches this client. That cleanup runs
    // in the same passive-effect flush that (re)registers useInput handlers after the
    // artifact's state change, so waiting on it makes keyboard input race-free here.
    cleared: boolean;
  }
  const clients: Record<'setup' | 'interpret' | 'run', FakeClient[]> = {setup: [], interpret: [], run: []};
  const makeClient = (kind: 'setup' | 'interpret' | 'run'): FakeClient => {
    const listeners = new Map<string, Listener[]>();
    const client: FakeClient = {
      child: {killed: false, kill: () => { client.child.killed = true; }},
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
  };
  return {clients, makeClient, reset};
});

vi.mock('../../tui/bridge.js', async importOriginal => {
  const actual = await importOriginal<typeof import('../../tui/bridge.js')>();
  return {
    ...actual,
    spawnSetupStatus: vi.fn(() => fake.makeClient('setup')),
    spawnKeywordInterpretation: vi.fn(() => fake.makeClient('interpret')),
    spawnBridge: vi.fn(() => fake.makeClient('run')),
  };
});

const interpretationArtifact = (context: string): BridgeEvent => ({
  type: 'artifact_ready',
  runId: 'interp_test',
  timestamp: '2026-07-02T00:00:00Z',
  phase: 'config',
  taskId: 'topic-interpretation',
  message: 'Topic interpretation ready',
  data: {artifact: 'topic_interpretation', context},
});

// Emit the artifact and wait for the resulting render before typing into the newly
// mounted context input. Request effects no longer clean up on unrelated state commits.
const emitInterpretation = async (context: string) => {
  const client = fake.clients.interpret.at(-1);
  client?.emit('events', [interpretationArtifact(context)]);
  await vi.waitFor(() => expect(client?.cleared).toBe(true));
  await tick();
};

describe('context journey step', () => {
  beforeEach(() => {
    fake.reset();
    vi.mocked(spawnKeywordInterpretation).mockClear();
    vi.mocked(spawnBridge).mockClear();
  });

  it('walks start › interpret › steer › regenerate › run', async () => {
    const {lastFrame, stdin, unmount} = render(<App config={{keyword: 'lead routing', style: 'guide'}} />);
    await tick();

    // Start generation routes to the context step and auto-triggers interpretation.
    // (waitFor: the spawn happens in a passive effect, which can lag under load.)
    stdin.write('s');
    await vi.waitFor(() => expect(spawnKeywordInterpretation).toHaveBeenCalledTimes(1));
    await tick();
    expect(lastFrame()).toContain('Interpreting topic — Gemini Flash');
    expect(vi.mocked(spawnKeywordInterpretation).mock.calls[0]?.[2]).toEqual({feedback: undefined, previous: undefined});

    // The interpretation artifact lands: the two-sentence take renders.
    await emitInterpretation('Lead routing assigns inbound leads to reps. This piece targets ops managers.');
    expect(lastFrame()).toContain('Lead routing assigns inbound leads to reps.');
    expect(lastFrame()).not.toContain('Interpreting topic');

    // Typed feedback = steered regeneration carrying feedback + the previous take.
    for (const char of 'sharper') {
      stdin.write(char);
      await tick();
    }
    stdin.write('\r');
    await vi.waitFor(() => expect(spawnKeywordInterpretation).toHaveBeenCalledTimes(2));
    await tick();
    expect(vi.mocked(spawnKeywordInterpretation).mock.calls[1]?.[2]).toEqual({
      feedback: 'sharper',
      previous: 'Lead routing assigns inbound leads to reps. This piece targets ops managers.',
    });
    expect(lastFrame()).toContain('take 2');

    await emitInterpretation('Second take on lead routing.');
    expect(lastFrame()).toContain('Second take on lead routing.');

    // Ctrl+N = fresh regenerate: attempt bumps, no feedback — but the previous
    // take rides along so the bridge prompt is never byte-identical (an
    // identical prompt could return the exact same interpretation).
    stdin.write('\x0e');
    await vi.waitFor(() => expect(spawnKeywordInterpretation).toHaveBeenCalledTimes(3));
    await tick();
    expect(vi.mocked(spawnKeywordInterpretation).mock.calls[2]?.[2]).toEqual({
      feedback: undefined,
      previous: 'Second take on lead routing.',
    });
    expect(lastFrame()).toContain('take 3');

    await emitInterpretation('Third take on lead routing.');

    // Enter accepts and starts the run with the confirmed context.
    stdin.write('\r');
    await vi.waitFor(() => expect(spawnBridge).toHaveBeenCalledTimes(1));
    await tick();
    expect(lastFrame()).toContain('Generating');
    const runConfig = vi.mocked(spawnBridge).mock.calls[0]?.[0];
    expect(runConfig?.keywordContext).toBe('Third take on lead routing.');
    unmount();
  });

  it('returns to config settings on Esc without starting a run', async () => {
    const {lastFrame, stdin, unmount} = render(<App config={{keyword: 'lead routing', style: 'guide'}} />);
    await tick();
    stdin.write('s');
    await vi.waitFor(() => expect(fake.clients.interpret.length).toBeGreaterThan(0));
    await tick();
    await emitInterpretation('A take to walk away from.');
    stdin.write(''); // Esc
    await tick();
    expect(lastFrame()).toContain('Generation Config');
    expect(spawnBridge).not.toHaveBeenCalled();
    unmount();
  });

  it('cancels interpretation before generation and ignores late interpretation output', async () => {
    const {stdin, unmount} = render(<App config={{keyword: 'lead routing', style: 'guide'}} />);
    await tick();
    stdin.write('s');
    await vi.waitFor(() => expect(fake.clients.interpret.length).toBe(1));
    const interpretation = fake.clients.interpret[0]!;

    stdin.write('\r');
    await vi.waitFor(() => expect(spawnBridge).toHaveBeenCalledTimes(1));
    const run = fake.clients.run[0]!;
    expect(interpretation.cleared).toBe(true);
    expect(interpretation.child.killed).toBe(true);

    interpretation.emit('events', [interpretationArtifact('too late')]);
    await tick();
    expect(run.child.killed).toBe(false);
    unmount();
  });

  it('stays on the context step when interpretation fails', async () => {
    const {lastFrame, stdin, unmount} = render(<App config={{keyword: 'lead routing', style: 'guide'}} />);
    await tick();
    stdin.write('s');
    await vi.waitFor(() => expect(fake.clients.interpret.length).toBeGreaterThan(0));
    await tick();
    fake.clients.interpret.at(-1)?.emit('events', [
      {
        type: 'error',
        runId: 'interp_test',
        timestamp: '2026-07-02T00:00:00Z',
        phase: 'config',
        taskId: 'topic-interpretation',
        message: 'Topic interpretation failed: no API key',
      } satisfies BridgeEvent,
    ]);
    await tick();
    expect(lastFrame()).toContain('Topic interpretation failed');
    expect(lastFrame()).toContain('Topic Context');
    unmount();
  });
});
