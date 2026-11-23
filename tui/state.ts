import {latestArtifactByName} from './preview.js';
import type {AppAction, AppState, BridgeEvent, GenerationConfig, KeywordSuggestion, LogEntry, Notification, PlanPhase, ProgressRow, SetupStatus} from './types.js';

export const defaultConfig: GenerationConfig = {
  keyword: '',
  style: 'standard',
  depth: 'standard',
  communityMode: 'full',
  flyweelMode: 'full',
  icpEnabled: true,
  gscCheck: true,
  gscKeywords: true,
  solutionCount: 8,
  competitors: '',
  competitorMentions: true,
  note: '',
  keywordContext: '',
  saveDefault: 'preview',
};

type InitialStatePatch = Partial<Omit<AppState, 'config'>> & {config?: Partial<GenerationConfig>};

const setupKeyAliases: Record<string, string> = {
  openrouter: 'OPENROUTER_API_KEY',
  openrouter_api_key: 'OPENROUTER_API_KEY',
  openrouter_api: 'OPENROUTER_API_KEY',
  google: 'GOOGLE_API_KEY',
  google_api_key: 'GOOGLE_API_KEY',
  gemini: 'GOOGLE_API_KEY',
  gemini_api_key: 'GOOGLE_API_KEY',
  perplexity: 'PERPLEXITY_API_KEY',
  perplexity_api_key: 'PERPLEXITY_API_KEY',
  gsc_service_account: 'GOOGLE_SERVICE_ACCOUNT_PATH',
  google_service_account_path: 'GOOGLE_SERVICE_ACCOUNT_PATH',
  gsc_site_url: 'GSC_SITE_URL',
  gsc_property_url: 'GSC_PROPERTY_URL',
  interfaze: 'INTERFAZE_API_KEY',
  interfaze_api_key: 'INTERFAZE_API_KEY',
  astro_blog_path: 'ASTRO_BLOG_PATH',
  astro_path: 'ASTRO_BLOG_PATH',
  output_dir: 'OUTPUT_DIR',
  output: 'OUTPUT_DIR',
};

const secretSetupKeys = new Set([
  'OPENROUTER_API_KEY',
  'GOOGLE_API_KEY',
  'GEMINI_API_KEY',
  'PERPLEXITY_API_KEY',
  'INTERFAZE_API_KEY',
  'INDEXNOW_KEY',
  'FLEO_RESEARCH_API_KEY',
  'FLEO_DRAFT_API_KEY',
  'FLEO_POLISH_API_KEY',
  'FLEO_SEO_API_KEY',
  'FLEO_LIGHT_API_KEY',
  'FLEO_EMBED_API_KEY',
  'FLEO_EXTRACTOR_API_KEY',
  'FLEO_DECISIONS_API_KEY',
]);

export const normalizeSetupKey = (key: string): string | undefined => {
  const normalized = key.trim().toLowerCase().replace(/-/g, '_');
  const direct = setupKeyAliases[normalized];
  if (direct) return direct;
  const upper = key.trim().toUpperCase();
  if (upper.startsWith('FLEO_') || secretSetupKeys.has(upper) || upper === 'MODEL_TIER') return upper;
  return Object.values(setupKeyAliases).includes(upper) ? upper : undefined;
};

export const maskSetupValue = (key: string, value: string): string => {
  if (!value) return '';
  if (!secretSetupKeys.has(key) && !key.toUpperCase().endsWith('_API_KEY')) return value;
  return value.length <= 6 ? '******' : `${value.slice(0, 3)}...${value.slice(-3)}`;
};

// Notification ids come from an ever-incrementing counter (never array length — the
// list is trimmed, so length-derived ids collide and break React keys).
const notifyPatch = (
  state: AppState,
  tone: Notification['tone'],
  message: string,
): Pick<AppState, 'notifications' | 'noticeSeq'> => {
  const noticeSeq = state.noticeSeq + 1;
  return {
    noticeSeq,
    notifications: [...state.notifications.slice(-4), {id: `n-${noticeSeq}`, tone, message}],
  };
};

const logPatch = (
  state: AppState,
  level: LogEntry['level'],
  text: string,
): Pick<AppState, 'log' | 'logSeq'> => {
  const logSeq = state.logSeq + 1;
  return {
    logSeq,
    log: [...state.log.slice(-499), {id: logSeq, level, text}],
  };
};

const mergeComments = (existing: string[], incoming: unknown): string[] => {
  if (!Array.isArray(incoming)) return existing;
  const comments = incoming.map(comment => String(comment).trim()).filter(Boolean);
  return [...existing, ...comments.filter(comment => !existing.includes(comment))];
};

export const hasFinalArtifact = (state: AppState): boolean => Boolean(latestArtifactByName(state, ['final_mdx']));

