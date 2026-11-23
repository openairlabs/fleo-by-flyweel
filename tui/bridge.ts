import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {existsSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {fileURLToPath} from 'node:url';

import type {BridgeCommand, BridgeEvent, GenerationConfig} from './types.js';

export interface BridgeClient extends EventEmitter {
  child: ChildProcessWithoutNullStreams;
  sendCommand: (command: BridgeCommand) => void;
  cancel: (runId: string) => void;
}

const moduleDirectory = path.dirname(fileURLToPath(import.meta.url));
export const installationRoot = [
  path.resolve(moduleDirectory, '..'),
  path.resolve(moduleDirectory, '../..'),
].find(candidate => existsSync(path.join(candidate, 'tui_bridge.py'))) ?? path.resolve(moduleDirectory, '..');

export const bridgePath = (): string => path.join(installationRoot, 'tui_bridge.py');

export const preferredPython = (): string => {
  if (process.env.FLEO_PYTHON) return process.env.FLEO_PYTHON;
  if (process.env.FLYWEEL_PYTHON) return process.env.FLYWEEL_PYTHON;
  const homeVenv = path.join(os.homedir(), '.fleo', 'venv');
  const candidates = [
    path.join(installationRoot, 'venv', 'bin', 'python'),
    path.join(installationRoot, 'venv', 'Scripts', 'python.exe'),
    path.join(installationRoot, '.venv', 'bin', 'python'),
    path.join(installationRoot, '.venv', 'Scripts', 'python.exe'),
    path.join(homeVenv, 'bin', 'python'),
    path.join(homeVenv, 'Scripts', 'python.exe'),
  ];
  return candidates.find(existsSync) ?? (process.platform === 'win32' ? 'python' : 'python3');
};

export const MAX_JSONL_FRAME_BYTES = 1024 * 1024;
const MAX_CAPTURE_BYTES = 4 * MAX_JSONL_FRAME_BYTES;
const EVENT_TYPES = new Set<BridgeEvent['type']>([
  'setup_status',
  'run_plan',
  'phase_started',
  'task_progress',
  'tool_call',
  'warning',
  'error',
  'artifact_ready',
  'artifact_conflict',
  'review_gate_opened',
  'review_gate_closed',
  'gsc_gate_opened',
  'gsc_gate_closed',
  'generation_complete',
  'saved',
  'published',
]);
const TASK_STATUSES = new Set(['pending', 'running', 'complete', 'failed', 'warning', 'error', 'skipped']);
const isRecord = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value);

export interface ParsedBridgeEvent {
  event?: BridgeEvent;
  diagnostic?: string;
}

// Strip OSC (clipboard/hyperlink/title), CSI/SGR, and C0/C1 controls while keeping \t and \n.
export const stripTerminalEscapes = (value: string): string =>
  value
    .replace(/\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)?/g, '')
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]?/g, '')
    .replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g, '');

export const parseBridgeEventLine = (line: string): ParsedBridgeEvent => {
  if (Buffer.byteLength(line, 'utf8') > MAX_JSONL_FRAME_BYTES) {
    return {diagnostic: `Malformed bridge event: JSONL frame exceeds ${MAX_JSONL_FRAME_BYTES} bytes`};
  }
  let value: unknown;
  try {
    value = JSON.parse(line, (_k, v) => (typeof v === 'string' ? stripTerminalEscapes(v) : v));
  } catch {
    return {diagnostic: `Malformed bridge output: ${stripTerminalEscapes(line.slice(0, 160))}`};
  }
  if (!isRecord(value)) return {diagnostic: 'Malformed bridge event: expected an object'};
  const type = value.type;
  if (typeof type !== 'string' || !EVENT_TYPES.has(type as BridgeEvent['type'])) {
    return {diagnostic: `Malformed bridge event: unsupported type ${String(type)}`};
  }
  if (typeof value.runId !== 'string' || !value.runId || typeof value.timestamp !== 'string' || !value.timestamp) {
    return {diagnostic: 'Malformed bridge event: runId and timestamp are required strings'};
  }
  for (const field of ['phase', 'agent', 'taskId', 'message'] as const) {
    if (value[field] !== undefined && typeof value[field] !== 'string') {
      return {diagnostic: `Malformed bridge event: ${field} must be a string`};
    }
  }
  const data = value.data;
  if (type === 'setup_status') {
    if (!isRecord(data) || !Array.isArray(data.checks) || typeof data.canGenerate !== 'boolean') {
      return {diagnostic: 'Malformed bridge event: setup_status data shape is invalid'};
    }
  } else if (type === 'run_plan') {
    if (!isRecord(data) || !Array.isArray(data.plan)) {
      return {diagnostic: 'Malformed bridge event: run_plan data shape is invalid'};
    }
  } else if (type === 'task_progress' && data !== undefined) {
    if (!isRecord(data) || (data.status !== undefined && (typeof data.status !== 'string' || !TASK_STATUSES.has(data.status)))) {
      return {diagnostic: 'Malformed bridge event: task_progress data shape is invalid'};
    }
  } else if (type === 'artifact_ready') {
    if (!isRecord(data) || typeof data.artifact !== 'string') {
      return {diagnostic: 'Malformed bridge event: artifact_ready data shape is invalid'};
    }
  } else if (['saved', 'published', 'artifact_conflict', 'generation_complete'].includes(type)) {
    if (data !== undefined && !isRecord(data)) {
      return {diagnostic: `Malformed bridge event: ${type} data must be an object`};
    }
  }
  return {event: value as unknown as BridgeEvent};
};

