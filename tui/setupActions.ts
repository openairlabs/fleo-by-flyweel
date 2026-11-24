import {parseJsonlEvents, runBridgeOnce} from './bridge.js';
import type {AppState, BridgeEvent} from './types.js';

export const runSetupSaveAction = async (
  state: AppState,
  options: {signal?: AbortSignal} = {},
): Promise<{events: BridgeEvent[]; diagnostic?: string}> => {
  if (Object.keys(state.setupDraft).length === 0) {
    return {events: [], diagnostic: 'No setup changes are staged.'};
  }
  return runSaveSetupDirect(state.setupDraft, options);
};

export const runSaveSetupDirect = async (
  values: Record<string, string>,
  options: {signal?: AbortSignal} = {},
): Promise<{events: BridgeEvent[]; diagnostic?: string}> => {
  const result = await runBridgeOnce(
    ['tui_bridge.py', 'save-setup', '--run-id', `setup_${Date.now().toString(36)}`],
    JSON.stringify({values}),
    {signal: options.signal, timeoutMs: 15000},
  );
  const events = parseJsonlEvents(result.stdout);
  if (result.code !== 0) return {events, diagnostic: result.stderr || `Setup save exited ${result.code ?? 'without a code'}`};
  return {events};
};

export const runTestKeysAction = async (
  values: Record<string, string> = {},
): Promise<{events: BridgeEvent[]; diagnostic?: string}> => {
  const result = await runBridgeOnce(
    ['tui_bridge.py', 'test-keys', '--run-id', `testkeys_${Date.now().toString(36)}`],
    JSON.stringify({values}),
    {timeoutMs: 20000},
  );
  const events = parseJsonlEvents(result.stdout);
  if (result.code !== 0) return {events, diagnostic: result.stderr || 'API key test failed'};
  return {events};
};

export const runScanBrandUrlAction = async (
  url: string,
): Promise<{events: BridgeEvent[]; diagnostic?: string}> => {
  const result = await runBridgeOnce(
    ['tui_bridge.py', 'scan-brand-url', '--run-id', `scan_${Date.now().toString(36)}`, '--url', url],
    undefined,
    {timeoutMs: 60000},
  );
  const events = parseJsonlEvents(result.stdout);
  const errEvent = events.find(e => e.type === 'error');
  if (result.code !== 0 || errEvent) {
    return {events, diagnostic: errEvent?.message || result.stderr || 'Website scan failed'};
  }
  return {events};
};

export const runSaveBrandProfileAction = async (
  payload: Record<string, unknown>,
): Promise<{events: BridgeEvent[]; diagnostic?: string}> => {
  const result = await runBridgeOnce(
    ['tui_bridge.py', 'save-brand-profile', '--run-id', `brand_${Date.now().toString(36)}`],
    JSON.stringify(payload),
    {timeoutMs: 15000},
  );
  const events = parseJsonlEvents(result.stdout);
  const errEvent = events.find(e => e.type === 'error');
  if (result.code !== 0 || errEvent) {
    return {events, diagnostic: errEvent?.message || result.stderr || 'Brand save failed'};
  }
  return {events};
};

export const runDetectSiteTargetAction = async (
  payload: {mode: 'handoff' | 'repo'; framework?: string; repoPath?: string; contentDir?: string},
): Promise<{events: BridgeEvent[]; diagnostic?: string}> => {
  const result = await runBridgeOnce(
    ['tui_bridge.py', 'detect-site-target', '--run-id', `target_${Date.now().toString(36)}`],
    JSON.stringify(payload),
    {timeoutMs: 20000},
  );
  const events = parseJsonlEvents(result.stdout);
  const errEvent = events.find(e => e.type === 'error');
  if (result.code !== 0 || errEvent) {
    return {events, diagnostic: errEvent?.message || result.stderr || 'Site target setup failed'};
  }
  return {events};
};