export const createInitialState = (patch: InitialStatePatch = {}): AppState => {
  const {config, ...rest} = patch;
  const hasKeyword = Boolean(config?.keyword && config.keyword.trim());
  return {
    step: 'config',
    returnStep: 'config',
    configStage: hasKeyword ? 'settings' : 'keyword',
    helpOpen: false,
    logOpen: false,
    cancelPending: false,
    publishArmed: false,
    commandMode: false,
    inputValue: '',
    setupDraft: {},
    setupConfirmPending: false,
    planPhases: [],
    progressRows: [],
    artifacts: [],
    queuedComments: [],
    notifications: [],
    noticeSeq: 0,
    log: [],
    logSeq: 0,
    saveState: 'idle',
    publishState: 'idle',
    outputPaths: {},
    qualityMetrics: {},
    generationSummary: {},
    interpretState: 'idle',
    interpretAttempt: 0,
    runRequestId: 0,
    setupRequestId: 0,
    setupSaveRequestId: 0,
    interpretRequestId: 0,
    saveRequestId: 0,
    publishRequestId: 0,
    suggestState: 'idle',
    suggestAttempt: 0,
    suggestRequestId: 0,
    // Trends default to fast — fresh (live web search) is the Ctrl+T opt-in.
    suggestTrendsFresh: false,
    topicMode: 'angle',
    ...rest,
    config: {...defaultConfig, ...(config ?? {})},
  };
};

const statusFromEvent = (event: BridgeEvent): ProgressRow['status'] => {
  if (event.type === 'phase_started') return 'running';
  if (event.type === 'task_progress') return 'running';
  if (event.type === 'review_gate_opened') return 'running';
  return 'complete';
};

// One row per task (or per phase when the bridge sends no taskId). Messages update the
// row's detail IN PLACE instead of appending a new row per message.
const progressKey = (event: BridgeEvent): string => (event.taskId ? `task:${event.taskId}` : `phase:${event.phase ?? 'run'}`);

const humanizePhase = (phase: string): string => phase.charAt(0).toUpperCase() + phase.slice(1);

const upsertProgress = (rows: ProgressRow[], event: BridgeEvent): ProgressRow[] => {
  const id = progressKey(event);
  // A new phase starting settles earlier phase-level rows still marked running.
  const base = event.type === 'phase_started'
    ? rows.map(row => (row.id !== id && row.id.startsWith('phase:') && row.status === 'running' ? {...row, status: 'complete' as const} : row))
    : rows;
  const existing = base.find(row => row.id === id);
  const next: ProgressRow = {
    id,
    label: event.taskId ?? humanizePhase(event.phase ?? 'run'),
    phase: event.phase ?? existing?.phase ?? 'run',
    agent: event.agent ?? existing?.agent,
    status: statusFromEvent(event),
    detail: event.message ?? existing?.detail,
  };
  return existing ? base.map(row => (row.id === id ? next : row)) : [...base, next];
};

// ── plan-aware progress (run_plan seeded todo list) ─────────────────────────

// Legacy/fuzzy phase keys the bridge may still emit, mapped onto the manifest
// phases the run_plan seeds rows under.
const PHASE_ALIASES: Record<string, string> = {
  generation: 'draft',
  polishing: 'refinement',
  citation: 'refinement',
  review: 'refinement',
};

export const canonicalPhase = (phase?: string): string => (phase ? PHASE_ALIASES[phase] ?? phase : 'run');

const BRIDGE_STATUS: Record<string, ProgressRow['status']> = {
  pending: 'pending',
  running: 'running',
  complete: 'complete',
  skipped: 'skipped',
  failed: 'error',
};

interface TaskEventData {
  status?: string;
  label?: string;
  tasks?: Array<{id: string; label: string}>;
  progressCurrent?: number;
  progressTotal?: number;
  insights?: number;
  query?: string;
  elapsed?: number;
}

const taskEventData = (event: BridgeEvent): TaskEventData =>
  event.data && typeof event.data === 'object' ? (event.data as TaskEventData) : {};

const seedRowsFromPlan = (plan: PlanPhase[]): ProgressRow[] =>
  plan.flatMap(planPhase =>
    planPhase.tasks.map(task => ({
      id: `task:${task.id}`,
      label: task.label,
      phase: planPhase.phase,
      status: 'pending' as const,
    })),
  );

// Insert a dynamic row after the last row of its phase group (or at the end).
const insertIntoPhase = (rows: ProgressRow[], row: ProgressRow): ProgressRow[] => {
  let lastIndex = -1;
  rows.forEach((candidate, index) => {
    if (candidate.phase === row.phase) lastIndex = index;
  });
  if (lastIndex === -1) return [...rows, row];
  return [...rows.slice(0, lastIndex + 1), row, ...rows.slice(lastIndex + 1)];
};

const applyTaskUpdate = (row: ProgressRow, event: BridgeEvent): ProgressRow => {
  const data = taskEventData(event);
  const status = BRIDGE_STATUS[data.status ?? ''] ?? 'running';
  const terminal = status === 'complete' || status === 'skipped' || status === 'error';
  const hasBar = typeof data.progressCurrent === 'number' && typeof data.progressTotal === 'number';
  const message = event.message?.trim() ? event.message : undefined;
  return {
    ...row,
    status,
    agent: event.agent ?? row.agent,
    detail: message ?? row.detail,
    elapsed: data.elapsed ?? (terminal ? row.elapsed : undefined),
    // Terminal transitions and fresh plain details supersede the n/m bar.
    progressCurrent: terminal ? undefined : hasBar ? data.progressCurrent : message ? undefined : row.progressCurrent,
    progressTotal: terminal ? undefined : hasBar ? data.progressTotal : message ? undefined : row.progressTotal,
    insights: terminal ? undefined : hasBar ? data.insights : message ? undefined : row.insights,
    query: terminal ? undefined : hasBar ? data.query : message ? undefined : row.query,
  };
};

