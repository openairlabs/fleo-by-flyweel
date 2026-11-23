import {parseJsonlEvents, runBridgeOnce} from './bridge.js';
import type {AppState, BridgeEvent} from './types.js';

interface FinalArtifactData {
  artifact?: string;
  content?: string;
  rawContent?: string;
  metadata?: {
    keyword?: string;
    style?: string;
    title?: string;
    slug?: string;
    qualityScorecard?: Record<string, unknown>;
    modelUsed?: Record<string, unknown>;
    keywordContext?: string;
  };
}

const latestFinalArtifact = (state: AppState): FinalArtifactData | undefined => {
  for (const artifact of [...state.artifacts].reverse()) {
    const data = artifact.data as FinalArtifactData | undefined;
    if (data?.artifact === 'final_mdx' && data.content) return data;
  }
  return undefined;
};

const buildResultPayload = (state: AppState): Record<string, unknown> | undefined => {
  const artifact = latestFinalArtifact(state);
  if (!artifact) return undefined;
  const metadata = artifact.metadata ?? {};
  return {
    keyword: metadata.keyword ?? state.config.keyword,
    style: metadata.style ?? state.config.style,
    title: metadata.title ?? 'Generated Article',
    slug: metadata.slug ?? 'generated-article',
    content: artifact.content ?? '',
    raw_content: artifact.rawContent ?? '',
    quality_scorecard: metadata.qualityScorecard ?? state.qualityMetrics,
    model_used: metadata.modelUsed ?? {},
    keyword_context: metadata.keywordContext ?? '',
  };
};

export const runArtifactAction = async (
  state: AppState,
  action: 'save-artifact' | 'publish-artifact',
  options: {signal?: AbortSignal} = {},
): Promise<{events: BridgeEvent[]; diagnostic?: string}> => {
  const payload = buildResultPayload(state);
  if (!payload) {
    return {events: [], diagnostic: 'No final artifact is available yet.'};
  }
  const args = ['tui_bridge.py', action, '--run-id', state.runId ?? 'local'];
  const conflictAction = action === 'save-artifact' ? 'save' : 'publish';
  if (
    state.artifactConflict?.action === conflictAction &&
    state.artifactConflict.slug === payload.slug
  ) {
    args.push('--overwrite');
  }
  const result = await runBridgeOnce(args, JSON.stringify(payload), {signal: options.signal});
  const events = parseJsonlEvents(result.stdout);
  if (result.code !== 0) return {events, diagnostic: result.stderr || `Artifact action exited ${result.code ?? 'without a code'}`};
  return {events};
};