// Styles whose whole point is competitor coverage — the engine ignores
// --no-competitors for them, so the TUI never sends it.
export const COMPARE_STYLES = new Set<GenerationConfig['style']>(['comparison', 'top-compare']);

export const buildBridgeArgs = (config: GenerationConfig, runId: string): string[] => {
  const args = ['tui_bridge.py', 'generate', '--run-id', runId, '--keyword', config.keyword, '--style', config.style, '--depth', config.depth, '--community-mode', config.communityMode, '--flyweel-mode', config.flyweelMode, '--solutions', String(config.solutionCount)];
  if (!config.icpEnabled) args.push('--no-icp');
  if (!config.gscCheck) args.push('--no-gsc-check');
  if (!config.gscKeywords) args.push('--no-gsc-keywords');
  if (!config.competitorMentions && !COMPARE_STYLES.has(config.style)) args.push('--no-competitors');
  if (config.note) args.push('--note', config.note);
  if (config.keywordContext) args.push('--keyword-context', config.keywordContext);
  if (config.competitors) args.push('--competitors', config.competitors);
  if (config.saveDefault === 'local') args.push('--save-local');
  return args;
};

// Wire a spawned child's stdio onto the client. stdout JSONL lines that arrive in
// the same chunk are coalesced into ONE `events` emission so the UI dispatches (and
// re-renders) once per burst instead of once per line — this is what keeps keyboard
// input responsive while the Python pipeline streams progress.
const wireClient = (client: BridgeClient, child: ChildProcessWithoutNullStreams): void => {
  client.child = child;
  let buffer = '';

  child.stdout.setEncoding('utf8');
  child.stderr.setEncoding('utf8');

  child.stdout.on('data', (chunk: string | Buffer) => {
    buffer += typeof chunk === 'string' ? chunk : chunk.toString('utf8');
    if (Buffer.byteLength(buffer, 'utf8') > MAX_JSONL_FRAME_BYTES && !buffer.includes('\n')) {
      client.emit('diagnostic', `Malformed bridge output: JSONL frame exceeds ${MAX_JSONL_FRAME_BYTES} bytes`);
      buffer = '';
      child.kill('SIGTERM');
      return;
    }
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? '';
    const events: BridgeEvent[] = [];
    for (const line of lines) {
      if (!line.trim()) continue;
      const parsed = parseBridgeEventLine(line);
      if (parsed.event) events.push(parsed.event);
      if (parsed.diagnostic) client.emit('diagnostic', parsed.diagnostic);
    }
    if (events.length > 0) client.emit('events', events);
  });

  child.stderr.on('data', (chunk: string | Buffer) => {
    client.emit('diagnostic', stripTerminalEscapes(typeof chunk === 'string' ? chunk : chunk.toString('utf8')).trimEnd());
  });

  let exited = false;
  const emitExitOnce = (code: number | null, signal: NodeJS.Signals | null) => {
    if (exited) return;
    exited = true;
    client.emit('exit', code, signal);
  };

  child.on('close', (code, signal) => {
    emitExitOnce(code, signal);
  });
  child.on('error', error => {
    client.emit('diagnostic', `Bridge spawn failed: ${error.message}`);
    emitExitOnce(127, null);
  });
  child.stdin.on('error', error => {
    client.emit('diagnostic', `Bridge stdin failed: ${error.message}`);
  });

  client.sendCommand = command => {
    if (child.stdin.destroyed || !child.stdin.writable) {
      client.emit('diagnostic', 'Bridge stdin is not writable');
      return;
    }
    child.stdin.write(`${JSON.stringify(command)}\n`, error => {
      if (error) client.emit('diagnostic', `Bridge stdin failed: ${error.message}`);
    });
  };
  client.cancel = id => {
    client.sendCommand({type: 'cancel', runId: id});
  };
};

const spawnBridgeChild = (args: string[]) =>
  spawn(preferredPython(), args[0] === 'tui_bridge.py' ? [bridgePath(), ...args.slice(1)] : args, {
    cwd: installationRoot,
    env: {...process.env, FLEO_WORKDIR: process.env.FLEO_WORKDIR || process.cwd(), PYTHONIOENCODING: 'utf-8'},
    stdio: ['pipe', 'pipe', 'pipe'],
  });

const spawnPython = (args: string[]): BridgeClient => {
  const client = new EventEmitter() as BridgeClient;
  wireClient(client, spawnBridgeChild(args));
  return client;
};

