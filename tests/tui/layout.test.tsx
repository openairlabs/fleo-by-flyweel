import React from 'react';
import {describe, expect, it} from 'vitest';
import {render} from 'ink-testing-library';

import {App} from '../../tui/App.js';
import {ANSI_LOGO_LINES} from '../../tui/components/chrome.js';
import {OptionList, windowRange} from '../../tui/components/termcn.js';
import {readTerminalSize} from '../../tui/lib/useTerminalSize.js';
import stringWidth from 'string-width';

const tick = () => new Promise(resolve => setTimeout(resolve, 40));

const frameLines = (frame: string | undefined): string[] => (frame ?? '').split('\n');

const SMALL = {columns: 80, rows: 20};

describe('height discipline', () => {
  it.each(['review', 'preview'] as const)('keeps %s usable in a narrow terminal', step => {
    const artifact = {
      id: step === 'review' ? 'pre' : 'final',
      data: {
        artifact: step === 'review' ? 'pre_polish_mdx' : 'final_mdx',
        content: Array.from({length: 40}, (_, index) => `Long content line ${index + 1}`).join('\n'),
        metadata: {title: 'A long generated title', slug: 'long-generated-title'},
      },
    };
    const {lastFrame, unmount} = render(
      <App
        noBridge
        size={{columns: 40, rows: 20}}
        initialState={{step, commandMode: step === 'review' ? 'review' : false, artifacts: [artifact]}}
        config={{keyword: 'a very long keyword for narrow terminals'}}
      />,
    );
    const lines = frameLines(lastFrame());
    expect(lines.length).toBeLessThanOrEqual(19);
    expect(Math.max(...lines.map(line => stringWidth(line)))).toBeLessThanOrEqual(40);
    expect(lastFrame()).toContain(step === 'review' ? 'Pre-polish' : 'Title');
    unmount();
  });

  it('keeps the config settings frame within a 20-row terminal', () => {
    const {lastFrame, unmount} = render(<App noBridge size={SMALL} config={{keyword: 'lead routing'}} />);
    const lines = frameLines(lastFrame());
    expect(lines.length).toBeLessThanOrEqual(SMALL.rows);
    // The 12-row settings list is windowed, not dropped: markers advertise the rest.
    expect(lastFrame()).toContain('Generation Config');
    expect(lastFrame()).toContain('… ');
    unmount();
  });

  it('keeps the help overlay frame within a 20-row terminal', async () => {
    const {lastFrame, stdin, unmount} = render(<App noBridge size={SMALL} initialState={{step: 'running'}} config={{keyword: 'lead routing'}} />);
    stdin.write('?');
    await tick();
    expect(lastFrame()).toContain('One journey');
    expect(frameLines(lastFrame()).length).toBeLessThanOrEqual(SMALL.rows);
    unmount();
  });

  it('shows the ANSI masthead only on the keyword stage of a tall terminal', () => {
    const tall = {columns: 100, rows: 40};
    const keywordStage = render(<App noBridge size={tall} />);
    expect(keywordStage.lastFrame()).toContain('▄');
    keywordStage.unmount();

    const settingsStage = render(<App noBridge size={tall} config={{keyword: 'lead routing'}} />);
    expect(settingsStage.lastFrame()).not.toContain('▄');
    // The brand collapses to its single line instead.
    expect(settingsStage.lastFrame()).toContain('by ');
    expect(settingsStage.lastFrame()).toContain('Flyweel');
    settingsStage.unmount();

    const shortKeywordStage = render(<App noBridge size={SMALL} />);
    expect(shortKeywordStage.lastFrame()).not.toContain('▄');
    shortKeywordStage.unmount();
  });

  it('keeps every masthead line intact when the body overflows (chrome never shrinks)', () => {
    // 25 unwindowed setup checks overflow the body budget at 36 rows; Yoga's shrink
    // pass must clip the body only — with default flexShrink the header would lose
    // logo lines instead ("layout shift on initiation" bug).
    const checks = Array.from({length: 25}, (_, index) => ({
      id: `check-${index}`,
      label: `Check ${index + 1}`,
      required: false,
      ok: true,
      detail: 'ok',
    }));
    const {lastFrame, unmount} = render(
      <App
        noBridge
        size={{columns: 100, rows: 36}}
        initialState={{step: 'setup', setupStatus: {checks, canGenerate: true, requiredMissing: [], optionalMissing: []}}}
      />,
    );
    const lines = frameLines(lastFrame());
    expect(lines.length).toBeLessThanOrEqual(36);
    const logoGlyphLines = ANSI_LOGO_LINES.filter(line => /[▄▀]/.test(line)).length;
    const renderedGlyphLines = lines.filter(line => /[▄▀]/.test(line)).length;
    expect(renderedGlyphLines).toBe(logoGlyphLines);
    unmount();
  });

  it('leaves one row of headroom below the frame (Ink fullscreen clear-per-render guard)', () => {
    // At exactly `rows` tall, Ink's renderer hard-clears the whole screen on every
    // render (a visible flash per keystroke on terminals without DEC-2026 support).
    // The root frame must therefore never reach the terminal height.
    for (const size of [SMALL, {columns: 100, rows: 40}]) {
      const keywordStage = render(<App noBridge size={size} />);
      expect(frameLines(keywordStage.lastFrame()).length).toBeLessThanOrEqual(size.rows - 1);
      keywordStage.unmount();

      const settingsStage = render(<App noBridge size={size} config={{keyword: 'lead routing'}} />);
      expect(frameLines(settingsStage.lastFrame()).length).toBeLessThanOrEqual(size.rows - 1);
      settingsStage.unmount();
    }
  });

  it('caps review queued comments at the last three with a +N more line', () => {
    const {lastFrame, unmount} = render(
      <App
        noBridge
        size={{columns: 100, rows: 24}}
        initialState={{
          step: 'review',
          commandMode: 'review',
          queuedComments: ['first note', 'second note', 'third note', 'fourth note', 'fifth note'],
        }}
        config={{keyword: 'lead routing'}}
      />,
    );
    const frame = lastFrame() ?? '';
    expect(frame).toContain('fifth note');
    expect(frame).toContain('third note');
    expect(frame).not.toContain('first note');
    expect(frame).toContain('+2 more');
    unmount();
  });
});