// Route an event onto the seeded todo rows. Phase-scoped taskIds ("research:
// serp-analysis") update their row in place; unknown phase-scoped ids append a
// dynamic row inside their phase group; legacy taskId-less (or unscoped-id)
// messages become detail on the running row of that phase — never orphan rows.
const planAwareProgress = (rows: ProgressRow[], event: BridgeEvent): ProgressRow[] => {
  const data = taskEventData(event);
  const phase = canonicalPhase(event.phase);

  if (event.type === 'phase_started') {
    let next = rows;
    for (const task of data.tasks ?? []) {
      if (!next.some(row => row.id === `task:${task.id}`)) {
        next = insertIntoPhase(next, {id: `task:${task.id}`, label: task.label, phase, status: 'pending'});
      }
    }
    return next;
  }

  const scopedTaskId = event.taskId && event.taskId.includes(':') ? event.taskId : undefined;
  if (scopedTaskId) {
    const id = `task:${scopedTaskId}`;
    const existing = rows.find(row => row.id === id);
    if (existing) return rows.map(row => (row.id === id ? applyTaskUpdate(row, event) : row));
    const seeded: ProgressRow = {id, label: data.label ?? scopedTaskId, phase, status: 'pending'};
    return insertIntoPhase(rows, seeded).map(row => (row.id === id ? applyTaskUpdate(row, event) : row));
  }

  // Review-gate lifecycle events map onto the seeded Review Gate row.
  if (event.type === 'review_gate_opened' || event.type === 'review_gate_closed') {
    const gateId = 'task:refinement:review-gate';
    if (rows.some(row => row.id === gateId)) {
      return rows.map(row =>
        row.id === gateId
          ? {
              ...row,
              status: event.type === 'review_gate_opened' ? ('running' as const) : ('complete' as const),
              detail: event.message ?? row.detail,
            }
          : row,
      );
    }
    return rows;
  }

  // Legacy coarse message: annotate the running row of this phase (or the
  // first still-pending one) with the message as detail.
  const message = event.message?.trim();
  if (!message) return rows;
  const target =
    rows.find(row => row.phase === phase && row.status === 'running') ??
    rows.find(row => row.phase === phase && row.status === 'pending');
  if (!target) return rows;
  return rows.map(row => (row.id === target.id ? {...row, detail: message} : row));
};

const settleRunningRows = (rows: ProgressRow[], planSeeded = false): ProgressRow[] =>
  rows.map(row => {
    if (row.status === 'running') return {...row, status: 'complete' as const};
    if (planSeeded && row.status === 'pending') return {...row, status: 'skipped' as const, detail: row.detail ?? 'Not run'};
    return row;
  });

const setupStatusFromEvent = (event: BridgeEvent): SetupStatus | undefined => {
  if (!event.data || typeof event.data !== 'object') return undefined;
  return event.data as SetupStatus;
};