export const spawnBridge = (config: GenerationConfig, runId: string): BridgeClient => spawnPython(buildBridgeArgs(config, runId));

export const spawnSetupStatus = (runId: string): BridgeClient => spawnPython(['tui_bridge.py', 'setup-status', '--run-id', runId]);

export interface InterpretOptions {
  // Steering note typed by the user; sent together with the take it refers to.
  feedback?: string;
  previous?: string;
}

export const spawnKeywordInterpretation = (config: GenerationConfig, runId: string, opts: InterpretOptions = {}): BridgeClient => {
  const args = ['tui_bridge.py', 'interpret-keyword', '--run-id', runId, '--keyword', config.keyword, '--style', config.style];
  if (opts.feedback) args.push('--feedback', opts.feedback);
  if (opts.previous) args.push('--previous', opts.previous);
  return spawnPython(args);
};

export interface SuggestOptions {
  steer?: string;
  previous?: string[];
  trendsFresh?: boolean;
  target?: number;
  mode?: 'keyword' | 'angle';
}

export const buildSuggestArgs = (config: GenerationConfig, runId: string, options: SuggestOptions = {}): string[] => {
  const args = ['tui_bridge.py', 'suggest-keywords', '--run-id', runId, '--style', config.style];
  if (config.keyword.trim()) args.push('--seed', config.keyword.trim());
  if (options.steer) args.push('--steer', options.steer);
  if (options.previous?.length) args.push('--previous', JSON.stringify(options.previous));
  if (options.target !== undefined) args.push('--target', String(options.target));
  if (options.trendsFresh) args.push('--trends-fresh');
  if (options.mode) args.push('--mode', options.mode);
  return args;
};

export const spawnKeywordSuggestions = (config: GenerationConfig, runId: string, options: SuggestOptions = {}): BridgeClient =>
  spawnPython(buildSuggestArgs(config, runId, options));

// Parse a buffered JSONL stdout capture into bridge events, skipping malformed lines.
export const parseJsonlEvents = (stdout: string): BridgeEvent[] =>
  stdout
    .split(/\r?\n/)
    .filter(Boolean)
    .flatMap(line => {
      const parsed = parseBridgeEventLine(line);
      return parsed.event ? [parsed.event] : [];
    });

export interface BridgeOnceResult {
  stdout: string;
  stderr: string;
  code: number | null;
  signal?: NodeJS.Signals | null;
  timedOut?: boolean;
}

export interface BridgeOnceOptions {
  timeoutMs?: number;
  signal?: AbortSignal;
}

// One-shot bridge invocation for short-lived subcommands (save-artifact, publish-artifact,
// save-setup). Async on purpose: spawnSync would freeze the Ink event loop and the whole UI.
export const runBridgeOnce = (args: string[], input?: string, options: BridgeOnceOptions = {}): Promise<BridgeOnceResult> =>
  new Promise(resolve => {
    const child = spawnBridgeChild(args);
    let stdout = '';
    let stderr = '';
    let settled = false;
    let timedOut = false;
    const finish = (result: BridgeOnceResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      options.signal?.removeEventListener('abort', abort);
      resolve({...result, stderr: stripTerminalEscapes(result.stderr)});
    };
    const terminate = (reason: string, timeout = false) => {
      timedOut = timedOut || timeout;
      stderr = stderr ? `${stderr}\n${reason}` : reason;
      child.kill('SIGTERM');
    };
    const abort = () => terminate('Bridge command cancelled');
    const timer = setTimeout(
      () => terminate(`Bridge command timed out after ${options.timeoutMs ?? 30_000}ms`, true),
      options.timeoutMs ?? 30_000,
    );
    options.signal?.addEventListener('abort', abort, {once: true});
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk: string | Buffer) => {
      stdout += typeof chunk === 'string' ? chunk : chunk.toString('utf8');
      if (Buffer.byteLength(stdout, 'utf8') > MAX_CAPTURE_BYTES) terminate('Bridge stdout exceeded capture limit');
    });
    child.stderr.on('data', (chunk: string | Buffer) => {
      stderr += typeof chunk === 'string' ? chunk : chunk.toString('utf8');
      if (Buffer.byteLength(stderr, 'utf8') > MAX_CAPTURE_BYTES) terminate('Bridge stderr exceeded capture limit');
    });
    child.on('error', error => {
      finish({stdout, stderr: stderr || error.message, code: null, timedOut});
    });
    child.on('close', (code, signal) => {
      finish({stdout, stderr, code, signal, timedOut});
    });
    child.stdin.on('error', error => {
      stderr = stderr ? `${stderr}\n${error.message}` : error.message;
    });
    if (input !== undefined) {
      child.stdin.write(input, error => {
        if (error) terminate(`Bridge stdin failed: ${error.message}`);
      });
      child.stdin.end();
    } else {
      child.stdin.end();
    }
  });

// Test helper: replay a fixture of events through a per-event dispatcher (no subprocess).
export const replayFixture = (events: BridgeEvent[], dispatch: (event: BridgeEvent) => void): void => {
  for (const event of events) dispatch(event);
};
