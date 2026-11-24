export type BridgeEventType =
  | 'setup_status'
  | 'run_plan'
  | 'phase_started'
  | 'task_progress'
  | 'tool_call'
  | 'warning'
  | 'error'
  | 'artifact_ready'
  | 'artifact_conflict'
  | 'review_gate_opened'
  | 'review_gate_closed'
  | 'gsc_gate_opened'
  | 'gsc_gate_closed'
  | 'generation_complete'
  | 'saved'
  | 'published';

export interface BridgeEvent {
  type: BridgeEventType;
  runId: string;
  timestamp: string;
  phase?: string;
  agent?: string;
  taskId?: string;
  message?: string;
  data?: unknown;
}

export type BridgeCommand =
  | {type: 'queue_comment'; runId: string; comment: string}
  | {
      type: 'review_decision';
      runId: string;
      decision: 'approve' | 'apply_comments' | 'add_final_comment' | 'rerun_comment_edit' | 'cancel';
      comment?: string;
    }
  | {type: 'gsc_decision'; runId: string; decision: 'proceed' | 'abort'}
  | {type: 'cancel'; runId: string};

// One linear journey: Configure › Context › Generate (running) › Review › Preview › Output.
// 'setup' is a side-trip for environment checks; Esc returns to `returnStep`.
export type JourneyStep = 'setup' | 'config' | 'context' | 'running' | 'review' | 'preview' | 'output';

export type RunOutcome = 'complete' | 'error' | 'cancelled';

export type ActionStatus = 'idle' | 'running' | 'done' | 'failed';

export interface LogEntry {
  id: number;
  level: 'info' | 'tool' | 'warn' | 'error';
  text: string;
}

export interface ProgressRow {
  id: string;
  label: string;
  phase: string;
  status: 'pending' | 'running' | 'complete' | 'warning' | 'error' | 'skipped';
  detail?: string;
  agent?: string;
  // Mid-task n/m progress (mining query loops) with optional preview fields.
  progressCurrent?: number;
  progressTotal?: number;
  insights?: number;
  query?: string;
  // Seconds, stamped on terminal transitions by the engine reporter.
  elapsed?: number;
}

export interface PlanPhase {
  phase: string;
  label: string;
  tasks: Array<{id: string; label: string}>;
}

export interface Artifact {
  id: string;
  phase?: string;
  message?: string;
  data?: unknown;
}

export interface GenerationConfig {
  keyword: string;
  style: 'standard' | 'guide' | 'comparison' | 'research' | 'news' | 'category' | 'top-compare' | 'feature';
  depth: 'quick' | 'standard' | 'comprehensive';
  communityMode: 'full' | 'limited' | 'skip';
  flyweelMode: 'full' | 'limited' | 'none';
  icpEnabled: boolean;
  gscCheck: boolean;
  gscKeywords: boolean;
  solutionCount: 5 | 8 | 10 | 15;
  competitors: string;
  // Whether the article may name/compare competitors. Forced on (flag ignored by the
  // engine) for comparison/top-compare styles, where competitor coverage is the point.
  competitorMentions: boolean;
  note: string;
  keywordContext: string;
  saveDefault: 'preview' | 'local' | 'publish';
}

export interface SetupCheck {
  id: string;
  label: string;
  required: boolean;
  ok: boolean;
  detail: string;
}

export interface AgentRoleStatus {
  role: string;
  label: string;
  keyEnv: string;
  modelEnv: string;
  configured: boolean;
  provider: string;
  model: string;
  sourceEnv: string;
}

export interface SiteTargetStatus {
  mode: 'handoff' | 'repo';
  framework: string;
  frameworkLabel: string;
  contentDir: string;
  contentDirOverrideEnv?: string | null;
  existingPostCount: number;
}

export interface SetupStatus {
  checks: SetupCheck[];
  canGenerate: boolean;
  requiredMissing: string[];
  optionalMissing: string[];
  repoRoot?: string;
  onboardingComplete?: boolean;
  brandConfigured?: boolean;
  brandName?: string;
  brandSiteUrl?: string;
  brandCategory?: string;
  productMarketingFile?: string | null;
  siteTarget?: SiteTargetStatus;
  agentRoles?: AgentRoleStatus[];
}

export interface Notification {
  id: string;
  tone: 'info' | 'success' | 'warning' | 'error';
  message: string;
}

export type ConfigStage = 'keyword' | 'style' | 'depth' | 'settings' | 'keyword-ideas';

// One AI-suggested keyword idea from the `suggest-keywords` bridge subcommand.
// `score` is null when GSC has no signal at all for the idea; `estimated` marks a
// score inferred from semantically related queries rather than direct GSC data.
export interface KeywordSuggestion {
  keyword: string;
  score: number | null;
  grounded: boolean;
  estimated: boolean;
  trafficPotential: number | null;
  rationale?: string;
  // Angle-driven framing of the idea (present when the bridge ran with --mode angle);
  // null/absent for keyword-driven suggestions.
  angle?: string | null;
}