export const reduceBridgeEvent = (state: AppState, event: BridgeEvent): AppState => {
  const base: AppState = {
    ...state,
    runId: state.runId ?? event.runId,
    activePhase: event.phase ?? state.activePhase,
    activeAgent: event.agent ?? state.activeAgent,
  };

  const planSeeded = state.planPhases.length > 0;

  switch (event.type) {
    case 'setup_status':
      return {
        ...base,
        setupStatus: setupStatusFromEvent(event),
      };
    case 'run_plan': {
      const plan = (event.data as {plan?: PlanPhase[]} | undefined)?.plan ?? [];
      if (!Array.isArray(plan) || plan.length === 0) return base;
      return {
        ...base,
        planPhases: plan.map(({phase, label}) => ({phase, label})),
        progressRows: seedRowsFromPlan(plan),
      };
    }
    case 'phase_started':
    case 'task_progress':
      if (event.phase === 'setup') return base;
      return {
        ...base,
        step: state.step === 'config' || state.step === 'setup' ? 'running' : state.step,
        progressRows: planSeeded
          ? planAwareProgress(state.progressRows, event)
          : upsertProgress(state.progressRows, event),
      };
    case 'tool_call':
      return {
        ...base,
        ...logPatch(state, 'tool', `${event.phase ?? 'run'}: ${event.message ?? 'Tool call'}`),
      };
    case 'warning':
      return {
        ...base,
        ...notifyPatch(state, 'warning', event.message ?? 'Warning'),
        ...logPatch(state, 'warn', event.message ?? 'Warning'),
      };
    case 'error': {
      const message = event.message ?? 'Error';
      const failsRun = state.step === 'running' || state.step === 'review';
      return {
        ...base,
        // An error during config/setup/context (e.g. topic interpretation) is a
        // notification, not a journey change; a mid-run error ends the run at output.
        step: failsRun ? 'output' : state.step,
        outcome: failsRun ? 'error' : state.outcome,
        interpretState: event.taskId === 'topic-interpretation' ? 'failed' : state.interpretState,
        suggestState: event.taskId === 'keyword-suggestions' ? 'failed' : state.suggestState,
        lastError: message,
        commandMode: state.commandMode === 'review' && failsRun ? false : state.commandMode,
        ...notifyPatch(state, 'error', message),
        ...logPatch(state, 'error', message),
      };
    }
    case 'artifact_ready':
      if (event.data && typeof event.data === 'object' && (event.data as {artifact?: string}).artifact === 'topic_interpretation') {
        const context = (event.data as {context?: string}).context ?? '';
        return {
          ...base,
          config: {...state.config, keywordContext: context},
          interpretState: 'done',
          artifacts: [
            ...state.artifacts,
            {id: event.taskId ?? `artifact-${state.artifacts.length + 1}`, phase: event.phase, message: event.message, data: event.data},
          ],
        };
      }
      if (event.data && typeof event.data === 'object' && (event.data as {artifact?: string}).artifact === 'keyword_suggestions') {
        const rawSuggestions = (event.data as {suggestions?: unknown}).suggestions;
        const suggestions = Array.isArray(rawSuggestions) ? (rawSuggestions as KeywordSuggestion[]) : [];
        // Additive field — only trust it when it's an actual boolean, else leave undefined.
        const rawGscAvailable = (event.data as {gscAvailable?: unknown}).gscAvailable;
        return {
          ...base,
          keywordSuggestions: suggestions,
          suggestGscAvailable: typeof rawGscAvailable === 'boolean' ? rawGscAvailable : undefined,
          suggestState: 'done',
          artifacts: [
            ...state.artifacts,
            {id: event.taskId ?? `artifact-${state.artifacts.length + 1}`, phase: event.phase, message: event.message, data: event.data},
          ],
        };
      }
      return {
        ...base,
        artifacts: [
          ...state.artifacts,
          {id: event.taskId ?? `artifact-${state.artifacts.length + 1}`, phase: event.phase, message: event.message, data: event.data},
        ],
      };
    case 'artifact_conflict': {
      const data = (event.data ?? {}) as {slug?: unknown; action?: unknown};
      const action = data.action === 'save' ? 'save' : 'publish';
      const slug = String(data.slug ?? 'existing artifact');
      return {
        ...base,
        saveState: action === 'save' ? 'failed' : state.saveState,
        publishState: action === 'publish' ? 'failed' : state.publishState,
        artifactConflict: {action, slug},
        ...notifyPatch(state, 'warning', `Artifact already exists for ${slug}. Confirm the same ${action} action again to overwrite that exact slug.`),
      };
    }
    case 'review_gate_opened':
      return {
        ...base,
        step: 'review',
        commandMode: 'review',
        queuedComments: mergeComments(state.queuedComments, (event.data as {queuedComments?: unknown} | undefined)?.queuedComments),
        progressRows: planSeeded
          ? planAwareProgress(state.progressRows, event)
          : upsertProgress(state.progressRows, event),
      };
    case 'review_gate_closed':
      return {
        ...base,
        step: 'running',
        commandMode: false,
        inputValue: '',
        progressRows: planSeeded
          ? planAwareProgress(state.progressRows, event)
          : upsertProgress(state.progressRows, event),
      };
    case 'gsc_gate_opened': {
      const data = (event.data ?? {}) as {
        score?: number;
        threshold?: number;
        trafficPotential?: number;
        primaryQuery?: string;
        estimated?: boolean;
        neighborCount?: number | null;
      };
      const message = event.message ?? 'GSC score is below the acceptable threshold';
      return {
        ...base,
        gscGate: {
          score: Number(data.score ?? 0),
          threshold: Number(data.threshold ?? 25),
          trafficPotential: typeof data.trafficPotential === 'number' ? data.trafficPotential : undefined,
          primaryQuery: data.primaryQuery ? String(data.primaryQuery) : undefined,
          // Additive fields — absent on old bridges; never assume they exist.
          estimated: data.estimated === true ? true : undefined,
          neighborCount: typeof data.neighborCount === 'number' ? data.neighborCount : undefined,
        },
        commandMode: false,
        inputValue: '',
        ...notifyPatch(state, 'warning', message),
        ...logPatch(state, 'warn', message),
      };
    }
    case 'gsc_gate_closed':
      return {
        ...base,
        gscGate: undefined,
        ...logPatch(state, 'info', event.message ?? 'GSC gate closed'),
      };
    case 'generation_complete': {
      const data = (event.data ?? {}) as {metrics?: Record<string, unknown>; qualityScorecard?: Record<string, unknown>};
      return {
        ...base,
        step: 'preview',
        generationSummary: data as Record<string, unknown>,
        qualityMetrics: data.qualityScorecard ?? data.metrics ?? state.qualityMetrics,
        progressRows: planSeeded
          ? settleRunningRows(state.progressRows, true)
          : settleRunningRows(upsertProgress(state.progressRows, event)),
      };
    }
    case 'saved':
      if (event.phase === 'setup') {
        return {
          ...base,
          step: 'setup',
          setupDraft: {},
          setupConfirmPending: false,
          ...notifyPatch(state, 'success', event.message ?? 'Setup saved'),
        };
      }
      return {
        ...base,
        step: 'output',
        outcome: state.outcome ?? 'complete',
        saveState: 'done',
        outputPaths: {...state.outputPaths, ...((event.data as Record<string, string>) ?? {})},
        ...notifyPatch(state, 'success', event.message ?? 'Saved'),
      };
    case 'published':
      return {
        ...base,
        step: 'output',
        outcome: state.outcome ?? 'complete',
        publishState: 'done',
        ...notifyPatch(state, 'success', event.message ?? 'Published'),
      };
    default:
      return base;
  }
};

