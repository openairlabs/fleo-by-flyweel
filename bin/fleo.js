#!/usr/bin/env node
import {spawnSync} from 'node:child_process';
import {existsSync, readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const argv = process.argv.slice(2);

if (argv.length === 1 && (argv[0] === '--version' || argv[0] === '-V')) {
  const pkg = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'));
  console.log(pkg.version);
  process.exit(0);
}

const major = Number(process.versions.node.split('.')[0] ?? 0);
if (major < 22) {
  console.error(`fleo requires Node.js >= 22 (detected ${process.version}). Upgrade Node and re-run.`);
  process.exit(1);
}

const distEntry = path.resolve(root, 'dist/tui/cli.js');
const tsxBin = path.resolve(root, 'node_modules/.bin/tsx');
const srcEntry = path.resolve(root, 'tui/cli.tsx');

let cmd = process.execPath;
let cmdArgs;
if (existsSync(distEntry)) {
  cmdArgs = [distEntry, ...argv];
} else if (existsSync(tsxBin) && existsSync(srcEntry)) {
  cmd = tsxBin;
  cmdArgs = [srcEntry, ...argv];
} else {
  console.error('fleo is not built. Run `pnpm build` (or `npm run build`) from the installation root.');
  process.exit(1);
}

const result = spawnSync(cmd, cmdArgs, {
  stdio: 'inherit',
  env: {
    ...process.env,
    FLEO_PACKAGE_ROOT: root,
    FLEO_WORKDIR: process.env.FLEO_WORKDIR || process.cwd(),
  },
});

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}

if (result.signal) {
  process.kill(process.pid, result.signal);
  process.exit(1);
}

process.exit(result.status ?? 1);
