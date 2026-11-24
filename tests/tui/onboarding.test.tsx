import {chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import React from 'react';
import {Box} from 'ink';
import {render} from 'ink-testing-library';
import stringWidth from 'string-width';
import {describe, expect, it, vi} from 'vitest';

import {SetupView} from '../../tui/components/setupView.js';
import * as setupActions from '../../tui/setupActions.js';
import {installBundledSkills} from '../../tui/cli.js';
import {ThemeProvider} from '../../tui/components/ui/theme-provider.js';
import {ensurePythonRuntime} from '../../tui/lib/pythonBootstrap.js';
import {LayoutContext} from '../../tui/lib/useTerminalSize.js';
import {createInitialState} from '../../tui/state.js';

const tick = (ms = 25) => new Promise(resolve => setTimeout(resolve, ms));

describe('First-Time TUI Setup Wizard', () => {
  it('opens directly on Step 2 API Keys when runtime is ready and keys are missing, and Tab switches to Per-Agent Keys', async () => {
    const state = createInitialState({
      step: 'setup',
      setupStatus: {
        checks: [
          {id: 'venv', label: 'Python engine runtime', required: true, ok: true, detail: '/usr/bin/python3'},
          {id: 'ai_credentials', label: 'AI model credentials', required: true, ok: false, detail: 'OPENROUTER_API_KEY'},
        ],
        canGenerate: false,
        requiredMissing: ['ai_credentials'],
        optionalMissing: [],
        onboardingComplete: false,
        brandConfigured: false,
      },
    });

    const {lastFrame, stdin, unmount} = render(
      <ThemeProvider>
        <LayoutContext.Provider value={{columns: 100, rows: 32, bodyRows: 24}}>
          <SetupView state={state} dispatch={() => {}} />
        </LayoutContext.Provider>
      </ThemeProvider>,
    );

    expect(lastFrame()).toContain('2 API Keys');
    expect(lastFrame()).toContain('Quickstart (1 Key)');
    expect(lastFrame()).toContain('OpenRouter API Key (Required)');

    // Press Tab -> switches to Separate Per-Agent Keys view!
    stdin.write('\t');
    await tick();
    expect(lastFrame()).toContain('▸ Separate Per-Agent Keys ◂');
    expect(lastFrame()).toContain('FLEO_RESEARCH_API_KEY');
    expect(lastFrame()).toContain('FLEO_DRAFT_API_KEY');
    unmount();
  });

  it('opens on Step 3 Brand Context when API keys are already set, with Auto-Scan URL and 5-Question Interview', () => {
    const state = createInitialState({
      step: 'setup',
      setupStatus: {
        checks: [
          {id: 'venv', label: 'Python engine runtime', required: true, ok: true, detail: '/usr/bin/python3'},
          {id: 'ai_credentials', label: 'AI model credentials', required: true, ok: true, detail: 'OPENROUTER_API_KEY'},
        ],
        canGenerate: true,
        requiredMissing: [],
        optionalMissing: [],
        onboardingComplete: false,
        brandConfigured: false,
      },
    });

    const {lastFrame, unmount} = render(
      <ThemeProvider>
        <LayoutContext.Provider value={{columns: 100, rows: 32, bodyRows: 24}}>
          <SetupView state={state} dispatch={() => {}} />
        </LayoutContext.Provider>
      </ThemeProvider>,
    );

    expect(lastFrame()).toContain('3 Your Brand');
    expect(lastFrame()).toContain('Auto-Scan My Website URL');
    expect(lastFrame()).toContain('5-Question Quick Interview');
    unmount();
  });

  it('lets users pick per-step models with Default · Recommended badges and reset cleanly', async () => {
    const state = createInitialState({
      step: 'setup',
      setupStatus: {
        checks: [
          {id: 'venv', label: 'Python engine runtime', required: true, ok: true, detail: '/usr/bin/python3'},
          {id: 'ai_credentials', label: 'AI model credentials', required: true, ok: false, detail: 'OPENROUTER_API_KEY'},
        ],
        canGenerate: false,
        requiredMissing: ['ai_credentials'],
        optionalMissing: [],
        onboardingComplete: false,
        brandConfigured: false,
      },
    });

    const {lastFrame, stdin, unmount} = render(
      <ThemeProvider>
        <LayoutContext.Provider value={{columns: 108, rows: 32, bodyRows: 22}}>
          <SetupView state={state} dispatch={() => {}} />
        </LayoutContext.Provider>
      </ThemeProvider>,
    );

    expect(lastFrame()).toContain('Choose Models per Step');
    expect(lastFrame()).toContain('Recommended Defaults');

    // Move down 3 rows to "Choose Models per Step" and press Enter
    for (let i = 0; i < 3; i++) {
      stdin.write('\u001b[B');
      await tick();
    }
    stdin.write('\r');
    await tick();

    const listFrame = lastFrame() ?? '';
    expect(listFrame).toContain('Choose AI Model for Each Pipeline Step');
    expect(listFrame).toContain('1 · Web & Community Research');
    expect(listFrame).toContain('3 · Humanizer & Polish');
    expect(listFrame).toContain('[Default · Recommended]');

    // Move down 2 rows to "3 · Humanizer & Polish" and press Enter
    stdin.write('\u001b[B');
    await tick();
    stdin.write('\u001b[B');
    await tick();
    stdin.write('\r');
    await tick();

    const polishFrame = lastFrame() ?? '';
    expect(polishFrame).toContain('Step 3 · Humanizer & Polish');
    expect(polishFrame).toContain('Anthropic Claude Sonnet 5.5 [Default · Recommended]');
    expect(polishFrame).toContain('OpenAI GPT-6 Sol [OpenAI / BYOK]');

    // Move down 1 row to OpenAI GPT-6 Sol and press Enter
    stdin.write('\u001b[B');
    await tick();
    stdin.write('\r');
    await tick();

    const updatedListFrame = lastFrame() ?? '';
    expect(updatedListFrame).toContain('OpenAI GPT-6 Sol [Custom]');

    // Press Esc to return to Quickstart -> shows 1 custom model(s)
    stdin.write('\u001b');
    await tick();
    expect(lastFrame()).toContain('1 custom model(s)');
    unmount();
  });

  it('renders SetupView & Model Picker responsively within a compact 64x18 viewport without overflow', async () => {
    const state = createInitialState({
      step: 'setup',
      setupStatus: {
        checks: [
          {id: 'venv', label: 'Python engine runtime', required: true, ok: true, detail: '/usr/bin/python3'},
          {id: 'ai_credentials', label: 'AI model credentials', required: true, ok: false, detail: 'OPENROUTER_API_KEY'},
        ],
        canGenerate: false,
        requiredMissing: ['ai_credentials'],
        optionalMissing: [],
        onboardingComplete: false,
        brandConfigured: false,
      },
    });

    const {lastFrame, stdin, unmount} = render(
      <ThemeProvider>
        <LayoutContext.Provider value={{columns: 64, rows: 18, bodyRows: 11}}>
          <Box width={64} overflowX="hidden">
            <SetupView state={state} dispatch={() => {}} />
          </Box>
        </LayoutContext.Provider>
      </ThemeProvider>,
    );

    const checkBounds = () => {
      const lines = (lastFrame() ?? '').split('\n');
      expect(lines.length).toBeLessThanOrEqual(11);
      expect(Math.max(...lines.map(l => stringWidth(l)))).toBeLessThanOrEqual(64);
    };

    expect(lastFrame()).toContain('Step 2/5');
    checkBounds();

    // Open Model Picker in compact viewport
    for (let i = 0; i < 3; i++) {
      stdin.write('\u001b[B');
      await tick();
    }
    stdin.write('\r');
    await tick();
    expect(lastFrame()).toContain('Choose AI Model');
    checkBounds();
    unmount();
  });

  it('spawns Python bootstrap probes inside installationRoot rather than an untrusted cwd', async () => {
    const origCwd = process.cwd();
    const origFleoPy = process.env.FLEO_PYTHON;
    const origPoison = process.env.POISON_CWD;
    const tmpDir = mkdtempSync(path.join(os.tmpdir(), 'fleo-poison-cwd-'));
    const binDir = mkdtempSync(path.join(os.tmpdir(), 'fleo-fake-py-'));
    const markerFile = path.join(tmpDir, 'executed.txt');
    writeFileSync(
      path.join(tmpDir, 'json.py'),
      `import pathlib, sys\npathlib.Path(${JSON.stringify(markerFile)}).write_text("pwned")\nsys.exit(42)\n`,
      'utf8',
    );
    const fakePy = path.join(binDir, 'fake-python.sh');
    writeFileSync(
      fakePy,
      '#!/bin/sh\nexec python3 -c \'import os, json; assert os.getcwd() != os.environ["POISON_CWD"]; assert os.environ.get("PYTHONSAFEPATH") == "1"; assert os.environ.get("UV_NO_CONFIG") == "1"\'\n',
      'utf8',
    );
    chmodSync(fakePy, 0o755);
    try {
      process.env.FLEO_PYTHON = fakePy;
      process.env.POISON_CWD = tmpDir;
      process.chdir(tmpDir);
      const res = await ensurePythonRuntime();
      expect(res.ok).toBe(true);
      expect(() => rmSync(markerFile)).toThrow();
    } finally {
      process.chdir(origCwd);
      if (origFleoPy === undefined) delete process.env.FLEO_PYTHON;
      else process.env.FLEO_PYTHON = origFleoPy;
      if (origPoison === undefined) delete process.env.POISON_CWD;
      else process.env.POISON_CWD = origPoison;
      rmSync(tmpDir, {recursive: true, force: true});
      rmSync(binDir, {recursive: true, force: true});
    }
  });

  it('install-skill skips existing files without --force, rejects unknown agents, and refuses symlinks', () => {
    const tmpHome = mkdtempSync(path.join(os.tmpdir(), 'fleo-skill-home-'));
    try {
      expect(() => installBundledSkills('unknown-agent', {homeDir: tmpHome})).toThrow(/Unsupported --agent/);

      const claudeDest = path.join(tmpHome, '.claude', 'skills', 'fleo-setup-brand', 'SKILL.md');
      mkdirSync(path.dirname(claudeDest), {recursive: true});
      writeFileSync(claudeDest, 'CUSTOM_EXISTING_CONTENT', 'utf8');

      installBundledSkills('claude-code', {homeDir: tmpHome, force: false});
      expect(readFileSync(claudeDest, 'utf8')).toBe('CUSTOM_EXISTING_CONTENT');

      installBundledSkills('claude-code', {homeDir: tmpHome, force: true});
      expect(readFileSync(claudeDest, 'utf8')).toContain('fleo-setup-brand');

      // Symlinked target file must be refused even with force: true
      const victim = path.join(tmpHome, 'victim.md');
      writeFileSync(victim, 'DO_NOT_TOUCH', 'utf8');
      rmSync(claudeDest);
      symlinkSync(victim, claudeDest);
      expect(() => installBundledSkills('claude-code', {homeDir: tmpHome, force: true})).toThrow(/symlink/);
      expect(readFileSync(victim, 'utf8')).toBe('DO_NOT_TOUCH');
    } finally {
      rmSync(tmpHome, {recursive: true, force: true});
    }
  });

  it('marks direct local repo publishing as Experimental in Step 4 and shows fallback guidance on select', async () => {
    const state = createInitialState({
      step: 'setup',
      setupStatus: {
        checks: [
          {id: 'venv', label: 'Python engine runtime', required: true, ok: true, detail: '/usr/bin/python3'},
          {id: 'ai_credentials', label: 'AI model credentials', required: true, ok: true, detail: 'OPENROUTER_API_KEY'},
        ],
        canGenerate: true,
        requiredMissing: [],
        optionalMissing: [],
        onboardingComplete: true,
        brandConfigured: true,
        brandName: 'Acme',
      },
    });

    const {lastFrame, stdin, unmount} = render(
      <ThemeProvider>
        <LayoutContext.Provider value={{columns: 110, rows: 34, bodyRows: 26}}>
          <SetupView state={state} dispatch={() => {}} />
        </LayoutContext.Provider>
      </ThemeProvider>,
    );

    // Hub shows Step 4 with Experimental label
    expect(lastFrame()).toContain('Direct Repo Publish (Experimental)');
    // Move down 4 items in Setup Hub to option 4 (index 4: target) and press Enter
    stdin.write('\x1b[B');
    await tick();
    stdin.write('\x1b[B');
    await tick();
    stdin.write('\x1b[B');
    await tick();
    stdin.write('\x1b[B');
    await tick();
    stdin.write('\r');
    await tick();

    expect(lastFrame()).toContain('Agent Handoff Packet (agent-handoff.md)');
    expect(lastFrame()).toContain('Connect Local Website Git Repo [Experimental]');

    // Select the Experimental repo option (index 1) -> shows warning + agent-handoff.md fallback guidance
    stdin.write('\x1b[B');
    await tick();
    stdin.write('\r');
    await tick();

    const frame = lastFrame() ?? '';
    expect(frame).toContain('Experimental: Direct repo publish');
    expect(frame).toContain('agent-handoff.md');
    expect(frame).toContain('README.md');

    // Submitting repo path advances to optional custom content folder path input
    stdin.write('\r');
    await tick();
    const contentDirFrame = lastFrame() ?? '';
    expect(contentDirFrame).toContain('target folder path for published articles');
    expect(contentDirFrame).toContain('leave blank to auto-detect');

    // Pressing Esc returns cleanly to the repo path prompt
    stdin.write('\x1b');
    await tick();
    expect(lastFrame() ?? '').toContain('Experimental: Direct repo publish');
    unmount();
  });

  it('displays status.siteTarget.contentDir and bridge override metadata rather than process.env in Step 5', async () => {
    const prevAstro = process.env.ASTRO_BLOG_PATH;
    const prevFleo = process.env.FLEO_BLOG_CONTENT_PATH;
    process.env.ASTRO_BLOG_PATH = '/publish/legacy';
    delete process.env.FLEO_BLOG_CONTENT_PATH;
    const spy = vi.spyOn(setupActions, 'runDetectSiteTargetAction').mockResolvedValue({events: []});

    try {
      const state = createInitialState({
        step: 'setup',
        setupStatus: {
          checks: [
            {id: 'venv', label: 'Python engine runtime', required: true, ok: true, detail: '/usr/bin/python3'},
            {id: 'ai_credentials', label: 'AI model credentials', required: true, ok: true, detail: 'OPENROUTER_API_KEY'},
          ],
          canGenerate: true,
          requiredMissing: [],
          optionalMissing: [],
          onboardingComplete: true,
          brandConfigured: true,
          brandName: 'Acme',
          siteTarget: {
            mode: 'handoff',
            framework: 'nextjs',
            frameworkLabel: 'Next.js',
            contentDir: '/publish/fleo',
            contentDirOverrideEnv: 'FLEO_BLOG_CONTENT_PATH',
            existingPostCount: 0,
          },
        },
      });

      const {lastFrame, stdin, unmount} = render(
        <ThemeProvider>
          <LayoutContext.Provider value={{columns: 120, rows: 34, bodyRows: 26}}>
            <SetupView state={state} dispatch={() => {}} />
          </LayoutContext.Provider>
        </ThemeProvider>,
      );

      // Hub -> option 4 (target) -> option 0 (Agent Handoff) -> option 0 (Next.js) -> Step 5 ready
      for (let i = 0; i < 4; i++) {
        stdin.write('\x1b[B');
        await tick();
      }
      stdin.write('\r');
      await tick();
      stdin.write('\r');
      await tick();
      stdin.write('\r');
      await tick(60);

      const frame = lastFrame() ?? '';
      expect(frame).toContain('/publish/fleo (FLEO_BLOG_CONTENT_PATH override)');
      expect(frame).not.toContain('/publish/legacy');
      unmount();
    } finally {
      spy.mockRestore();
      if (prevAstro === undefined) delete process.env.ASTRO_BLOG_PATH;
      else process.env.ASTRO_BLOG_PATH = prevAstro;
      if (prevFleo === undefined) delete process.env.FLEO_BLOG_CONTENT_PATH;
      else process.env.FLEO_BLOG_CONTENT_PATH = prevFleo;
    }
  });
});