const styleValues: GenerationConfig['style'][] = ['standard', 'guide', 'comparison', 'research', 'news', 'category', 'top-compare', 'feature'];
const depthValues: GenerationConfig['depth'][] = ['quick', 'standard', 'comprehensive'];
const communityValues: GenerationConfig['communityMode'][] = ['full', 'limited', 'skip'];
const flyweelValues: GenerationConfig['flyweelMode'][] = ['full', 'limited', 'none'];
const outputValues: GenerationConfig['saveDefault'][] = ['preview', 'local', 'publish'];
const solutionValues: GenerationConfig['solutionCount'][] = [5, 8, 10, 15];

const parseBoolean = (value: string): boolean | undefined => {
  const normalized = value.trim().toLowerCase();
  if (['on', 'yes', 'true', 'enabled', 'enable', '1'].includes(normalized)) return true;
  if (['off', 'no', 'false', 'disabled', 'disable', '0'].includes(normalized)) return false;
  return undefined;
};

// Colon-command grammar. Kept as a state-level API (and for tests) even though the
// current UI never opens command mode — the reducer treats it as plain input.
export const applyCommand = (state: AppState, rawCommand: string): AppState => {
  const command = rawCommand.trim();
  if (!command) return {...state, commandMode: false, inputValue: ''};
  const [verbRaw = '', ...restParts] = command.split(/\s+/);
  const verb = verbRaw.toLowerCase();
  const rest = restParts.join(' ').trim();
  const usage = (message: string): AppState => ({
    ...state,
    commandMode: false,
    inputValue: '',
    ...logPatch(state, 'warn', message),
  });

  if (['keyword', 'k'].includes(verb)) {
    if (!rest) return usage('Usage: keyword <topic>');
    return {...state, commandMode: false, inputValue: '', config: {...state.config, keyword: rest}, step: 'config'};
  }
  if (verb === 'style') {
    if (!styleValues.includes(rest as GenerationConfig['style'])) return usage(`Style must be one of: ${styleValues.join(', ')}`);
    return {...state, commandMode: false, inputValue: '', config: {...state.config, style: rest as GenerationConfig['style']}, step: 'config'};
  }
  if (verb === 'depth') {
    if (!depthValues.includes(rest as GenerationConfig['depth'])) return usage(`Depth must be one of: ${depthValues.join(', ')}`);
    return {...state, commandMode: false, inputValue: '', config: {...state.config, depth: rest as GenerationConfig['depth']}, step: 'config'};
  }
  if (verb === 'community') {
    if (!communityValues.includes(rest as GenerationConfig['communityMode'])) return usage(`Community mode must be one of: ${communityValues.join(', ')}`);
    return {...state, commandMode: false, inputValue: '', config: {...state.config, communityMode: rest as GenerationConfig['communityMode']}, step: 'config'};
  }
  if (verb === 'flyweel' || verb === 'brand') {
    if (!flyweelValues.includes(rest as GenerationConfig['flyweelMode'])) return usage(`Brand mode must be one of: ${flyweelValues.join(', ')}`);
    return {...state, commandMode: false, inputValue: '', config: {...state.config, flyweelMode: rest as GenerationConfig['flyweelMode']}, step: 'config'};
  }
  if (verb === 'icp') {
    const value = parseBoolean(rest);
    if (value === undefined) return usage('Usage: icp on|off');
    return {...state, commandMode: false, inputValue: '', config: {...state.config, icpEnabled: value}, step: 'config'};
  }
  if (verb === 'gsc') {
    const value = parseBoolean(rest);
    if (value === undefined) return usage('Usage: gsc on|off');
    return {...state, commandMode: false, inputValue: '', config: {...state.config, gscCheck: value}, step: 'config'};
  }
  if (['gsc-keywords', 'keywords'].includes(verb)) {
    const value = parseBoolean(rest);
    if (value === undefined) return usage('Usage: gsc-keywords on|off');
    return {...state, commandMode: false, inputValue: '', config: {...state.config, gscKeywords: value}, step: 'config'};
  }
  if (['solutions', 'solution-count'].includes(verb)) {
    const value = Number.parseInt(rest, 10);
    if (!solutionValues.includes(value as GenerationConfig['solutionCount'])) return usage(`Solutions must be one of: ${solutionValues.join(', ')}`);
    return {...state, commandMode: false, inputValue: '', config: {...state.config, solutionCount: value as GenerationConfig['solutionCount']}, step: 'config'};
  }
  if (['competitors', 'comp'].includes(verb)) {
    return {...state, commandMode: false, inputValue: '', config: {...state.config, competitors: rest}, step: 'config'};
  }
  if (verb === 'clear-competitors') {
    return {...state, commandMode: false, inputValue: '', config: {...state.config, competitors: ''}, step: 'config'};
  }
  if (verb === 'note') {
    return {...state, commandMode: false, inputValue: '', config: {...state.config, note: rest}, step: 'config'};
  }
  if (['context', 'keyword-context'].includes(verb)) {
    return {...state, commandMode: false, inputValue: '', config: {...state.config, keywordContext: rest}, step: 'config'};
  }
  if (verb === 'interpret') {
    if (!state.config.keyword.trim()) return usage('Set a keyword before interpreting: keyword <topic>');
    return {...state, commandMode: false, inputValue: '', step: 'context', ...startInterpretation(state)};
  }
  if (['competitor-mentions', 'competitor'].includes(verb)) {
    const value = parseBoolean(rest);
    if (value === undefined) return usage('Usage: competitor-mentions on|off');
    return {...state, commandMode: false, inputValue: '', config: {...state.config, competitorMentions: value}, step: 'config'};
  }
  if (['output', 'save-default'].includes(verb)) {
    if (!outputValues.includes(rest as GenerationConfig['saveDefault'])) return usage(`Output must be one of: ${outputValues.join(', ')}`);
    return {...state, commandMode: false, inputValue: '', config: {...state.config, saveDefault: rest as GenerationConfig['saveDefault']}, step: 'config'};
  }
  if (['setup', 'check'].includes(verb)) {
    return {
      ...state,
      commandMode: false,
      inputValue: '',
      returnStep: state.step === 'setup' ? state.returnStep : state.step,
      step: 'setup',
      setupRequestId: state.setupRequestId + 1,
    };
  }
  if (['setup-set', 'set-env', 'set-path'].includes(verb)) {
    const [rawKey = '', ...valueParts] = restParts;
    const key = normalizeSetupKey(rawKey);
    const value = valueParts.join(' ').trim();
    if (!key || !value) return usage('Usage: setup-set <openrouter|google-api-key|perplexity|gsc-site-url|astro-blog-path|output-dir> <value>');
    return {
      ...state,
      commandMode: false,
      inputValue: '',
      step: 'setup',
      setupDraft: {...state.setupDraft, [key]: value},
      setupConfirmPending: false,
      ...notifyPatch(state, 'info', `Staged setup value for ${key}: ${maskSetupValue(key, value)}`),
    };
  }
  if (verb === 'save-setup') {
    if (Object.keys(state.setupDraft).length === 0) return usage('No setup changes are staged. Use setup-set <key> <value>.');
    return {
      ...state,
      commandMode: false,
      inputValue: '',
      step: 'setup',
      setupConfirmPending: true,
      ...notifyPatch(state, 'warning', 'Setup changes staged. Type :confirm-setup to write .env or :cancel-setup to discard.'),
    };
  }
  if (verb === 'confirm-setup') {
    if (!state.setupConfirmPending || Object.keys(state.setupDraft).length === 0) return usage('No setup save is awaiting confirmation.');
    return {
      ...state,
      commandMode: false,
      inputValue: '',
      step: 'setup',
      setupConfirmPending: false,
      setupSaveRequestId: state.setupSaveRequestId + 1,
    };
  }
  if (verb === 'cancel-setup') {
    return {
      ...state,
      commandMode: false,
      inputValue: '',
      step: 'setup',
      setupDraft: {},
      setupConfirmPending: false,
      ...notifyPatch(state, 'info', 'Discarded staged setup changes.'),
    };
  }
  if (['start', 'generate', 'run'].includes(verb)) {
    if (!state.config.keyword.trim()) return usage('Set a keyword before starting: keyword <topic>');
    if (state.setupStatus && !state.setupStatus.canGenerate) {
      return usage(`Setup is missing required values: ${state.setupStatus.requiredMissing.join(', ')}`);
    }
    return {...state, commandMode: false, inputValue: '', step: 'running', runRequestId: state.runRequestId + 1};
  }
  if (verb === 'help') {
    return {...state, commandMode: false, inputValue: '', helpOpen: true};
  }
  if (verb === 'clear') {
    return {...state, commandMode: false, inputValue: '', log: [], notifications: []};
  }
  if (['dismiss', 'clear-notification'].includes(verb)) {
    return {...state, commandMode: false, inputValue: '', notifications: state.notifications.slice(0, -1)};
  }
  return usage(`Unknown command: ${verb}`);
};

