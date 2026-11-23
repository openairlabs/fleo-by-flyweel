export type ModelTier = 'balanced' | 'max';

export interface ModelOption {
  slug: string;
  name: string;
  badge?: string;
  summary: string;
}

export interface PipelineStepModelSpec {
  role: 'RESEARCH' | 'DRAFT' | 'POLISH' | 'SEO' | 'LIGHT' | 'EMBED';
  stepNumber: number;
  title: string;
  shortTitle: string;
  subtitle: string;
  modelEnv: string;
  defaults: Record<ModelTier, string>;
  options: ModelOption[];
}

export const PIPELINE_STEP_MODELS: PipelineStepModelSpec[] = [
  {
    role: 'RESEARCH',
    stepNumber: 1,
    title: 'Web & Community Research',
    shortTitle: 'Research',
    subtitle: 'Live SERP, Reddit, and Quora discovery with verified source URLs.',
    modelEnv: 'FLEO_RESEARCH_MODEL',
    defaults: {balanced: 'sonar-reasoning-pro', max: 'sonar-reasoning-pro'},
    options: [
      {slug: 'sonar-reasoning-pro', name: 'Perplexity Sonar Reasoning Pro', badge: 'Recommended', summary: 'Deep multi-step SERP + Reddit synthesis'},
      {slug: 'sonar-pro', name: 'Perplexity Sonar Pro', badge: 'Fast', summary: 'Lower-latency grounded web citations'},
      {slug: 'sonar', name: 'Perplexity Sonar', badge: 'Budget', summary: 'Lowest-cost live search queries'},
    ],
  },
  {
    role: 'DRAFT',
    stepNumber: 2,
    title: 'Article Writer (Draft)',
    shortTitle: 'Writer',
    subtitle: '3-part long-form drafting with citations, examples, and AEO structure.',
    modelEnv: 'FLEO_DRAFT_MODEL',
    defaults: {balanced: 'openai/gpt-6-sol', max: 'anthropic/claude-opus-5.5'},
    options: [
      {slug: 'openai/gpt-6-sol', name: 'OpenAI GPT-6 Sol', badge: 'Recommended', summary: 'Crisp structure, strong citations & AEO compliance'},
      {slug: 'anthropic/claude-opus-5.5', name: 'Anthropic Claude Opus 5.5', badge: 'Max Quality', summary: 'Flagship editorial depth & nuance'},
      {slug: 'openai/gpt-6.1-sol', name: 'OpenAI GPT-6.1 Sol', badge: 'Latest OpenAI', summary: 'Newest GPT-6.1 long-form writer'},
      {slug: 'anthropic/claude-sonnet-5.5', name: 'Anthropic Claude Sonnet 5.5', badge: 'Natural Voice', summary: 'Warm prose at mid-tier cost'},
      {slug: 'google/gemini-4-carbon', name: 'Google Gemini 4 Carbon', badge: 'Google Flagship', summary: 'High-context technical synthesis'},
      {slug: 'openai/gpt-6.1-terra', name: 'OpenAI GPT-6.1 Terra', badge: 'Low Cost', summary: '50% lower output token cost than Sol'},
    ],
  },
  {
    role: 'POLISH',
    stepNumber: 3,
    title: 'Humanizer & Polish',
    shortTitle: 'Polish',
    subtitle: 'Sentence-level rhythm, active voice, and readability refinement.',
    modelEnv: 'FLEO_POLISH_MODEL',
    defaults: {balanced: 'anthropic/claude-sonnet-5.5', max: 'anthropic/claude-opus-5.5'},
    options: [
      {slug: 'anthropic/claude-sonnet-5.5', name: 'Anthropic Claude Sonnet 5.5', badge: 'Recommended', summary: 'Most natural human flow & readability'},
      {slug: 'openai/gpt-6-sol', name: 'OpenAI GPT-6 Sol', badge: 'OpenAI / BYOK', summary: 'Best pick for OpenAI-only or BYOK keys'},
      {slug: 'anthropic/claude-opus-5.5', name: 'Anthropic Claude Opus 5.5', badge: 'Max Quality', summary: 'Flagship line-editing precision'},
      {slug: 'anthropic/claude-haiku-5.5', name: 'Anthropic Claude Haiku 5.5', badge: 'Budget', summary: 'Fast, ultra-low token cost polish'},
      {slug: 'google/gemini-3.8-flash', name: 'Google Gemini 3.8 Flash', badge: 'Fast', summary: 'Low-latency readability pass'},
    ],
  },
  {
    role: 'SEO',
    stepNumber: 4,
    title: 'SEO, Headings & Edits',
    shortTitle: 'SEO & Edits',
    subtitle: 'Surgical section edits, H2 optimization, title SEO, and TL;DR block.',
    modelEnv: 'FLEO_SEO_MODEL',
    defaults: {balanced: 'google/gemini-3.8-flash', max: 'google/gemini-3.8-flash'},
    options: [
      {slug: 'google/gemini-3.8-flash', name: 'Google Gemini 3.8 Flash', badge: 'Recommended', summary: 'Fast precision edits & metadata'},
      {slug: 'openai/gpt-6-sol', name: 'OpenAI GPT-6 Sol', badge: 'OpenAI / BYOK', summary: 'High-accuracy structured SEO passes'},
      {slug: 'google/gemini-4-hydrogen', name: 'Google Gemini 4 Hydrogen', badge: 'Google Next-Gen', summary: 'Stronger reasoning for complex edits'},
      {slug: 'openai/gpt-6-luna', name: 'OpenAI GPT-6 Luna', badge: 'Budget', summary: 'Fastest & lowest-cost SEO pass'},
    ],
  },
  {
    role: 'LIGHT',
    stepNumber: 5,
    title: 'Fast Classifier & Brand Scan',
    shortTitle: 'Classifier',
    subtitle: 'Topic angle framing, insight filtering, brand URL scan, and GEO judge.',
    modelEnv: 'FLEO_LIGHT_MODEL',
    defaults: {balanced: 'openai/gpt-6-luna', max: 'openai/gpt-6-luna'},
    options: [
      {slug: 'openai/gpt-6-luna', name: 'OpenAI GPT-6 Luna', badge: 'Recommended', summary: 'Sub-second JSON tasks (~$0.0005/post)'},
      {slug: 'anthropic/claude-haiku-5.5', name: 'Anthropic Claude Haiku 5.5', badge: 'Anthropic Fast', summary: 'Fast structured classification'},
      {slug: 'google/gemini-3.8-flash', name: 'Google Gemini 3.8 Flash', badge: 'Google Fast', summary: 'Low-cost JSON & rubric scoring'},
    ],
  },
  {
    role: 'EMBED',
    stepNumber: 6,
    title: 'Semantic Embeddings',
    shortTitle: 'Embeddings',
    subtitle: 'Deduplication, internal link matching, and GSC topic clustering.',
    modelEnv: 'FLEO_EMBED_MODEL',
    defaults: {balanced: 'qwen/qwen3-embedding-8b', max: 'qwen/qwen3-embedding-8b'},
    options: [
      {slug: 'qwen/qwen3-embedding-8b', name: 'Qwen3 Embedding 8B', badge: 'Recommended', summary: '32K-context semantic embeddings via OpenRouter'},
      {slug: 'openai/text-embedding-3-small', name: 'OpenAI Text Embedding 3 Small', badge: 'OpenAI / BYOK', summary: 'Native OpenAI embeddings model'},
    ],
  },
];

export const friendlyModelName = (slug: string): string => {
  for (const step of PIPELINE_STEP_MODELS) {
    const found = step.options.find(o => o.slug === slug);
    if (found) return found.name;
  }
  return slug;
};

export const formatModelOptionLabel = (
  opt: ModelOption,
  tierDefaultSlug: string,
  activeSlug: string,
  compact: boolean,
): string => {
  const isDefault = opt.slug === tierDefaultSlug;
  const tagParts: string[] = [];
  if (isDefault) tagParts.push('Default · Recommended');
  else if (opt.badge) tagParts.push(opt.badge);

  const check = opt.slug === activeSlug ? '● ' : '○ ';
  const tag = tagParts.length > 0 ? ` [${tagParts.join(' · ')}]` : '';
  return compact ? `${check}${opt.name}${tag}` : `${check}${opt.name}${tag} — ${opt.summary}`;
};
