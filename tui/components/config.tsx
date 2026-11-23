import React, {useState} from 'react';
import {Box, Text, useInput} from 'ink';
import {TextInput} from './textInput.js';

import {useLayout} from '../lib/useTerminalSize.js';
import {KeywordIdeasView} from './keywordIdeasView.js';
import {OptionList} from './termcn.js';
import {useTheme} from './ui/theme-provider.js';
import type {AppAction, AppState, ConfigStage, GenerationConfig} from '../types.js';

// Option lists (value + human label). Values mirror the GenerationConfig unions in types.ts.
const STYLE_OPTIONS = [
  {value: 'standard', label: 'Standard — comprehensive overview'},
  {value: 'guide', label: 'Guide — step-by-step tutorial'},
  {value: 'comparison', label: 'Comparison — head-to-head review'},
  {value: 'top-compare', label: 'Top X — ranked "best of" listicle'},
  {value: 'research', label: 'Research — data-driven analysis'},
  {value: 'news', label: 'News — trending coverage'},
  {value: 'category', label: 'Category — overview listicle'},
  {value: 'feature', label: 'Feature — conversion product page'},
];

const DEPTH_OPTIONS = [
  {value: 'quick', label: 'Quick — SERP + 3 Reddit + 1 Quora, no topical insights'},
  {value: 'standard', label: 'Standard — balanced'},
  {value: 'comprehensive', label: 'Comprehensive — deepest research'},
];

const COMMUNITY_OPTIONS = [
  {value: 'full', label: 'full — Reddit + Quora mining'},
  {value: 'limited', label: 'limited — 3 Reddit + 1 Quora'},
  {value: 'skip', label: 'skip — no community mining'},
];

const FLYWEEL_OPTIONS = [
  {value: 'full', label: 'full — brand mentions + links'},
  {value: 'limited', label: 'limited — sparing mentions'},
  {value: 'none', label: 'none — educational, no brand'},
];

const OUTPUT_OPTIONS = [
  {value: 'preview', label: 'preview — review before saving'},
  {value: 'local', label: 'local — save post + agent-handoff.md to output/'},
];

const SOLUTION_OPTIONS = [
  {value: '5', label: '5'},
  {value: '8', label: '8'},
  {value: '10', label: '10'},
  {value: '15', label: '15'},
];

const BOOL_OPTIONS = [
  {value: 'on', label: 'on'},
  {value: 'off', label: 'off'},
];

const STAGES: ConfigStage[] = ['keyword', 'style', 'depth', 'settings'];
// 'keyword-ideas' is a side-trip off 'keyword', not a linear stage — it's never in
// STAGES and Breadcrumb is hidden while active, but the label keeps the map total.
const STAGE_LABEL: Record<ConfigStage, string> = {keyword: 'Keyword', style: 'Style', depth: 'Depth', settings: 'Settings', 'keyword-ideas': 'Keyword Ideas'};

type FieldKind = 'enum' | 'bool' | 'text';
interface FieldRow {
  key: keyof GenerationConfig;
  label: string;
  kind: FieldKind;
  options?: Array<{value: string; label: string}>;
}

const FIELD_ROWS: FieldRow[] = [
  {key: 'communityMode', label: 'Community mining', kind: 'enum', options: COMMUNITY_OPTIONS},
  {key: 'flyweelMode', label: 'Brand mentions', kind: 'enum', options: FLYWEEL_OPTIONS},
  {key: 'icpEnabled', label: 'ICP targeting', kind: 'bool'},
  {key: 'gscCheck', label: 'GSC cannibalization', kind: 'bool'},
  {key: 'gscKeywords', label: 'GSC keyword map', kind: 'bool'},
  {key: 'solutionCount', label: 'Solutions (compare)', kind: 'enum', options: SOLUTION_OPTIONS},
  {key: 'competitors', label: 'Competitors', kind: 'text'},
  {key: 'competitorMentions', label: 'Competitor mentions', kind: 'bool'},
  {key: 'note', label: 'Editor note', kind: 'text'},
  {key: 'keywordContext', label: 'Topic context', kind: 'text'},
  {key: 'saveDefault', label: 'Default output', kind: 'enum', options: OUTPUT_OPTIONS},
];

// Put the current value first so it is pre-highlighted (OptionList focuses index 0); Enter
// fires onSelect even when the user accepts the already-selected option.
const withCurrentFirst = (options: Array<{value: string; label: string}>, current: string): Array<{value: string; label: string}> => {
  const match = options.find(option => option.value === current);
  if (!match) return options;
  return [match, ...options.filter(option => option.value !== current)];
};