// Kick off a (re)interpretation: bump the request counter + take counter and stage the
// optional steering feedback for App's interpret effect. The previous take travels on
// EVERY regen (fresh Ctrl+N or steered) — without it the bridge gets a byte-identical
// prompt and a fresh regenerate can come back as the exact same interpretation.
const startInterpretation = (
  state: AppState,
  feedback?: string,
): Pick<AppState, 'interpretState' | 'interpretAttempt' | 'interpretRequestId' | 'interpretFeedback' | 'interpretPrevious'> => ({
  interpretState: 'running',
  interpretAttempt: state.interpretAttempt + 1,
  interpretRequestId: state.interpretRequestId + 1,
  interpretFeedback: feedback,
  interpretPrevious: state.config.keywordContext.trim() || undefined,
});

// Kick off a (re)generation of keyword ideas: bump the request counter + take counter
// and stage the optional steer text. Unlike startInterpretation, previous ideas travel
// on EVERY regen (fresh or steered) so takes don't repeat the same 5 keywords.
const startSuggestion = (
  state: AppState,
  steer?: string,
): Pick<AppState, 'suggestState' | 'suggestAttempt' | 'suggestRequestId' | 'suggestSteer' | 'suggestPrevious'> => ({
  suggestState: 'running',
  suggestAttempt: state.suggestAttempt + 1,
  suggestRequestId: state.suggestRequestId + 1,
  suggestSteer: steer,
  // Stage previous ideas from the mode-primary field so regen doesn't repeat them: the
  // angle in angle mode (falling back to the keyword), the keyword otherwise.
  suggestPrevious: (state.keywordSuggestions ?? []).map(suggestion =>
    state.topicMode === 'angle' ? suggestion.angle ?? suggestion.keyword : suggestion.keyword,
  ),
});