describe('OptionList windowing', () => {
  const options = Array.from({length: 12}, (_, index) => ({value: `v${index + 1}`, label: `Option ${index + 1}`}));

  it('windows long lists to the viewport with a more marker', () => {
    const {lastFrame, unmount} = render(<OptionList options={options} onSelect={() => {}} viewport={6} isActive={false} />);
    const frame = lastFrame() ?? '';
    expect(frameLines(frame).length).toBeLessThanOrEqual(6);
    expect(frame).toContain('Option 1');
    expect(frame).not.toContain('Option 12');
    expect(frame).toContain('… 8 more');
    unmount();
  });

  it('keeps the same height at the scroll ends as mid-list (marker slots always reserved)', () => {
    // Scrolling must never change the frame height: at the top/bottom of a windowed
    // list one marker is blank, not dropped — otherwise the whole frame wobbles by a
    // line as the focus crosses the window edge.
    const heightAt = (initialIndex: number): number => {
      const {lastFrame, unmount} = render(
        <OptionList options={options} initialIndex={initialIndex} onSelect={() => {}} viewport={6} isActive={false} />,
      );
      const height = frameLines(lastFrame()).length;
      unmount();
      return height;
    };
    const top = heightAt(0);
    const middle = heightAt(6);
    const bottom = heightAt(options.length - 1);
    expect(top).toBe(6);
    expect(middle).toBe(6);
    expect(bottom).toBe(6);
  });

  it('renders everything when the list fits the viewport', () => {
    const {lastFrame, unmount} = render(<OptionList options={options.slice(0, 4)} onSelect={() => {}} viewport={6} isActive={false} />);
    expect(lastFrame()).toContain('Option 4');
    expect(lastFrame()).not.toContain('…');
    unmount();
  });
});

describe('windowRange', () => {
  it('returns the whole range when it fits', () => {
    expect(windowRange(5, 2, 8)).toEqual({start: 0, end: 5});
  });

  it('centers the window on the focused index', () => {
    expect(windowRange(12, 6, 5)).toEqual({start: 4, end: 9});
  });

  it('clamps at the top and bottom of the list', () => {
    expect(windowRange(12, 0, 5)).toEqual({start: 0, end: 5});
    expect(windowRange(12, 11, 5)).toEqual({start: 7, end: 12});
  });

  it('tolerates out-of-range focus and tiny spans', () => {
    expect(windowRange(12, 99, 5)).toEqual({start: 7, end: 12});
    expect(windowRange(3, 1, 0)).toEqual({start: 1, end: 2});
  });
});

describe('readTerminalSize', () => {
  it('falls back to 80x24 when the stream reports nothing', () => {
    expect(readTerminalSize(undefined)).toEqual({columns: 80, rows: 24});
    expect(readTerminalSize({})).toEqual({columns: 80, rows: 24});
  });

  it('uses the stream dimensions when present', () => {
    expect(readTerminalSize({columns: 120, rows: 40})).toEqual({columns: 120, rows: 40});
  });
});