// Comparison-family styles are competitor coverage by definition; the engine ignores
// --no-competitors for them, so the row locks to "forced on" and is not editable.
const competitorsForced = (style: GenerationConfig['style']): boolean => style === 'comparison' || style === 'top-compare';

const displayValue = (config: GenerationConfig, row: FieldRow): string => {
  const value = config[row.key];
  if (row.key === 'competitorMentions' && competitorsForced(config.style)) return 'forced on';
  if (row.kind === 'bool') return value ? 'on' : 'off';
  if (row.key === 'competitors') return (value as string) || 'auto-discover';
  if (row.key === 'keywordContext') return (value as string) || 'not confirmed';
  if (row.key === 'note') return (value as string) || 'none';
  return String(value);
};

const Breadcrumb = ({stage}: {stage: ConfigStage}) => {
  const theme = useTheme();
  return (
    <Box marginBottom={1}>
      {STAGES.map((current, index) => (
        <React.Fragment key={current}>
          {index > 0 ? <Text color={theme.colors.mutedForeground}> {'›'} </Text> : null}
          <Text bold={current === stage} color={current === stage ? theme.colors.primary : theme.colors.mutedForeground}>
            {STAGE_LABEL[current]}
          </Text>
        </React.Fragment>
      ))}
    </Box>
  );
};

const Hint = ({children}: {children: React.ReactNode}) => {
  const theme = useTheme();
  return (
    <Box marginTop={1}>
      <Text color={theme.colors.mutedForeground}>{children}</Text>
    </Box>
  );
};

// Single-height chip row under the title: which framing feeds the ideation agent and
// the topic input. Active chip is bracketed + bold in the primary color, inactive dim.
// Always rendered (fixed height) on every config stage except keyword-ideas.
const TopicModeChips = ({mode}: {mode: AppState['topicMode']}) => {
  const theme = useTheme();
  const chip = (active: boolean, label: string) =>
    active ? (
      <Text bold color={theme.colors.primary}>
        {`▸ ${label} ◂`}
      </Text>
    ) : (
      <Text color={theme.colors.mutedForeground}>{`  ${label}  `}</Text>
    );
  return (
    <Box marginBottom={1}>
      {chip(mode === 'angle', 'Angle-driven')}
      <Text color={theme.colors.mutedForeground}>{'   '}</Text>
      {chip(mode === 'keyword', 'Keyword-driven')}
      <Text color={theme.colors.mutedForeground}>{'   (Tab to switch)'}</Text>
    </Box>
  );
};

// Single-height dim indicator, left-aligned inside the panel below the stage content:
// the current fast/fresh trends preference and its Ctrl+T toggle. Fixed height.
const TrendsIndicator = ({fresh}: {fresh: boolean}) => {
  const theme = useTheme();
  return (
    <Box marginTop={1}>
      <Text color={theme.colors.mutedForeground}>{`Trends: ${fresh ? 'fresh' : 'fast'} (Ctrl+T)`}</Text>
    </Box>
  );
};

const FieldEditor = ({row, config, dispatch, done}: {row: FieldRow; config: GenerationConfig; dispatch: React.Dispatch<AppAction>; done: () => void}) => {
  const commit = (patch: Partial<GenerationConfig>) => {
    dispatch({type: 'update_config', patch});
    done();
  };
  return (
    <Box flexDirection="column">
      <Text>Edit {row.label}:</Text>
      <Box marginTop={1}>
        {row.kind === 'enum' ? (
          <OptionList
            key={`edit-${row.key}`}
            options={withCurrentFirst(row.options ?? [], String(config[row.key]))}
            viewport={(row.options ?? []).length}
            onSelect={value => commit({[row.key]: row.key === 'solutionCount' ? Number(value) : value} as Partial<GenerationConfig>)}
          />
        ) : null}
        {row.kind === 'bool' ? (
          <OptionList
            key={`edit-${row.key}`}
            options={withCurrentFirst(BOOL_OPTIONS, config[row.key] ? 'on' : 'off')}
            onSelect={value => commit({[row.key]: value === 'on'} as Partial<GenerationConfig>)}
          />
        ) : null}
        {row.kind === 'text' ? (
          <TextInput
            key={`edit-${row.key}`}
            defaultValue={String(config[row.key] ?? '')}
            placeholder="type a value…"
            onSubmit={value => commit({[row.key]: value.trim()} as Partial<GenerationConfig>)}
          />
        ) : null}
      </Box>
      <Hint>Enter commit · Esc cancel</Hint>
    </Box>
  );
};

