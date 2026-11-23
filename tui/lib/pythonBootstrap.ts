import {spawn} from 'node:child_process';
import {existsSync, mkdirSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {installationRoot, preferredPython} from '../bridge.js';

export interface PythonBootstrapResult {
  ok: boolean;
  pythonPath: string;
  bootstrapped: boolean;
  detail: string;
}

const runCmd = (
  cmd: string,
  args: string[],
  onStatus?: (msg: string) => void,
  signal?: AbortSignal,
): Promise<{code: number | null; stderr: string}> =>
  new Promise(resolve => {
    if (signal?.aborted) {
      resolve({code: 130, stderr: 'Bootstrap cancelled'});
      return;
    }
    const child = spawn(cmd, args, {
      cwd: installationRoot,
      env: {...process.env, PYTHONSAFEPATH: '1', UV_NO_CONFIG: '1'},
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stderr = '';
    let settled = false;
    const onError = (err: Error) => finish({code: 127, stderr: err.message}, false);
    const onStdoutData = (chunk: Buffer) => {
      const line = chunk.toString('utf8').trim().split(/\r?\n/).pop();
      if (line && onStatus) onStatus(line.slice(0, 72));
    };
    const onStderrData = (chunk: Buffer) => {
      stderr += chunk.toString('utf8');
    };
    const cleanupListeners = (closed: boolean) => {
      signal?.removeEventListener('abort', onAbort);
      child.stdout.removeListener('data', onStdoutData);
      child.stderr.removeListener('data', onStderrData);
      if (closed) {
        child.removeAllListeners('close');
        child.removeListener('error', onError);
      }
    };
    const finish = (res: {code: number | null; stderr: string}, closed = false) => {
      if (settled) {
        if (closed) cleanupListeners(true);
        return;
      }
      settled = true;
      cleanupListeners(closed);
      resolve(res);
    };
    const onAbort = () => {
      child.kill('SIGTERM');
      finish({code: 130, stderr: 'Bootstrap cancelled'}, false);
    };
    signal?.addEventListener('abort', onAbort, {once: true});
    child.stdout.on('data', onStdoutData);
    child.stderr.on('data', onStderrData);
    child.on('error', onError);
    child.on('close', code => finish({code, stderr}, true));
  });

export const managedVenvDir = (): string => path.join(os.homedir(), '.fleo', 'venv');

export const managedVenvPython = (): string =>
  process.platform === 'win32'
    ? path.join(managedVenvDir(), 'Scripts', 'python.exe')
    : path.join(managedVenvDir(), 'bin', 'python');

export const ensurePythonRuntime = async (
  onProgress?: (message: string) => void,
  signal?: AbortSignal,
): Promise<PythonBootstrapResult> => {
  const abortedResult = (): PythonBootstrapResult => ({
    ok: false,
    pythonPath: preferredPython(),
    bootstrapped: false,
    detail: 'Python bootstrap cancelled',
  });
  if (signal?.aborted) return abortedResult();

  const currentPy = preferredPython();
  onProgress?.('Checking Python engine dependencies…');
  const probe = await runCmd(
    currentPy,
    ['-c', 'import openai, aiohttp, pydantic, bs4, yaml, defusedxml, rich, click, questionary'],
    undefined,
    signal,
  );
  if (signal?.aborted) return abortedResult();
  if (probe.code === 0) {
    return {ok: true, pythonPath: currentPy, bootstrapped: false, detail: currentPy};
  }

  const reqPath = path.join(installationRoot, 'requirements.txt');
  const venvDir = managedVenvDir();
  const venvPy = managedVenvPython();

  mkdirSync(path.dirname(venvDir), {recursive: true});

  // Fast path: `uv` provisions isolated Python + packages in seconds
  const uvProbe = await runCmd('uv', ['--version'], undefined, signal);
  if (signal?.aborted) return abortedResult();
  if (uvProbe.code === 0) {
    if (!existsSync(venvPy)) {
      onProgress?.('Creating isolated Python 3.12 runtime via uv…');
      const mk = await runCmd('uv', ['--no-config', 'venv', venvDir, '--python', '3.12'], onProgress, signal);
      if (signal?.aborted) return abortedResult();
      if (mk.code !== 0) {
        await runCmd('uv', ['--no-config', 'venv', venvDir], onProgress, signal);
        if (signal?.aborted) return abortedResult();
      }
    }
    if (existsSync(venvPy)) {
      onProgress?.('Installing Fleo engine packages via uv…');
      const inst = await runCmd('uv', ['--no-config', 'pip', 'install', '--python', venvPy, '-r', reqPath], onProgress, signal);
      if (signal?.aborted) return abortedResult();
      if (inst.code === 0) {
        process.env.FLEO_PYTHON = venvPy;
        return {ok: true, pythonPath: venvPy, bootstrapped: true, detail: `${venvPy} (uv)`};
      }
    }
  }

  // Standard fallback: system python3 -m venv + pip install
  const sysPython = process.platform === 'win32' ? 'python' : 'python3';
  let needsVenvCreate = !existsSync(venvPy);
  if (!needsVenvCreate) {
    const pipProbe = await runCmd(venvPy, ['-m', 'pip', '--version'], undefined, signal);
    if (signal?.aborted) return abortedResult();
    if (pipProbe.code !== 0) {
      needsVenvCreate = true;
    }
  }
  if (needsVenvCreate) {
    onProgress?.('Creating Python virtual environment in ~/.fleo/venv…');
    const venvArgs = existsSync(venvPy) ? ['-m', 'venv', '--clear', venvDir] : ['-m', 'venv', venvDir];
    const mk = await runCmd(sysPython, venvArgs, onProgress, signal);
    if (signal?.aborted) return abortedResult();
    if (mk.code !== 0) {
      return {
        ok: false,
        pythonPath: sysPython,
        bootstrapped: false,
        detail: `Python 3.10+ required (${mk.stderr.trim().slice(0, 120) || 'install python3-venv or uv'})`,
      };
    }
  }

  onProgress?.('Installing engine packages (one-time setup, ~20s)…');
  const pipInstall = await runCmd(venvPy, ['-m', 'pip', 'install', '-q', '-r', reqPath], onProgress, signal);
  if (signal?.aborted) return abortedResult();
  if (pipInstall.code !== 0) {
    return {
      ok: false,
      pythonPath: venvPy,
      bootstrapped: false,
      detail: `pip install failed: ${pipInstall.stderr.trim().slice(0, 140)}`,
    };
  }

  process.env.FLEO_PYTHON = venvPy;
  return {ok: true, pythonPath: venvPy, bootstrapped: true, detail: venvPy};
};
