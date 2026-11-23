import {execFileSync, spawn} from 'node:child_process';
import {existsSync, lstatSync, mkdirSync, readFileSync, renameSync, writeFileSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {Command, Option} from 'commander';
import {render} from 'ink';
import pc from 'picocolors';
import React from 'react';

import {App} from './App.js';
import {enterAltScreen} from './lib/altScreen.js';
import {ensurePythonRuntime} from './lib/pythonBootstrap.js';
import {defaultConfig} from './state.js';
import {bridgePath, installationRoot, preferredPython} from './bridge.js';
import {themes} from './lib/terminal-themes/index.js';
import type {GenerationConfig} from './types.js';

const isInteractive = (): boolean => Boolean(process.stdin.isTTY && process.stdout.isTTY && process.env.CI !== 'true');

const runFullScreen = async (element: React.ReactElement): Promise<void> => {
  const leaveAltScreen = process.stdout.isTTY ? enterAltScreen(process.stdout) : undefined;
  try {
    const app = render(element, {exitOnCtrlC: false, incrementalRendering: true});
    await app.waitUntilExit();
  } finally {
    leaveAltScreen?.();
  }
};

const readSetupStatus = (): string => {
  try {
    return execFileSync(preferredPython(), [bridgePath(), 'setup-status'], {
      cwd: path.dirname(bridgePath()),
      env: {...process.env, FLEO_WORKDIR: process.env.FLEO_WORKDIR || process.cwd(), PYTHONIOENCODING: 'utf-8'},
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (error) {
    const output = error && typeof error === 'object' && 'stdout' in error ? String((error as {stdout?: unknown}).stdout ?? '') : '';
    return output || '';
  }
};

const printPlainStatus = (mode: string, config?: Partial<GenerationConfig>): void => {
  const setup = readSetupStatus()
    .split(/\r?\n/)
    .filter(Boolean)
    .map(line => {
      try {
        return JSON.parse(line) as {data?: {checks?: Array<{label: string; ok: boolean; required: boolean}>; canGenerate?: boolean}};
      } catch {
        return undefined;
      }
    })
    .find(Boolean);
  console.log(pc.bold('fleo (by Flyweel)'));
  console.log(`mode: ${mode}`);
  if (config?.keyword) console.log(`keyword: ${config.keyword}`);
  if (setup?.data?.checks) {
    for (const check of setup.data.checks) {
      console.log(`${check.ok ? '[OK]' : check.required ? '[MISS]' : '[WARN]'} ${check.label}`);
    }
  }
};

const hasSymlinkUnder = (baseDir: string, targetPath: string): boolean => {
  const rel = path.relative(baseDir, targetPath);
  if (rel.startsWith('..') || path.isAbsolute(rel)) return true;
  let cur = baseDir;
  for (const part of rel.split(path.sep).filter(Boolean)) {
    cur = path.join(cur, part);
    try {
      if (lstatSync(cur).isSymbolicLink()) return true;
    } catch {
      // Path component doesn't exist yet — safe to create
    }
  }
  return false;
};

export const installBundledSkills = (agentFilter = 'all', options: {force?: boolean; homeDir?: string} = {}): void => {
  const home = options.homeDir ?? os.homedir();
  const force = Boolean(options.force);
  const skillsDir = path.join(installationRoot, 'skills');
  const bundled = ['fleo-setup-brand', 'fleo-publish-post'];
  const xdgConfig = options.homeDir
    ? path.join(home, '.config')
    : process.env.XDG_CONFIG_HOME || path.join(home, '.config');
  const allTargets = [
    {id: 'claude-code', name: 'Claude Code', dir: path.join(home, '.claude', 'skills'), mode: 'folder' as const},
    {id: 'opencode', name: 'OpenCode', dir: path.join(xdgConfig, 'opencode', 'skills'), mode: 'folder' as const},
    {id: 'cursor', name: 'Cursor', dir: path.join(home, '.cursor', 'rules'), mode: 'mdc' as const},
    {id: 'windsurf', name: 'Windsurf', dir: path.join(home, '.windsurf', 'rules'), mode: 'md' as const},
  ];
  const targets = allTargets.filter(t => agentFilter === 'all' || t.id === agentFilter);
  if (targets.length === 0) {
    throw new Error(`Unsupported --agent "${agentFilter}". Expected one of: claude-code, opencode, cursor, windsurf, all.`);
  }

  console.log(pc.bold('✻ Installing Fleo agent skills'));
  for (const skill of bundled) {
    const srcFile = path.join(skillsDir, skill, 'SKILL.md');
    if (!existsSync(srcFile)) continue;
    const content = readFileSync(srcFile, 'utf8');
    for (const target of targets) {
      const destFile =
        target.mode === 'folder'
          ? path.join(target.dir, skill, 'SKILL.md')
          : path.join(target.dir, `${skill}${target.mode === 'mdc' ? '.mdc' : '.md'}`);
      const rootAnchor = target.id === 'opencode' ? xdgConfig : home;
      if (hasSymlinkUnder(rootAnchor, destFile)) {
        throw new Error(`Refusing to write skill through symlink: ${destFile}`);
      }
      if (existsSync(destFile) && !force) {
        console.log(`  ${pc.yellow('~')} ${target.name}: skipped existing ${destFile} (pass --force to overwrite)`);
        continue;
      }
      const destParent = path.dirname(destFile);
      mkdirSync(destParent, {recursive: true});
      const tmpFile = path.join(destParent, `.${path.basename(destFile)}.${process.pid}.tmp`);
      writeFileSync(tmpFile, content, 'utf8');
      renameSync(tmpFile, destFile);
      console.log(`  ${pc.green('+')} ${target.name}: ${destFile}`);
    }
  }
};

export const buildProgram = () => {
  const program = new Command();
  program
    .name('fleo')
    .description('Fleo by Flyweel — Company-agnostic AI SEO + AEO content engine & TUI')
    .option('--no-tui', 'Print non-interactive status instead of opening Ink')
    .addOption(new Option('--theme <theme>', 'Theme name').choices(Object.keys(themes)).default('theme-flyweel'));

  program
    .command('setup')
    .description('Open the interactive setup & brand onboarding wizard')
    .option('--no-tui', 'Print setup status instead of opening Ink')
    .action(async options => {
      const parent = program.opts<{theme: string; tui?: boolean}>();
      if (options.tui === false || parent.tui === false || !isInteractive()) {
        printPlainStatus('setup');
        return;
      }
      await runFullScreen(<App setupOnly themeName={parent.theme} />);
    });

  program
    .command('install-skill')
    .description('Install Fleo brand-setup and post-publishing skills into Claude Code, Cursor, OpenCode, or Windsurf')
    .addOption(
      new Option('--agent <agent>', 'Target agent').choices(['claude-code', 'opencode', 'cursor', 'windsurf', 'all']).default('all'),
    )
    .option('--force', 'Overwrite existing skill or rule files')
    .action(options => {
      installBundledSkills(options.agent, {force: Boolean(options.force)});
    });

  const runInheritedBridge = async (bridgeArgs: string[]): Promise<void> => {
    const runtime = await ensurePythonRuntime();
    if (!runtime.ok) {
      process.exitCode = 1;
      throw new Error(runtime.detail);
    }
    return new Promise((resolve, reject) => {
      const child = spawn(preferredPython(), bridgeArgs, {
        cwd: path.dirname(bridgePath()),
        env: {...process.env, FLEO_WORKDIR: process.env.FLEO_WORKDIR || process.cwd(), PYTHONIOENCODING: 'utf-8'},
        stdio: 'inherit',
      });
      const onError = (err: Error) => {
        child.removeListener('close', onClose);
        reject(err);
      };
      const onClose = (code: number | null, signal: NodeJS.Signals | null) => {
        child.removeListener('error', onError);
        if (code === 0) {
          resolve();
          return;
        }
        process.exitCode = code ?? 1;
        reject(new Error(signal ? `Bridge command terminated by ${signal}` : `Bridge command exited with status ${code ?? 1}`));
      };
      child.once('error', onError);
      child.once('close', onClose);
    });
  };

  program
    .command('scan-brand-url')
    .description('Scan a live website URL into a workspace BrandProfile draft')
    .requiredOption('--url <url>', 'Target website URL')
    .option('--run-id <id>', 'Optional run identifier')
    .action(async options => {
      const bridgeArgs = [bridgePath(), 'scan-brand-url', '--url', String(options.url)];
      if (options.runId) bridgeArgs.push('--run-id', String(options.runId));
      await runInheritedBridge(bridgeArgs);
    });

  program
    .command('detect-site-target')
    .description('Detect website framework and content contract for the current workspace')
    .option('--repo-path <path>', 'Repository path (defaults to current working directory)')
    .option('--run-id <id>', 'Optional run identifier')
    .action(async options => {
      const bridgeArgs = [bridgePath(), 'detect-site-target'];
      if (options.repoPath) bridgeArgs.push('--repo-path', String(options.repoPath));
      if (options.runId) bridgeArgs.push('--run-id', String(options.runId));
      await runInheritedBridge(bridgeArgs);
    });

  program
    .command('generate')
    .description('Open app with generation state prefilled')
    .requiredOption('-k, --keyword <keyword>')
    .addOption(new Option('--style <style>', 'Content style').choices(['standard', 'guide', 'comparison', 'research', 'news', 'category', 'top-compare', 'feature']).default(defaultConfig.style))
    .addOption(new Option('--depth <depth>', 'Content depth').choices(['quick', 'standard', 'comprehensive']).default(defaultConfig.depth))
    .addOption(new Option('--community-mode <mode>', 'Community mode').choices(['full', 'limited', 'skip']).default(defaultConfig.communityMode))
    .addOption(new Option('--brand-mode <mode>', 'Brand mention mode').choices(['full', 'limited', 'none']))
    .addOption(new Option('--flyweel-mode <mode>', 'Alias for --brand-mode').choices(['full', 'limited', 'none']).default(defaultConfig.flyweelMode))
    .option('--competitors <list>', 'Comma-separated competitor or solution names', defaultConfig.competitors)
    .option('--no-tui', 'Print status instead of opening Ink')
    .action(async options => {
      const parent = program.opts<{theme: string; tui?: boolean}>();
      const config: Partial<GenerationConfig> = {
        keyword: options.keyword,
        style: options.style,
        depth: options.depth,
        communityMode: options.communityMode,
        flyweelMode: options.brandMode ?? options.flyweelMode,
        competitors: options.competitors,
      };
      if (options.tui === false || parent.tui === false || !isInteractive()) {
        printPlainStatus('generate', config);
        return;
      }
      await runFullScreen(<App config={config} themeName={parent.theme} />);
    });

  program.action(async options => {
    if (options.tui === false || !isInteractive()) {
      printPlainStatus('config');
      return;
    }
    await runFullScreen(<App themeName={options.theme} />);
  });

  return program;
};

export const main = async (argv = process.argv): Promise<void> => {
  await buildProgram().parseAsync(argv);
};

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main().catch(error => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exit(1);
  });
}
