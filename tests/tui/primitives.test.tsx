import {EventEmitter} from 'node:events';
import {cpSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import React from 'react';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {render} from 'ink-testing-library';

import {App} from '../../tui/App.js';
import {
  MAX_JSONL_FRAME_BYTES,
  bridgePath,
  parseBridgeEventLine,
  parseJsonlEvents,
  preferredPython,
  runBridgeOnce,
} from '../../tui/bridge.js';
import {buildProgram} from '../../tui/cli.js';
import {TextInput} from '../../tui/components/textInput.js';
import {ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE, enterAltScreen} from '../../tui/lib/altScreen.js';
import {createQuitHandler} from '../../tui/lib/quit.js';
import {getTheme, themes} from '../../tui/lib/terminal-themes/index.js';
import {buildInkUITheme} from '../../tui/lib/terminal-themes/inkui.js';
import {createTheme} from '../../tui/lib/terminal-themes/theme.js';
import {wrapText} from '../../tui/lib/wrap.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 40));

// ---------------------------------------------------------------------------
// CLI Commander Validation & Package Launcher
// ---------------------------------------------------------------------------

describe('Commander runtime validation', () => {
  it.each([
    ['--style', 'not-a-style'],
    ['--depth', 'deepest'],
    ['--community-mode', 'sometimes'],
    ['--flyweel-mode', 'maximum'],
  ])('rejects invalid %s choices before starting the app', async (flag, value) => {
    const stderrWrite = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    const program = buildProgram().exitOverride().configureOutput({writeErr: () => {}});
    program.commands.forEach(command => command.exitOverride().configureOutput({writeErr: () => {}}));
    try {
      await expect(
        program.parseAsync(['generate', '--keyword', 'test', flag, value, '--no-tui'], {from: 'user'}),
      ).rejects.toMatchObject({code: 'commander.invalidArgument'});
      expect(stderrWrite).not.toHaveBeenCalled();
    } finally {
      stderrWrite.mockRestore();
    }
  });
});

describe('package launcher (bin/fleo.js)', () => {
  it('preserves a child termination signal', () => {
    const root = mkdtempSync(path.join(tmpdir(), 'fleo-launcher-'));
    try {
      mkdirSync(path.join(root, 'bin'), {recursive: true});
      mkdirSync(path.join(root, 'dist', 'tui'), {recursive: true});
      cpSync(path.resolve('bin/fleo.js'), path.join(root, 'bin', 'fleo.js'));
      writeFileSync(path.join(root, 'package.json'), JSON.stringify({version: '0.1.0'}), 'utf8');
      writeFileSync(path.join(root, 'dist', 'tui', 'cli.js'), "process.kill(process.pid, 'SIGTERM');\n", 'utf8');

      const result = spawnSync(process.execPath, [path.join(root, 'bin', 'fleo.js')]);
      expect(result.status).toBeNull();
      expect(result.signal).toBe('SIGTERM');
    } finally {
      rmSync(root, {recursive: true, force: true});
    }
  });

  it('fast-paths --version without spawning TUI', () => {
    const result = spawnSync(process.execPath, [path.resolve('bin/fleo.js'), '--version'], {encoding: 'utf8'});
    expect(result.status).toBe(0);
    expect(result.stdout.trim()).toBe('0.1.0');
  });
});

// ---------------------------------------------------------------------------
// JSONL Bridge Protocol & Process Lifecycle
// ---------------------------------------------------------------------------

const originalCwd = process.cwd();
const originalPython = process.env.FLYWEEL_PYTHON;
const originalFleoPython = process.env.FLEO_PYTHON;

afterEach(() => {
  process.chdir(originalCwd);
  if (originalPython === undefined) delete process.env.FLYWEEL_PYTHON;
  else process.env.FLYWEEL_PYTHON = originalPython;
  if (originalFleoPython === undefined) delete process.env.FLEO_PYTHON;
  else process.env.FLEO_PYTHON = originalFleoPython;
});

describe('bridge protocol validation & lifecycle', () => {
  it('accepts valid events and unknown additive fields', () => {
    const parsed = parseBridgeEventLine(JSON.stringify({
      type: 'task_progress',
      runId: 'run_1',
      timestamp: '2026-07-11T00:00:00Z',
      phase: 'draft',
      data: {status: 'running', label: 'Part 1/3'},
      futureField: {allowed: true},
    }));
    expect(parsed.event?.type).toBe('task_progress');
    expect(parsed.diagnostic).toBeUndefined();
  });

  it.each([
    ['unknown discriminator', {type: 'made_up', runId: 'run_1', timestamp: 'now'}],
    ['missing runId', {type: 'warning', timestamp: 'now', message: 'x'}],
    ['malformed setup payload', {type: 'setup_status', runId: 'run_1', timestamp: 'now', data: {checks: 'nope'}}],
    ['malformed task status', {type: 'task_progress', runId: 'run_1', timestamp: 'now', data: {status: 'banana'}}],
  ])('turns %s into a diagnostic', (_label, value) => {
    const parsed = parseBridgeEventLine(JSON.stringify(value));
    expect(parsed.event).toBeUndefined();
    expect(parsed.diagnostic).toContain('Malformed bridge event');
  });

  it('bounds JSONL frames and drops malformed captures', () => {
    const parsed = parseBridgeEventLine('x'.repeat(MAX_JSONL_FRAME_BYTES + 1));
    expect(parsed.event).toBeUndefined();
    expect(parsed.diagnostic).toContain('frame exceeds');
    expect(parseJsonlEvents('{"type":"warning"}\nnot-json')).toEqual([]);
  });

  it('resolves the bridge from installation root rather than cwd', () => {
    const temporary = mkdtempSync(path.join(tmpdir(), 'fleo-cwd-'));
    try {
      delete process.env.FLYWEEL_PYTHON;
      delete process.env.FLEO_PYTHON;
      process.chdir(temporary);
      expect(bridgePath()).toBe(path.join(originalCwd, 'tui_bridge.py'));
      expect(existsSync(bridgePath())).toBe(true);
      expect(preferredPython()).toBeTruthy();
    } finally {
      process.chdir(originalCwd);
      rmSync(temporary, {recursive: true, force: true});
    }
  });

  it('reports a missing Python spawn and times out stuck commands without hanging', async () => {
    const missingBin = path.join(tmpdir(), 'definitely-missing-python');
    process.env.FLEO_PYTHON = missingBin;
    process.env.FLYWEEL_PYTHON = missingBin;
    const missing = await runBridgeOnce(['tui_bridge.py', '--help'], undefined, {timeoutMs: 500});
    expect(missing.code).toBeNull();
    expect(missing.stderr).toMatch(/ENOENT|missing/i);

    delete process.env.FLEO_PYTHON;
    delete process.env.FLYWEEL_PYTHON;
    const stuck = await runBridgeOnce(['-c', 'import time; time.sleep(5)'], undefined, {timeoutMs: 50});
    expect(stuck.timedOut).toBe(true);
    expect(stuck.stderr).toContain('timed out');
  });

  it('decodes multibyte UTF-8 characters split across stdout chunks without replacement glyphs', async () => {
    delete process.env.FLEO_PYTHON;
    delete process.env.FLYWEEL_PYTHON;
    const pyScript = [
      'import os, sys, time',
      'payload = \'{"type":"warning","runId":"r1","timestamp":"now","message":"Launch 🚀 ready"}\\n\'.encode("utf-8")',
      'idx = payload.index(b"\\xf0\\x9f") + 2',
      'os.write(1, payload[:idx])',
      'time.sleep(0.03)',
      'os.write(1, payload[idx:])',
    ].join('; ');
    const res = await runBridgeOnce(['-c', pyScript], undefined, {timeoutMs: 5000});
    expect(res.code).toBe(0);
    expect(res.stdout).toContain('Launch 🚀 ready');
    expect(res.stdout).not.toContain('\uFFFD');
  });

  it('forces PYTHONIOENCODING=utf-8 on piped Python streams even when parent env is non-UTF-8', async () => {
    delete process.env.FLEO_PYTHON;
    delete process.env.FLYWEEL_PYTHON;
    const prevEncoding = process.env.PYTHONIOENCODING;
    process.env.PYTHONIOENCODING = 'ascii';
    try {
      const res = await runBridgeOnce(['-c', 'import os, sys; print(sys.stdin.read().strip(), "→", sys.stdout.encoding, os.environ.get("FLEO_WORKDIR", ""))'], 'café\n', {timeoutMs: 5000});
      expect(res.code).toBe(0);
      expect(res.stdout).toContain('café → utf-8');
    } finally {
      if (prevEncoding === undefined) delete process.env.PYTHONIOENCODING;
      else process.env.PYTHONIOENCODING = prevEncoding;
    }
  });
});

// ---------------------------------------------------------------------------
// Text Wrapping, Alt Screen, Chord-Safe TextInput, Quit Handler, and Themes
// ---------------------------------------------------------------------------

describe('wrapText & Unicode grapheme safety', () => {
  it('wraps words, hard-breaks long tokens, preserves blanks, and handles wide emoji', () => {
    expect(wrapText('alpha beta gamma delta', 11)).toEqual(['alpha beta', 'gamma delta']);
    expect(wrapText('x'.repeat(20), 8)).toEqual(['xxxxxxxx', 'xxxxxxxx', 'xxxx']);
    expect(wrapText('first\n\n  indented tail', 40)).toEqual(['first', '', '  indented tail']);
    expect(wrapText('🙂🙂🙂🙂🙂', 8)).toEqual(['🙂🙂🙂🙂', '🙂']);
    const family = '👨‍👩‍👧‍👦';
    expect(wrapText(`${family}${family}${family}${family}${family}`, 8)).toEqual([
      `${family}${family}${family}${family}`,
      family,
    ]);
  });
});

describe('enterAltScreen', () => {
  const makeStream = () => {
    const writes: string[] = [];
    return {writes, stream: {write: (chunk: string) => writes.push(chunk)}};
  };
  const makeProc = () => {
    const exits: Array<number | undefined> = [];
    const emitter = new EventEmitter();
    const proc = Object.assign(emitter, {
      exit: (code?: number) => {
        exits.push(code);
        emitter.emit('exit');
      },
    });
    return {proc, exits};
  };

  it('enters immediately, restores idempotently on teardown/exit, and exits 143 on SIGTERM', () => {
    const {writes, stream} = makeStream();
    const {proc, exits} = makeProc();
    const leave = enterAltScreen(stream, proc);
    expect(writes).toEqual([ALT_SCREEN_ENTER]);
    leave();
    leave();
    proc.emit('exit');
    expect(writes).toEqual([ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE]);

    const s2 = makeStream();
    const p2 = makeProc();
    enterAltScreen(s2.stream, p2.proc);
    p2.proc.emit('SIGTERM');
    expect(s2.writes).toEqual([ALT_SCREEN_ENTER, ALT_SCREEN_LEAVE]);
    expect(p2.exits).toEqual([143]);
  });
});

describe('TextInput (chord-safe)', () => {
  const type = async (stdin: {write: (data: string) => void}, text: string) => {
    for (const char of text) {
      stdin.write(char);
      await tick();
    }
  };

  it('submits typed text, drops Ctrl chords, handles backspace, and renders placeholder', async () => {
    const onSubmit = vi.fn();
    const {stdin, lastFrame, unmount} = render(<TextInput onSubmit={onSubmit} placeholder="e.g. topic" />);
    await tick();
    expect(lastFrame()).toContain('.g. topic');
    await type(stdin, 'lead');
    for (const chord of ['\x14', '\x07', '\x0e']) {
      stdin.write(chord);
      await tick();
    }
    expect(lastFrame()).not.toContain('leadt');
    stdin.write('\r');
    await tick();
    expect(onSubmit).toHaveBeenCalledWith('lead');
    unmount();
  });
});

describe('double Ctrl+C quit handler', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('arms on first Ctrl+C, disarms after 2s, and exits on second press within window', async () => {
    vi.useFakeTimers();
    const exit = vi.fn();
    const setArmed = vi.fn();
    const handler = createQuitHandler({exit, setArmed});
    handler('c', {ctrl: true});
    expect(setArmed).toHaveBeenLastCalledWith(true);
    vi.advanceTimersByTime(1000);
    handler('c', {ctrl: true});
    expect(exit).toHaveBeenCalledTimes(1);
    vi.useRealTimers();

    const {lastFrame, stdin, unmount} = render(<App noBridge config={{keyword: 'lead routing'}} />);
    stdin.write('\x03');
    await tick();
    expect(lastFrame()).toContain('press Ctrl+C again to quit');
    unmount();
  });
});

