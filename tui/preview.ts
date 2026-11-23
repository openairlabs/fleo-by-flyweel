import type {AppState, Artifact} from './types.js';

interface ArtifactData {
  artifact?: string;
  content?: string;
  rawContent?: string;
  metadata?: Record<string, unknown>;
  citations?: unknown;
  quality?: unknown;
}

export interface PreviewModel {
  title: string;
  slug: string;
  article: string;
  frontmatter: string;
  metadata: Record<string, unknown>;
  citations: string[];
  quality: Record<string, unknown>;
  rawDraft: string;
}

const asRecord = (value: unknown): Record<string, unknown> => (value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {});

const artifactData = (artifact?: Artifact): ArtifactData => asRecord(artifact?.data) as ArtifactData;

export const latestArtifactByName = (state: AppState, names: string[]): Artifact | undefined => {
  for (const artifact of [...state.artifacts].reverse()) {
    const data = artifactData(artifact);
    if (data.artifact && names.includes(data.artifact)) return artifact;
  }
  return undefined;
};

const splitFrontmatter = (content: string): {frontmatter: string; article: string} => {
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
  if (!match) return {frontmatter: '', article: content.trim()};
  return {
    frontmatter: match[1]?.trim() ?? '',
    article: content.slice(match[0].length).trim(),
  };
};

const extractLinks = (content: string): string[] => {
  const citations: string[] = [];
  const markdownLinkPattern = /\[([^\]]+)]\((https?:\/\/[^)\s]+)\)/g;
  for (const match of content.matchAll(markdownLinkPattern)) {
    citations.push(`${match[1]} -> ${match[2]}`);
  }
  const rawUrlPattern = /https?:\/\/[^\s)"'<>]+/g;
  for (const match of content.matchAll(rawUrlPattern)) {
    if (!citations.some(citation => citation.includes(match[0]))) citations.push(match[0]);
  }
  return citations.slice(0, 12);
};

const citationsFromMetadata = (value: unknown): string[] => {
  if (!Array.isArray(value)) return [];
  return value
    .map(item => {
      if (typeof item === 'string') return item;
      const record = asRecord(item);
      const title = String(record.title ?? record.source ?? record.label ?? 'citation');
      const url = record.url ? ` -> ${String(record.url)}` : '';
      return `${title}${url}`;
    })
    .filter(Boolean)
    .slice(0, 12);
};

export const buildPreviewModel = (state: AppState): PreviewModel => {
  const finalArtifact = latestArtifactByName(state, ['final_mdx']);
  const data = artifactData(finalArtifact);
  const metadata = {
    ...state.generationSummary,
    ...asRecord(data.metadata),
  };
  const content = data.content ?? '';
  const {frontmatter, article} = splitFrontmatter(content);
  const metadataCitations = citationsFromMetadata(data.citations ?? metadata.citations ?? metadata.sources);
  const citations = [...metadataCitations, ...extractLinks(content)].slice(0, 12);
  const quality = {
    ...state.qualityMetrics,
    ...asRecord(metadata.qualityScorecard),
    ...asRecord(data.quality),
  };
  return {
    title: String(metadata.title ?? 'pending'),
    slug: String(metadata.slug ?? 'pending'),
    article,
    frontmatter,
    metadata,
    citations,
    quality,
    rawDraft: data.rawContent ?? '',
  };
};