const SettingsList = ({
  state,
  dispatch,
  editing,
  setEditing,
  onStart,
}: {
  state: AppState;
  dispatch: React.Dispatch<AppAction>;
  editing: FieldRow | null;
  setEditing: (row: FieldRow | null) => void;
  onStart: () => void;
}) => {
  const theme = useTheme();
  const {config} = state;
  const {bodyRows} = useLayout();
  // Remember where the highlight was so it survives a field edit instead of
  // snapping back to the top of the list after every change.
  const [lastIndex, setLastIndex] = useState(0);

  const ready = Boolean(config.keyword.trim()) && (!state.setupStatus || state.setupStatus.canGenerate);
  const options = [
    ...FIELD_ROWS.map(row => ({value: row.key as string, label: `${row.label.padEnd(20)} ${displayValue(config, row)}`})),
    {value: '__interpret', label: 'Interpret topic context (AI)'},
    {value: '__start', label: '▶ Start generation'},
  ];

  if (editing) {
    return <FieldEditor row={editing} config={config} dispatch={dispatch} done={() => setEditing(null)} />;
  }

  const onSelect = (value: string, index: number) => {
    setLastIndex(index);
    if (value === '__start') return onStart();
    if (value === '__interpret') return dispatch({type: 'request_interpret'});
    const row = FIELD_ROWS.find(candidate => (candidate.key as string) === value);
    if (row?.key === 'competitorMentions' && competitorsForced(config.style)) return;
    if (row) setEditing(row);
  };

  const readiness = ready
    ? `[OK] Ready — ${config.keyword}`
    : config.keyword.trim()
      ? `[!] Setup incomplete${state.setupStatus ? `: ${state.setupStatus.requiredMissing.join(', ')}` : ' — checking environment…'}`
      : '[!] No keyword set';

  // Chrome around the list: title (1) + chip row (2) + breadcrumb (2) + readiness
  // block (3, incl. its marginTop) + trends indicator (2, incl. its marginTop) = 10.
  const listViewport = Math.max(3, bodyRows - 10);

  return (
    <Box flexDirection="column">
      <OptionList options={options} initialIndex={lastIndex} onSelect={onSelect} viewport={listViewport} />
      <Box marginTop={1} flexDirection="column">
        <Text color={ready ? theme.colors.success : theme.colors.warning}>{readiness}</Text>
        <Text color={theme.colors.mutedForeground}>↑↓ move · Enter edit · s start · Ctrl+G AI ideas · Esc back</Text>
      </Box>
    </Box>
  );
};