describe('terminal themes & WCAG AAA contrast', () => {
  const relativeLuminance = (hex: string): number => {
    const normalized = hex.replace('#', '');
    const [red, green, blue] = [0, 2, 4].map(start => Number.parseInt(normalized.slice(start, start + 2), 16));
    const linearize = (channel: number) => {
      const srgb = channel / 255;
      return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * linearize(red) + 0.7152 * linearize(green) + 0.0722 * linearize(blue);
  };
  const contrastRatio = (fg: string, bg: string): number => {
    const l1 = Math.max(relativeLuminance(fg), relativeLuminance(bg));
    const l2 = Math.min(relativeLuminance(fg), relativeLuminance(bg));
    return (l1 + 0.05) / (l2 + 0.05);
  };

  it('registers Flyweel mint theme at WCAG AAA contrast and maps onto @inkjs/ui', () => {
    for (const name of Object.keys(themes)) expect(getTheme(name).name).toBeTruthy();
    const theme = getTheme('theme-flyweel');
    expect(theme.colors.primary).toBe('#90DAB8');
    for (const token of [theme.colors.primary, theme.colors.accent, theme.colors.success, theme.colors.warning, theme.colors.error]) {
      expect(contrastRatio(token, '#0A0A0A')).toBeGreaterThanOrEqual(7);
    }
    expect(createTheme({name: 'custom', colors: {primary: '#123456'} as never}).colors.primary).toBe('#123456');
    expect(buildInkUITheme(theme).components.Select).toBeDefined();
  });
});