export interface AppState {
  step: JourneyStep;
  returnStep: JourneyStep;
  outcome?: RunOutcome;
  lastError?: string;
  configStage: ConfigStage;
  runId?: string;
  activePhase?: string;
  activeAgent?: string;
  helpOpen: boolean;
  logOpen: boolean;
  cancelPending: boolean;
  publishArmed: boolean;
  // Low-GSC-score gate: set by gsc_gate_opened, cleared by gsc_gate_closed. The
  // running view renders a y/n confirm strip while set; the decision goes back to
  // the bridge as a gsc_decision command. `estimated`/`neighborCount` are additive
  // bridge fields (score inferred from semantically related queries when the keyword
  // has no direct GSC data) — old bridges omit them, so both stay optional.
  gscGate?: {
    score: number;
    threshold: number;
    trafficPotential?: number;
    primaryQuery?: string;
    estimated?: boolean;
    neighborCount?: number;
  };
  commandMode: false | 'comment' | 'command' | 'review';
  inputValue: string;
  config: GenerationConfig;
  setupStatus?: SetupStatus;
  setupDraft: Record<string, string>;
  setupConfirmPending: boolean;
  // Phase order + labels from the bridge's run_plan event; empty until seeded.
  planPhases: Array<{phase: string; label: string}>;
  progressRows: ProgressRow[];
  artifacts: Artifact[];
  queuedComments: string[];
  notifications: Notification[];
  noticeSeq: number;
  log: LogEntry[];
  logSeq: number;
  saveState: ActionStatus;
  publishState: ActionStatus;
  artifactConflict?: {action: 'save' | 'publish'; slug: string};
  outputPaths: Record<string, string>;
  qualityMetrics: Record<string, unknown>;
  generationSummary: Record<string, unknown>;
  startedAt?: number;
  completedAt?: number;
  // Topic-interpretation lifecycle for the context step. Feedback/previous are staged
  // by 'request_interpret' and consumed by App's interpret effect on the next spawn.
  interpretState: ActionStatus;
  interpretAttempt: number;
  interpretFeedback?: string;
  interpretPrevious?: string;
  runRequestId: number;
  setupRequestId: number;
  setupSaveRequestId: number;
  interpretRequestId: number;
  saveRequestId: number;
  publishRequestId: number;
  // Keyword-ideas lifecycle (the ^G alternative on the keyword stage). Steer/previous
  // are staged by 'request_suggest' and consumed by App's suggest effect on the next
  // spawn; `suggestTrendsFresh` is a standing preference, not reset between runs.
  suggestState: ActionStatus;
  suggestAttempt: number;
  suggestSteer?: string;
  suggestPrevious?: string[];
  suggestRequestId: number;
  suggestTrendsFresh: boolean;
  keywordSuggestions?: KeywordSuggestion[];
  // Whether the ideation bridge run had GSC signal available (additive field from the
  // keyword_suggestions artifact); undefined until a run reports it. A suggest-lifecycle
  // field — may reset with the others across runs.
  suggestGscAvailable?: boolean;
  // Standing UI preference framing the topic input: 'angle' feeds the ideation agent an
  // angle to explore, 'keyword' a target keyword. Not part of GenerationConfig — the
  // picked/typed string still lands in config.keyword; this only changes framing + the
  // ideation bridge's --mode. Persists across runs (like suggestTrendsFresh).
  topicMode: 'keyword' | 'angle';
}

export type AppAction =
  | {type: 'bridge_event'; event: BridgeEvent}
  | {type: 'bridge_events'; events: BridgeEvent[]}
  | {type: 'bridge_exit'; code: number | null; signal?: NodeJS.Signals | null}
  | {type: 'set_config_stage'; stage: ConfigStage}
  | {type: 'set_input'; value: string}
  | {type: 'submit_input'}
  | {type: 'update_config'; patch: Partial<GenerationConfig>}
  | {type: 'start_run'; runId: string}
  | {type: 'request_run'}
  | {type: 'request_context'}
  | {type: 'request_setup'}
  | {type: 'request_interpret'; feedback?: string}
  | {type: 'interpret_finished'; ok: boolean}
  | {type: 'setup_save_complete'}
  | {type: 'log'; level: LogEntry['level']; text: string}
  | {type: 'notify'; tone: Notification['tone']; message: string}
  | {type: 'toggle_help'}
  | {type: 'toggle_log'}
  | {type: 'escape'}
  | {type: 'open_comment'}
  | {type: 'comment_submitted'; text: string}
  | {type: 'request_save'}
  | {type: 'request_publish'}
  | {type: 'action_finished'; kind: 'save' | 'publish'; ok: boolean; message?: string}
  | {type: 'request_cancel'}
  | {type: 'dismiss_cancel'}
  | {type: 'arm_publish'}
  | {type: 'goto_output'}
  | {type: 'goto_preview'}
  | {type: 'new_run'}
  | {type: 'set_step'; step: JourneyStep}
  | {type: 'request_suggest'; steer?: string}
  | {type: 'suggest_finished'; ok: boolean}
  | {type: 'toggle_suggest_trends'}
  | {type: 'toggle_topic_mode'}
  | {type: 'pick_suggestion'; keyword: string};