// Reset everything scoped to a single run, keeping config + environment knowledge, so
// "Start new article" drops the user back at settings with their choices intact.
// Request-id counters are monotonic and must never reset (App's lastXRef guards).
const resetForNewRun = (state: AppState): AppState => ({
  ...state,
  step: 'config',
  returnStep: 'config',
  configStage: 'settings',
  outcome: undefined,
  lastError: undefined,
  runId: undefined,
  activePhase: undefined,
  activeAgent: undefined,
  helpOpen: false,
  logOpen: false,
  cancelPending: false,
  publishArmed: false,
  gscGate: undefined,
  commandMode: false,
  inputValue: '',
  planPhases: [],
  progressRows: [],
  artifacts: [],
  queuedComments: [],
  notifications: [],
  log: [],
  saveState: 'idle',
  publishState: 'idle',
  artifactConflict: undefined,
  outputPaths: {},
  qualityMetrics: {},
  generationSummary: {},
  interpretState: 'idle',
  interpretAttempt: 0,
  interpretFeedback: undefined,
  interpretPrevious: undefined,
  suggestState: 'idle',
  suggestAttempt: 0,
  suggestSteer: undefined,
  suggestPrevious: undefined,
  keywordSuggestions: undefined,
  // topicMode + suggestTrendsFresh are standing preferences — intentionally NOT reset.
  suggestGscAvailable: undefined,
  startedAt: undefined,
  completedAt: undefined,
});

