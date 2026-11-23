import React from 'react';
import {describe, expect, it} from 'vitest';
import {render} from 'ink-testing-library';

import {App} from '../../tui/App.js';
import {ANSI_LOGO_LINES, BRAND_HEADER} from '../../tui/components/chrome.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 40));

const finalArtifact = {
  id: 'final-mdx',
  data: {
    artifact: 'final_mdx',
    content: '---\ntitle: Test Article\n---\n# Article\nSee [source](https://example.com).',
    rawContent: '# Raw Draft',
    metadata: {title: 'Test Article', slug: 'test-article', qualityScorecard: {composite: 90}},
  },
};

describe('Ink app shell', () => {
  it('renders generation config at the settings stage when a keyword is prefilled', () => {
    const {lastFrame, unmount} = render(<App noBridge config={{keyword: 'pipeline attribution', style: 'research'}} />);
    expect(lastFrame()).toContain('by ');
    expect(lastFrame()).toContain('Flyweel');
    expect(lastFrame()).not.toContain('FLEO');
    expect(lastFrame()).toContain('Generation Config');
    expect(lastFrame()).toContain('pipeline attribution');
    unmount();
  });

  it('stores the logo with real ANSI escape bytes', () => {
    expect(ANSI_LOGO_LINES[0]).toContain('\u001b[49m');
    expect(ANSI_LOGO_LINES[0]).not.toContain('\\e');
    expect(ANSI_LOGO_LINES[0]).not.toContain('\\x1b');
    expect(ANSI_LOGO_LINES).toHaveLength(12);
  });


  it('links the masthead Flyweel label to the website', () => {
    expect(BRAND_HEADER).toContain('by ');
    expect(BRAND_HEADER).toContain('Flyweel');
    expect(BRAND_HEADER).toContain('\u001b]8;;https://www.flyweel.co\u0007');
    expect(BRAND_HEADER).toContain('Flyweel\u001b]8;;\u0007');
  });


  it('starts the guided flow at the keyword step with no prefill', () => {
    const {lastFrame, unmount} = render(<App noBridge />);
    expect(lastFrame()).toContain('Generation Config');
    // Default mode is angle-driven, so the prompt asks for an angle / topic.
    expect(lastFrame()).toContain('angle / topic');
    unmount();
  });

  it('shows the Ctrl+G AI-ideas hint on the keyword stage', () => {
    const {lastFrame, unmount} = render(<App noBridge />);
    expect(lastFrame()).toContain('Ctrl+G AI ideas');
    unmount();
  });

  it('defaults to angle-driven mode with a fast trends indicator', () => {
    const {lastFrame, unmount} = render(<App noBridge />);
    const frame = lastFrame() ?? '';
    // Angle chip active (bracketed), keyword-stage label mode-aware, trends fast by default.
    expect(frame).toContain('▸ Angle-driven ◂');
    expect(frame).toContain('What angle / topic should this article take?');
    expect(frame).toContain('Trends: fast (Ctrl+T)');
    unmount();
  });

  it('flips the mode chip + keyword-stage label with Tab and does not inject a tab into the input', async () => {
    const {lastFrame, stdin, unmount} = render(<App noBridge />);
    expect(lastFrame()).toContain('▸ Angle-driven ◂');
    // Type, flip mid-stream, keep typing — the chip must flip and the input must stay
    // contiguous (no literal tab inserted by @inkjs/ui TextInput on Tab).
    stdin.write('f');
    await tick();
    stdin.write('l');
    await tick();
    stdin.write('\t'); // Tab -> keyword mode
    await tick();
    const flipped = lastFrame() ?? '';
    expect(flipped).toContain('▸ Keyword-driven ◂');
    expect(flipped).toContain('What keyword / topic should this article target?');
    stdin.write('o');
    await tick();
    stdin.write('w');
    await tick();
    // Contiguous "flow" proves Tab did not land a tab char between "fl" and "ow".
    expect(lastFrame()).toContain('flow');
    unmount();
  });

  it('flips the trends indicator with Ctrl+T on the config page', async () => {
    const {lastFrame, stdin, unmount} = render(<App noBridge />);
    expect(lastFrame()).toContain('Trends: fast (Ctrl+T)');
    stdin.write('\x14'); // Ctrl+T
    await tick();
    expect(lastFrame()).toContain('Trends: fresh (Ctrl+T)');
    stdin.write('\x14');
    await tick();
    expect(lastFrame()).toContain('Trends: fast (Ctrl+T)');
    unmount();
  });

  it('renders setup view without bridge', () => {
    const {lastFrame, unmount} = render(<App noBridge setupOnly />);
    expect(lastFrame()).toContain('Environment setup');
    expect(lastFrame()).toContain('Setup status has not been loaded yet');
    unmount();
  });

  it('renders setup status rows and staged setup values', () => {
    const {lastFrame, unmount} = render(
      <App
        noBridge
        initialState={{
          step: 'setup',
          setupStatus: {
            checks: [
              {id: 'venv', label: 'Python venv', required: true, ok: true, detail: 'venv/bin/python'},
              {id: 'openrouter', label: 'OPENROUTER_API_KEY', required: true, ok: false, detail: '.env or process env'},
            ],
            canGenerate: false,
            requiredMissing: ['openrouter'],
            optionalMissing: ['gsc'],
          },
          setupDraft: {OPENROUTER_API_KEY: 'sk-secret-value'},
        }}
      />,
    );
    expect(lastFrame()).toContain('Python venv');
    expect(lastFrame()).toContain('OPENROUTER_API_KEY');
    expect(lastFrame()).toContain('Staged changes');
    expect(lastFrame()).toContain('sk-...lue');
    unmount();
  });

  it('shows the journey trail with the active step highlighted', () => {
    const {lastFrame, unmount} = render(<App noBridge initialState={{step: 'running'}} config={{keyword: 'lead routing'}} />);
    expect(lastFrame()).toContain('Configure');
    expect(lastFrame()).toContain('Generate');
    expect(lastFrame()).toContain('Output');
    // No review row yet, so Review stays out of the trail.
    expect(lastFrame()).not.toContain('Review');
    unmount();
  });

  it('opens the keymap-derived help overlay with question mark', async () => {
    const {lastFrame, stdin, unmount} = render(<App noBridge initialState={{step: 'running'}} config={{keyword: 'lead routing'}} />);
    stdin.write('?');
    await tick();
    expect(lastFrame()).toContain('One journey');
    expect(lastFrame()).toContain('Esc cancel run');
    stdin.write('?');
    await tick();
    expect(lastFrame()).not.toContain('One journey');
    unmount();
  });

  it('opens the log drawer with l and shows log entries', async () => {
    const {lastFrame, stdin, unmount} = render(
      <App
        noBridge
        initialState={{
          step: 'running',
          log: [
            {id: 1, level: 'info', text: 'stderr: bridge log line'},
            {id: 2, level: 'tool', text: 'research: SERP tool'},
            {id: 3, level: 'warn', text: 'citation warning'},
            {id: 4, level: 'error', text: 'publish failed'},
          ],
          logSeq: 4,
        }}
        config={{keyword: 'lead routing'}}
      />,
    );
    stdin.write('l');
    await tick();
    expect(lastFrame()).toContain('bridge log line');
    expect(lastFrame()).toContain('SERP tool');
    expect(lastFrame()).toContain('citation warning');
    expect(lastFrame()).toContain('publish failed');
    stdin.write('l');
    await tick();
    expect(lastFrame()).not.toContain('bridge log line');
    unmount();
  });

  it('opens comment input and records typed text during a run', async () => {
    const {lastFrame, stdin, unmount} = render(<App noBridge initialState={{step: 'running', runId: 'run_x'}} config={{keyword: 'lead routing'}} />);
    stdin.write('c');
    await tick();
    expect(lastFrame()).toContain('comment');
    for (const char of 'tighten') {
      stdin.write(char);
      await tick();
    }
    expect(lastFrame()).toContain('tighten');
    unmount();
  });

  it('does not open comments when Ctrl+C arms quit', async () => {
    const {lastFrame, stdin, unmount} = render(<App noBridge initialState={{step: 'running', runId: 'run_x'}} />);
    stdin.write('\x03');
    await tick();
    expect(lastFrame()).toContain('press Ctrl+C again to quit');
    expect(lastFrame()).not.toContain('comment — Enter to queue');
    unmount();
  });

  it('hides an already-open comment editor while the GSC gate owns input', () => {
    const {lastFrame, unmount} = render(
      <App
        noBridge
        initialState={{
          step: 'running',
          commandMode: 'comment',
          gscGate: {score: 5, threshold: 25},
        }}
      />,
    );
    expect(lastFrame()).toContain('Still produce this article?');
    expect(lastFrame()).not.toContain('comment — Enter to queue');
    unmount();
  });

  it('renders running progress with in-place rows', () => {
    const {lastFrame, unmount} = render(
      <App
        noBridge
        initialState={{
          step: 'running',
          activePhase: 'research',
          progressRows: [{id: 'task:serp', label: 'SERP Analysis', phase: 'research', status: 'running', detail: 'Perplexity'}],
        }}
        config={{keyword: 'lead routing'}}
      />,
    );
    expect(lastFrame()).toContain('SERP Analysis');
    expect(lastFrame()).toContain('research');
    expect(lastFrame()).toContain('Generating — research');
    unmount();
  });

  it('renders review gate comments and pre-polish artifact exactly once', () => {
    const {lastFrame, unmount} = render(
      <App
        noBridge
        initialState={{
          step: 'review',
          commandMode: 'review',
          queuedComments: ['Make the proof more concrete'],
          artifacts: [{id: 'pre-polish', data: {artifact: 'pre_polish_mdx', content: '# Pre polish\nDraft body'}}],
          progressRows: [{id: 'task:comment-edit', label: 'Comment edit', phase: 'review', status: 'complete', detail: 'validated'}],
        }}
      />,
    );
    const frame = lastFrame() ?? '';
    expect(frame).toContain('Queued Comments');
    expect(frame).toContain('Make the proof more concrete');
    expect(frame).toContain('Pre polish');
    expect(frame.match(/validated/g)).toHaveLength(1);
    expect(frame).toContain('apply');
    expect(frame).toContain('rerun');
    unmount();
  });

  it('renders a human preview summary without JSON dumps', () => {
    const {lastFrame, unmount} = render(
      <App noBridge initialState={{step: 'preview', artifacts: [finalArtifact]}} config={{keyword: 'lead routing'}} />,
    );
    const frame = lastFrame() ?? '';
    expect(frame).toContain('Test Article');
    expect(frame).toContain('test-article');
    expect(frame).toContain('90 composite');
    expect(frame).toContain('# Article');
    expect(frame).not.toContain('qualityScorecard');
    expect(frame).not.toContain('{');
    unmount();
  });

  it('toggles the info pane with i on preview', async () => {
    const {lastFrame, stdin, unmount} = render(
      <App noBridge initialState={{step: 'preview', artifacts: [finalArtifact]}} config={{keyword: 'lead routing'}} />,
    );
    stdin.write('i');
    await tick();
    expect(lastFrame()).toContain('Citations');
    expect(lastFrame()).toContain('https://example.com');
    stdin.write('i');
    await tick();
    expect(lastFrame()).toContain('# Article');
    unmount();
  });

  it('scrolls a long article with the arrow keys', async () => {
    const longBody = Array.from({length: 120}, (_, index) => `Line ${index + 1} of the article body.`).join('\n');
    const artifact = {id: 'final-mdx', data: {artifact: 'final_mdx', content: `---\ntitle: Long\n---\n${longBody}`}};
    const {lastFrame, stdin, unmount} = render(
      <App noBridge initialState={{step: 'preview', artifacts: [artifact]}} config={{keyword: 'lead routing'}} />,
    );
    expect(lastFrame()).toContain('Line 1 of');
    stdin.write('\x1b[B'); // down arrow
    await tick();
    expect(lastFrame()).not.toContain('Line 1 of');
    expect(lastFrame()).toContain('lines 2–');
    stdin.write('g');
    await tick();
    expect(lastFrame()).toContain('Line 1 of');
    unmount();
  });

  it('toggles the Ctrl+P full-article view on preview', async () => {
    const longBody = Array.from({length: 120}, (_, index) => `Line ${index + 1} of the article body.`).join('\n');
    const artifact = {id: 'final-mdx', data: {artifact: 'final_mdx', content: `---\ntitle: Long\n---\n${longBody}`, metadata: {title: 'Long', slug: 'long-article'}}};
    const {lastFrame, stdin, unmount} = render(
      <App noBridge size={{columns: 80, rows: 24}} initialState={{step: 'preview', artifacts: [artifact]}} config={{keyword: 'lead routing'}} />,
    );
    // Normal preview: summary chrome + the ~10-line article window.
    expect(lastFrame()).toContain('Slug');
    expect(lastFrame()).toContain('lines 1–9 of 120');
    stdin.write('\x10'); // Ctrl+P -> full article
    await tick();
    expect(lastFrame()).not.toContain('Slug');
    expect(lastFrame()).toContain('lines 1–16 of 120');
    expect(lastFrame()).toContain('Line 1 of');
    stdin.write(''); // Esc leaves full-article mode, not the preview step
    await tick();
    expect(lastFrame()).toContain('Slug');
    expect(lastFrame()).toContain('lines 1–9 of 120');
    unmount();
  });

  it('renders the GSC gate confirm strip and proceeds on y', async () => {
    const {lastFrame, stdin, unmount} = render(
      <App
        noBridge
        initialState={{step: 'running', runId: 'run_x', gscGate: {score: 12, threshold: 25, trafficPotential: 1.4, primaryQuery: 'kw variant'}}}
        config={{keyword: 'lead routing'}}
      />,
    );
    expect(lastFrame()).toContain('GSC score 12%');
    expect(lastFrame()).toContain('below 25%');
    expect(lastFrame()).toContain('kw variant');
    expect(lastFrame()).toContain('Still produce this article?');
    stdin.write('y');
    await tick();
    expect(lastFrame()).not.toContain('Still produce this article?');
    unmount();
  });

  it('stops the run from the GSC gate strip on n', async () => {
    const {lastFrame, stdin, unmount} = render(
      <App
        noBridge
        initialState={{step: 'running', runId: 'run_x', gscGate: {score: 5, threshold: 25}}}
        config={{keyword: 'lead routing'}}
      />,
    );
    stdin.write('n');
    await tick();
    expect(lastFrame()).not.toContain('Still produce this article?');
    expect(lastFrame()).toContain('Stopping run at GSC score gate');
    unmount();
  });

  it('toggles the Ctrl+O full-content view on preview', async () => {
    const longBody = Array.from({length: 120}, (_, index) => `Line ${index + 1} of the article body.`).join('\n');
    const artifact = {id: 'final-mdx', data: {artifact: 'final_mdx', content: `---\ntitle: Long\n---\n${longBody}`, metadata: {title: 'Long', slug: 'long-article'}}};
    const {lastFrame, stdin, unmount} = render(
      <App noBridge size={{columns: 80, rows: 24}} initialState={{step: 'preview', artifacts: [artifact]}} config={{keyword: 'lead routing'}} />,
    );
    expect(lastFrame()).toContain('Slug');
    stdin.write('\x0f'); // Ctrl+O -> full article
    await tick();
    expect(lastFrame()).not.toContain('Slug');
    expect(lastFrame()).toContain('lines 1–16 of 120');
    stdin.write('\x0f'); // Ctrl+O toggles back too
    await tick();
    expect(lastFrame()).toContain('Slug');
    unmount();
  });

  it('expands the pre-polish draft to full content with Ctrl+O on review', async () => {
    const draftBody = Array.from({length: 80}, (_, index) => `Draft line ${index + 1}.`).join('\n');
    const {lastFrame, stdin, unmount} = render(
      <App
        noBridge
        size={{columns: 96, rows: 24}}
        initialState={{
          step: 'review',
          commandMode: 'review',
          artifacts: [{id: 'pre-polish', data: {artifact: 'pre_polish_mdx', content: draftBody}}],
        }}
        config={{keyword: 'lead routing'}}
      />,
    );
    expect(lastFrame()).toContain('approve — continue to final polish');
    stdin.write('\x0f'); // Ctrl+O -> full draft replaces the gate body
    await tick();
    expect(lastFrame()).not.toContain('approve — continue to final polish');
    expect(lastFrame()).toContain('lines 1–16 of 80');
    stdin.write('\x1b[B'); // scroll down one line
    await tick();
    expect(lastFrame()).toContain('lines 2–17 of 80');
    stdin.write(''); // Esc collapses back to the gate menu
    await tick();
    expect(lastFrame()).toContain('approve — continue to final polish');
    unmount();
  });

  it('locks competitor mentions to forced on for comparison styles', async () => {
    const forced = render(<App noBridge size={{columns: 100, rows: 30}} config={{keyword: 'crm tools', style: 'comparison'}} />);
    expect(forced.lastFrame()).toContain('Competitor mentions');
    expect(forced.lastFrame()).toContain('forced on');
    // Navigate to the row (index 7 in FIELD_ROWS) and try to edit it: no editor opens.
    for (let presses = 0; presses < 7; presses += 1) {
      forced.stdin.write('\x1b[B');
      await tick();
    }
    forced.stdin.write('\r');
    await tick();
    expect(forced.lastFrame()).not.toContain('Edit Competitor mentions');
    expect(forced.lastFrame()).toContain('forced on');
    forced.unmount();

    const editable = render(<App noBridge size={{columns: 100, rows: 30}} config={{keyword: 'crm tools', style: 'guide'}} />);
    expect(editable.lastFrame()).toMatch(/Competitor mentions\s+on/);
    for (let presses = 0; presses < 7; presses += 1) {
      editable.stdin.write('\x1b[B');
      await tick();
    }
    editable.stdin.write('\r');
    await tick();
    expect(editable.lastFrame()).toContain('Edit Competitor mentions');
    editable.unmount();
  });

  it('renders the output step with outcome, paths, and actions', () => {
    const {lastFrame, unmount} = render(
      <App
        noBridge
        initialState={{
          step: 'output',
          outcome: 'complete',
          artifacts: [finalArtifact],
          outputPaths: {final: '/tmp/final.mdx', raw: '/tmp/raw.mdx'},
          setupStatus: {
            checks: [{
              id: 'site_target',
              label: 'Website target',
              required: false,
              ok: true,
              detail: '/tmp/acme-site/content/blog',
            }],
            canGenerate: true,
            requiredMissing: [],
            optionalMissing: [],
          },
        }}
        config={{keyword: 'lead routing'}}
      />,
    );
    expect(lastFrame()).toContain('Generation complete');
    expect(lastFrame()).toContain('/tmp/final.mdx');
    expect(lastFrame()).toContain('/tmp/raw.mdx');
    expect(lastFrame()).toContain('/tmp/acme-site/content/blog');
    expect(lastFrame()).toContain('Publish directly to connected site…');
    expect(lastFrame()).toContain('Start a new article');
    unmount();
  });

  it('confirms publishing with y/n on the output step', async () => {
    process.env.FLEO_BLOG_CONTENT_PATH = '/tmp/site-blog';
    const {lastFrame, stdin, unmount} = render(
      <App
        noBridge
        initialState={{step: 'output', outcome: 'complete', artifacts: [finalArtifact], publishArmed: true}}
        config={{keyword: 'lead routing'}}
      />,
    );
    expect(lastFrame()).toContain('Publish to /tmp/site-blog?');
    stdin.write('n');
    await tick();
    expect(lastFrame()).not.toContain('Publish to /tmp/site-blog?');
    stdin.write('P');
    await tick();
    expect(lastFrame()).toContain('Publish to /tmp/site-blog?');
    stdin.write('y');
    await tick();
    expect(lastFrame()).toContain('Publishing to connected site…');
    delete process.env.FLEO_BLOG_CONTENT_PATH;
    unmount();
  });

  it('renders notifications above the step region', () => {
    const {lastFrame, unmount} = render(
      <App
        noBridge
        initialState={{
          step: 'running',
          notifications: [{id: 'n-1', tone: 'warning', message: 'Citation fallback used'}],
          noticeSeq: 1,
        }}
      />,
    );
    expect(lastFrame()).toContain('Citation fallback used');
    expect(lastFrame()).toContain('[!]');
    unmount();
  });

  it('walks the guided config flow through the context step and starts a run', async () => {
    const {lastFrame, stdin, unmount} = render(<App noBridge />);
    for (const char of 'pipeline attribution') {
      stdin.write(char);
      await tick();
    }
    stdin.write('\r'); // submit keyword -> style stage
    await tick();
    expect(lastFrame()).toContain('Pick a content style');
    stdin.write('\r'); // pick default style -> depth stage
    await tick();
    expect(lastFrame()).toContain('research go');
    stdin.write('\r'); // pick default depth -> settings stage
    await tick();
    stdin.write('s'); // start shortcut -> context step (auto-interpreting)
    await tick();
    expect(lastFrame()).toContain('Interpreting topic');
    stdin.write('\r'); // Enter starts the run without waiting for the interpretation
    await tick();
    const frame = lastFrame() ?? '';
    expect(frame).toContain('Generating');
    expect(frame).toContain('pipeline attribution');
    unmount();
  });

  it('honors backspace while typing a keyword', async () => {
    const {lastFrame, stdin, unmount} = render(<App noBridge />);
    for (const char of 'flow') {
      stdin.write(char);
      await tick();
    }
    stdin.write(''); // delete the trailing 'w'
    await tick();
    stdin.write('\r'); // submit -> 'flo'
    await tick();
    expect(lastFrame()).toContain('flo');
    expect(lastFrame()).not.toContain('flow');
    unmount();
  });

  it('warns instead of freezing when saving without a final artifact', async () => {
    const rendered = render(<App noBridge initialState={{step: 'preview'}} config={{keyword: 'lead routing'}} />);
    rendered.stdin.write('s');
    await tick();
    // No final artifact ⇒ save/publish bindings are inactive; nothing crashes and
    // the preview stays put with its empty-state copy.
    expect(rendered.lastFrame()).toContain('Final article body appears here');
    rendered.unmount();
  });

  it('asks for confirmation before cancelling a run on Esc', async () => {
    const rendered = render(<App noBridge initialState={{step: 'running', runId: 'run_x'}} config={{keyword: 'lead routing'}} />);
    rendered.stdin.write(''); // Esc
    await tick();
    expect(rendered.lastFrame()).toContain('Cancel this run?');
    rendered.stdin.write('n');
    await tick();
    expect(rendered.lastFrame()).not.toContain('Cancel this run?');
    rendered.unmount();
  });

  it('starts a fresh run from the output step', async () => {
    const {lastFrame, stdin, unmount} = render(
      <App
        noBridge
        initialState={{step: 'output', outcome: 'complete', artifacts: [finalArtifact], outputPaths: {final: '/tmp/final.mdx'}}}
        config={{keyword: 'lead routing'}}
      />,
    );
    stdin.write('n');
    await tick();
    expect(lastFrame()).toContain('Generation Config');
    expect(lastFrame()).toContain('lead routing');
    unmount();
  });
});