// Self-contained guided config flow. It owns its own Esc/back + start-shortcut handling
// because App's global nav handler is disabled while config widgets are focused — so this
// is the single keyboard owner for the config stage and nothing competes for keystrokes.
export const ConfigFlow = ({state, dispatch, onExit}: {state: AppState; dispatch: React.Dispatch<AppAction>; onExit: () => void}) => {
  const theme = useTheme();
  const {config, configStage} = state;
  const {bodyRows} = useLayout();
  const [editing, setEditing] = useState<FieldRow | null>(null);
  const [keywordDraft, setKeywordDraft] = useState<string>(config.keyword);
  React.useEffect(() => {
    setKeywordDraft(config.keyword);
  }, [config.keyword]);
  // Chrome around a stage's option list: title (1) + chip row (2) + breadcrumb (2) +
  // question (1) + select marginTop (1) + hint (2) + trends indicator (2) = 11.
  const selectViewport = Math.max(3, bodyRows - 11);

  const handleStart = () => {
    if (!config.keyword.trim()) return dispatch({type: 'set_config_stage', stage: 'keyword'});
    if (state.setupStatus && !state.setupStatus.canGenerate) return dispatch({type: 'request_setup'});
    // The run starts from the context step (Enter there), never straight from here.
    dispatch({type: 'request_context'});
  };

  const advance = (stage: ConfigStage) => dispatch({type: 'set_config_stage', stage});

  useInput((input, key) => {
    // The keyword-ideas side-trip owns all of its own keys (it's not in STAGES, so
    // the index-based Esc below would otherwise fall through to onExit()/quit).
    if (configStage === 'keyword-ideas') return;
    if (key.escape) {
      if (editing) {
        setEditing(null);
        return;
      }
      const index = STAGES.indexOf(configStage);
      if (index <= 0) return onExit();
      advance(STAGES[index - 1]);
      return;
    }
    // Ctrl+G anywhere on the config step (the side-trip itself returned above): ask the
    // bridge for 5 AI keyword ideas instead of typing one. Not gated to the keyword
    // stage — a prefilled keyword lands on the settings list, and it must work there too.
    if (!editing && key.ctrl && input === 'g') {
      if (configStage === 'keyword') {
        const trimmedDraft = keywordDraft.trim();
        if (trimmedDraft !== config.keyword) {
          dispatch({type: 'update_config', patch: {keyword: trimmedDraft}});
        }
      }
      dispatch({type: 'request_suggest'});
      return;
    }
    // Tab flips angle-driven ↔ keyword-driven framing (the chip row under the title).
    // The !editing guard keeps it out of field editors; the local TextInput (chord-safe,
    // ./textInput.tsx) ignores Tab and all ctrl chords, so this owns them everywhere.
    if (!editing && key.tab) {
      dispatch({type: 'toggle_topic_mode'});
      return;
    }
    // Ctrl+T flips the fast/fresh trends preference from the config page (the ideas
    // side-trip owns its own Ctrl+T above the early-return).
    if (!editing && key.ctrl && input === 't') {
      dispatch({type: 'toggle_suggest_trends'});
      return;
    }
    // Ctrl+S opens the Setup & Brand Context Wizard from any config stage.
    if (!editing && key.ctrl && input === 's') {
      dispatch({type: 'set_step', step: 'setup'});
      return;
    }
    // Start shortcut only on the settings stage and only when not typing into a field.
    if (configStage === 'settings' && !editing && (input === 's' || input === 'S')) {
      handleStart();
    }
  });

  const isSideTrip = configStage === 'keyword-ideas';

  return (
    <Box flexDirection="column" paddingX={1}>
      <Text color={theme.colors.primary} bold>
        Generation Config
      </Text>
      {!isSideTrip ? <TopicModeChips mode={state.topicMode} /> : null}
      {!isSideTrip ? <Breadcrumb stage={configStage} /> : null}

      {configStage === 'keyword' ? (
        <Box flexDirection="column">
          <Text>
            {state.topicMode === 'angle'
              ? 'What angle / topic should this article take?'
              : 'What keyword / topic should this article target?'}
          </Text>
          <Box marginTop={1}>
            <TextInput
              key="kw"
              defaultValue={config.keyword}
              placeholder="e.g. closed loop attribution"
              onChange={setKeywordDraft}
              onSubmit={value => {
                const keyword = value.trim();
                if (!keyword) return;
                dispatch({type: 'update_config', patch: {keyword}});
                advance('style');
              }}
            />
          </Box>
          <Hint>Enter to continue · Ctrl+G AI ideas · Ctrl+S brand/setup · Esc to quit</Hint>
        </Box>
      ) : null}

      {configStage === 'keyword-ideas' ? <KeywordIdeasView state={state} dispatch={dispatch} /> : null}

      {configStage === 'style' ? (
        <Box flexDirection="column">
          <Text>Pick a content style:</Text>
          <Box marginTop={1}>
            <OptionList
              key="style"
              options={withCurrentFirst(STYLE_OPTIONS, config.style)}
              viewport={Math.min(STYLE_OPTIONS.length, selectViewport)}
              onSelect={value => {
                dispatch({type: 'update_config', patch: {style: value as GenerationConfig['style']}});
                advance('depth');
              }}
            />
          </Box>
          <Hint>↑↓ move · Enter select · Esc back</Hint>
        </Box>
      ) : null}

      {configStage === 'depth' ? (
        <Box flexDirection="column">
          <Text>How deep should the research go?</Text>
          <Box marginTop={1}>
            <OptionList
              key="depth"
              options={withCurrentFirst(DEPTH_OPTIONS, config.depth)}
              viewport={Math.min(DEPTH_OPTIONS.length, selectViewport)}
              onSelect={value => {
                dispatch({type: 'update_config', patch: {depth: value as GenerationConfig['depth']}});
                advance('settings');
              }}
            />
          </Box>
          <Hint>↑↓ move · Enter select · Esc back</Hint>
        </Box>
      ) : null}

      {configStage === 'settings' ? (
        <SettingsList state={state} dispatch={dispatch} editing={editing} setEditing={setEditing} onStart={handleStart} />
      ) : null}

      {!isSideTrip ? <TrendsIndicator fresh={state.suggestTrendsFresh} /> : null}
    </Box>
  );
};