export const reducer = (state: AppState, action: AppAction): AppState => {
  switch (action.type) {
    case 'bridge_event':
      return reduceBridgeEvent(state, action.event);
    case 'bridge_events':
      return action.events.reduce((acc, event) => reduceBridgeEvent(acc, event), state);
    case 'set_config_stage':
      return {...state, configStage: action.stage};
    case 'bridge_exit': {
      if (state.step === 'running' || state.step === 'review') {
        const outcome: AppState['outcome'] =
          action.code === 0
            ? 'complete'
            : action.code === 130 || action.signal === 'SIGTERM' || action.signal === 'SIGINT'
              ? 'cancelled'
              : 'error';
        return {
          ...state,
          step: 'output',
          outcome,
          lastError: outcome === 'error' ? state.lastError ?? `Bridge exited with code ${action.code ?? 'unknown'}` : state.lastError,
          commandMode: false,
          cancelPending: false,
          gscGate: undefined,
          progressRows: outcome === 'complete' ? settleRunningRows(state.progressRows, state.planPhases.length > 0) : state.progressRows,
          completedAt: Date.now(),
        };
      }
      return {...state, cancelPending: false, gscGate: undefined, completedAt: Date.now()};
    }
    case 'set_input':
      return {...state, inputValue: action.value};
    case 'submit_input':
      if (state.commandMode === 'comment' && state.inputValue.trim()) {
        return {
          ...state,
          queuedComments: [...state.queuedComments, state.inputValue.trim()],
          inputValue: '',
          commandMode: false,
        };
      }
      if (state.commandMode === 'command') {
        return applyCommand(state, state.inputValue);
      }
      return {...state, inputValue: '', commandMode: false};
    case 'update_config': {
      // A new keyword invalidates the interpretation lifecycle (fresh take counter)
      // and any AI keyword ideas (they were suggested for the old keyword).
      const keywordChanged = action.patch.keyword !== undefined && action.patch.keyword !== state.config.keyword;
      const nextConfig = {
        ...state.config,
        ...action.patch,
        ...(keywordChanged && action.patch.keywordContext === undefined ? {keywordContext: ''} : {}),
      };
      return {
        ...state,
        config: nextConfig,
        ...(keywordChanged
          ? {
              interpretState: 'idle' as const,
              interpretAttempt: 0,
              interpretFeedback: undefined,
              interpretPrevious: undefined,
              suggestState: 'idle' as const,
              suggestAttempt: 0,
              keywordSuggestions: undefined,
            }
          : {}),
      };
    }
    case 'start_run':
      return {...state, step: 'running', runId: action.runId, startedAt: Date.now()};
    case 'request_run':
      if (!state.config.keyword.trim()) {
        return {...state, ...logPatch(state, 'warn', 'Set a keyword before starting: keyword <topic>')};
      }
      if (state.setupStatus && !state.setupStatus.canGenerate) {
        const message = `Setup is missing required values: ${state.setupStatus.requiredMissing.join(', ')}`;
        return {...state, ...logPatch(state, 'warn', message), ...notifyPatch(state, 'warning', message)};
      }
      return {...state, step: 'running', runRequestId: state.runRequestId + 1};
    case 'request_setup':
      return {
        ...state,
        returnStep: state.step === 'setup' ? state.returnStep : state.step,
        step: 'setup',
        setupRequestId: state.setupRequestId + 1,
      };
    // Entry to the context step from "Start generation": interpretation auto-runs
    // only when no confirmed context exists yet; otherwise the last take is shown.
    case 'request_context': {
      if (!state.config.keyword.trim()) {
        return {...state, ...logPatch(state, 'warn', 'Set a keyword before starting: keyword <topic>')};
      }
      if (state.setupStatus && !state.setupStatus.canGenerate) {
        const message = `Setup is missing required values: ${state.setupStatus.requiredMissing.join(', ')}`;
        return {...state, ...logPatch(state, 'warn', message), ...notifyPatch(state, 'warning', message)};
      }
      if (!state.config.keywordContext.trim()) {
        return {...state, step: 'context', ...startInterpretation(state)};
      }
      return {...state, step: 'context'};
    }
    case 'request_interpret':
      if (!state.config.keyword.trim()) {
        return {...state, ...logPatch(state, 'warn', 'Set a keyword before interpreting: keyword <topic>')};
      }
      return {...state, step: 'context', ...startInterpretation(state, action.feedback)};
    case 'interpret_finished':
      // Only settles an in-flight interpretation; artifact/error events take precedence.
      if (state.interpretState !== 'running') return state;
      return {...state, interpretState: action.ok ? 'done' : 'failed'};
    case 'request_suggest':
      return {...state, configStage: 'keyword-ideas', ...startSuggestion(state, action.steer)};
    case 'suggest_finished':
      // Only settles an in-flight suggestion run; artifact/error events take precedence.
      if (state.suggestState !== 'running') return state;
      return {...state, suggestState: action.ok ? 'done' : 'failed'};
    case 'toggle_suggest_trends':
      return {...state, suggestTrendsFresh: !state.suggestTrendsFresh};
    case 'toggle_topic_mode':
      return {...state, topicMode: state.topicMode === 'angle' ? 'keyword' : 'angle'};
    case 'pick_suggestion': {
      // Route the keyword through the same reset semantics update_config uses (the
      // interpret-lifecycle + keyword-ideas reset), then land on the style stage.
      const withKeyword = reducer(state, {type: 'update_config', patch: {keyword: action.keyword}});
      return {...withKeyword, configStage: 'style'};
    }
    case 'setup_save_complete':
      return {...state, setupDraft: {}, setupConfirmPending: false};
    case 'log':
      // Bridge stderr is routine Python logging — record it, never toast it.
      return {...state, ...logPatch(state, action.level, action.text)};
    case 'notify':
      return {
        ...state,
        ...notifyPatch(state, action.tone, action.message),
        ...logPatch(state, action.tone === 'error' ? 'error' : action.tone === 'warning' ? 'warn' : 'info', action.message),
      };
    case 'toggle_help':
      return {...state, helpOpen: !state.helpOpen, logOpen: false};
    case 'toggle_log':
      return {...state, logOpen: !state.logOpen, helpOpen: false};
    case 'escape':
      // Overlay-closing escape only; step-level back/cancel decisions live in the keymap.
      if (state.helpOpen || state.logOpen) return {...state, helpOpen: false, logOpen: false};
      if (state.cancelPending) return {...state, cancelPending: false};
      if (state.publishArmed) return {...state, publishArmed: false};
      if (state.commandMode === 'comment' || state.commandMode === 'command') return {...state, commandMode: false, inputValue: ''};
      return state;
    case 'open_comment':
      if (state.gscGate || state.cancelPending) return state;
      return {...state, commandMode: 'comment', inputValue: ''};
    case 'comment_submitted': {
      const text = action.text.trim();
      return {
        ...state,
        queuedComments: text ? [...state.queuedComments, text] : state.queuedComments,
        commandMode: false,
        inputValue: '',
      };
    }
    case 'request_save': {
      if (state.saveState === 'running' || state.publishState === 'running') return state;
      if (!hasFinalArtifact(state)) {
        return {...state, ...notifyPatch(state, 'warning', 'No final artifact is available to save yet.')};
      }
      return {...state, saveState: 'running', saveRequestId: state.saveRequestId + 1};
    }
    case 'request_publish': {
      if (state.publishState === 'running' || state.saveState === 'running') return state;
      if (!hasFinalArtifact(state)) {
        return {...state, ...notifyPatch(state, 'warning', 'No final artifact is available to publish yet.')};
      }
      return {...state, publishArmed: false, publishState: 'running', publishRequestId: state.publishRequestId + 1};
    }
    case 'action_finished': {
      const status: AppState['saveState'] = action.ok ? 'done' : 'failed';
      const statusPatch = action.kind === 'save' ? {saveState: status} : {publishState: status};
      if (action.ok) return {...state, ...statusPatch, artifactConflict: undefined};
      return {...state, ...statusPatch, ...notifyPatch(state, 'error', action.message ?? `${action.kind === 'save' ? 'Save' : 'Publish'} failed.`)};
    }
    case 'request_cancel':
      return state.step === 'running' || state.step === 'review' ? {...state, cancelPending: true} : state;
    case 'dismiss_cancel':
      return {...state, cancelPending: false};
    case 'arm_publish':
      return hasFinalArtifact(state) ? {...state, publishArmed: true} : state;
    case 'goto_output':
      if (state.step !== 'preview') return state;
      return {...state, step: 'output', outcome: state.outcome ?? 'complete'};
    case 'goto_preview':
      if (state.step !== 'output' || !hasFinalArtifact(state)) return state;
      return {...state, step: 'preview', publishArmed: false};
    case 'new_run':
      if (state.saveState === 'running' || state.publishState === 'running') {
        return {...state, ...notifyPatch(state, 'warning', 'Wait for the current save or publish action to finish.')};
      }
      return resetForNewRun(state);
    case 'set_step':
      return {...state, step: action.step};
    default:
      return state;
  }
};
