import React from 'react';
import {Box, Text} from 'ink';

import {KEYMAP, bindingsFor} from '../keymap.js';
import {useLayout} from '../lib/useTerminalSize.js';
import {StatusMessage} from './termcn.js';
import {useTheme} from './ui/theme-provider.js';
import type {AppState, JourneyStep, LogEntry} from '../types.js';

const JOURNEY: Array<{step: JourneyStep; label: string}> = [
  {step: 'config', label: 'Configure'},
  {step: 'context', label: 'Context'},
  {step: 'running', label: 'Generate'},
  {step: 'review', label: 'Review'},
  {step: 'preview', label: 'Preview'},
  {step: 'output', label: 'Output'},
];

const OUTCOME_BADGE: Record<NonNullable<AppState['outcome']>, {glyph: string; label: string; tone: 'success' | 'error' | 'warning'}> = {
  complete: {glyph: '✓', label: 'complete', tone: 'success'},
  error: {glyph: '✗', label: 'error', tone: 'error'},
  cancelled: {glyph: '—', label: 'cancelled', tone: 'warning'},
};

export const ANSI_LOGO_LINES = [
  "\u001b[49m     \u001b[38;2;209;254;230;49m▄\u001b[38;2;203;254;227;49m▄▄\u001b[38;2;165;204;183;48;2;207;255;229m▄\u001b[49;38;2;204;254;227m▀\u001b[49;38;2;203;254;227m▀\u001b[49;38;2;204;254;227m▀\u001b[38;2;163;202;184;48;2;207;254;230m▄\u001b[38;2;203;254;227;49m▄▄\u001b[38;2;208;254;230;49m▄\u001b[49m                                                 \u001b[m",
  "\u001b[49m    \u001b[38;2;164;203;183;48;2;203;253;227m▄\u001b[38;2;162;204;183;48;2;204;254;227m▄\u001b[38;2;164;203;181;48;2;204;254;227m▄\u001b[38;2;196;244;218;48;2;208;254;230m▄\u001b[38;2;204;253;227;48;2;208;253;229m▄\u001b[38;2;164;202;181;48;2;208;254;230m▄\u001b[38;2;168;203;185;49m▄\u001b[38;2;214;254;231;49m▄\u001b[49m   \u001b[49;38;2;175;217;195m▀\u001b[49;38;2;165;203;183m▀\u001b[49m          \u001b[38;2;204;252;227;49m▄▄▄▄\u001b[49m  \u001b[38;2;204;252;227;49m▄▄\u001b[49m                              \u001b[m",
  "\u001b[49m \u001b[38;2;207;253;228;49m▄\u001b[38;2;203;250;225;48;2;214;254;234m▄\u001b[49m     \u001b[49;38;2;163;202;182m▀\u001b[49m  \u001b[49;38;2;164;202;182m▀\u001b[49;38;2;159;207;175m▀\u001b[49m    \u001b[38;2;205;252;227;49m▄\u001b[38;2;204;252;227;48;2;204;253;227m▄\u001b[38;2;206;252;228;49m▄\u001b[49m     \u001b[38;2;204;252;227;49m▄▄\u001b[49;38;2;204;252;227m▀▀▀▀\u001b[49m  \u001b[48;2;204;252;227m  \u001b[49m                              \u001b[m",
  "\u001b[38;2;210;251;229;49m▄\u001b[38;2;205;251;227;48;2;205;252;227m▄\u001b[49m             \u001b[38;2;207;251;228;49m▄\u001b[38;2;205;251;227;49m▄\u001b[38;2;205;251;227;48;2;205;252;227m▄\u001b[49m \u001b[38;2;204;250;226;48;2;205;252;227m▄\u001b[38;2;209;251;229;49m▄\u001b[49m  \u001b[38;2;204;252;227;49m▄▄\u001b[48;2;204;252;227m  \u001b[38;2;204;252;227;49m▄▄▄▄\u001b[49m  \u001b[48;2;204;252;227m  \u001b[49m       \u001b[38;2;204;252;227;49m▄▄▄▄▄▄\u001b[49m       \u001b[38;2;204;252;227;49m▄▄▄▄▄▄\u001b[49m    \u001b[m",
  "\u001b[38;2;206;250;227;48;2;205;251;227m▄\u001b[49m     \u001b[38;2;212;252;230;49m▄\u001b[49m        \u001b[49;38;2;164;201;181m▀\u001b[38;2;206;250;227;48;2;205;250;226m▄\u001b[49;38;2;159;201;180m▀\u001b[49m  \u001b[38;2;205;250;226;48;2;205;251;227m▄\u001b[49m  \u001b[49;38;2;204;252;227m▀▀\u001b[48;2;204;252;227m  \u001b[49;38;2;204;252;227m▀▀▀▀\u001b[49m  \u001b[48;2;204;252;227m  \u001b[49m     \u001b[38;2;204;252;227;49m▄▄\u001b[49;38;2;204;252;227m▀▀▀▀▀▀\u001b[38;2;204;252;227;49m▄\u001b[38;2;203;251;226;49m▄\u001b[49m   \u001b[38;2;204;252;227;49m▄▄\u001b[49;38;2;204;252;227m▀▀▀▀▀▀\u001b[38;2;204;252;227;49m▄▄\u001b[49m  \u001b[m",
  "\u001b[38;2;206;249;226;48;2;206;250;227m▄\u001b[49m   \u001b[38;2;206;249;226;49m▄\u001b[49;38;2;211;251;229m▀\u001b[49;38;2;206;250;227m▀\u001b[49m       \u001b[38;2;165;199;181;48;2;208;250;227m▄\u001b[49;38;2;174;210;191m▀\u001b[49m    \u001b[48;2;206;249;226m \u001b[49m    \u001b[48;2;204;252;227m  \u001b[49m      \u001b[48;2;204;252;227m  \u001b[49m     \u001b[48;2;204;252;227m  \u001b[38;2;204;252;227;49m▄▄▄▄▄▄\u001b[48;2;204;252;227m \u001b[48;2;203;251;226m \u001b[49m   \u001b[48;2;204;252;227m  \u001b[49m      \u001b[48;2;204;252;227m  \u001b[49m  \u001b[m",
  "\u001b[38;2;167;198;182;48;2;206;249;226m▄\u001b[38;2;208;248;227;49m▄\u001b[49m \u001b[38;2;207;248;226;49m▄\u001b[38;2;207;248;226;48;2;206;249;226m▄\u001b[38;2;207;248;226;48;2;212;250;230m▄\u001b[49m             \u001b[38;2;208;248;227;49m▄\u001b[38;2;178;212;194;48;2;206;249;226m▄\u001b[49m    \u001b[48;2;204;252;227m  \u001b[49m      \u001b[48;2;204;252;227m  \u001b[49m     \u001b[48;2;204;252;227m  \u001b[49;38;2;204;252;227m▀▀▀▀▀▀▀\u001b[49;38;2;203;251;226m▀\u001b[49m   \u001b[48;2;204;252;227m  \u001b[49m      \u001b[48;2;204;252;227m  \u001b[49m  \u001b[m",
  "\u001b[49m \u001b[38;2;199;237;217;48;2;207;248;226m▄\u001b[38;2;207;247;226;49m▄\u001b[38;2;207;247;226;48;2;207;248;226m▄\u001b[49m              \u001b[38;2;210;248;228;49m▄\u001b[38;2;207;247;226;48;2;207;248;226m▄\u001b[49m     \u001b[48;2;204;252;227m  \u001b[49m      \u001b[48;2;204;252;227m  \u001b[49m     \u001b[48;2;204;252;227m  \u001b[49m      \u001b[38;2;204;252;227;49m▄\u001b[38;2;203;251;226;49m▄\u001b[49m   \u001b[48;2;204;252;227m  \u001b[49m      \u001b[48;2;204;252;227m  \u001b[49m  \u001b[m",
  "\u001b[49m  \u001b[49;38;2;207;247;226m▀\u001b[49;38;2;167;198;181m▀\u001b[49m     \u001b[49;38;2;208;247;226m▀\u001b[49m \u001b[38;2;211;247;228;49m▄\u001b[38;2;208;247;226;48;2;209;247;226m▄\u001b[38;2;208;246;226;48;2;212;248;229m▄\u001b[38;2;211;247;228;49m▄▄\u001b[38;2;212;247;228;49m▄\u001b[49m        \u001b[48;2;204;252;227m  \u001b[49m      \u001b[49;38;2;204;252;227m▀▀\u001b[38;2;204;252;227;49m▄▄\u001b[49m   \u001b[49;38;2;204;252;227m▀▀\u001b[38;2;204;252;227;49m▄▄▄▄▄▄\u001b[49;38;2;204;252;227m▀\u001b[49;38;2;203;251;226m▀\u001b[49m   \u001b[49;38;2;204;252;227m▀▀\u001b[38;2;204;252;227;49m▄▄▄▄▄▄\u001b[49;38;2;204;252;227m▀▀\u001b[49m  \u001b[m",
  "\u001b[49m    \u001b[49;38;2;215;248;231m▀\u001b[38;2;167;197;181;48;2;210;246;227m▄\u001b[38;2;208;245;226;49m▄\u001b[38;2;209;246;226;49m▄\u001b[38;2;216;247;230;49m▄\u001b[49m   \u001b[38;2;215;247;230;49m▄\u001b[38;2;209;245;226;49m▄\u001b[48;2;208;246;226m \u001b[38;2;166;196;181;48;2;208;246;226m▄\u001b[49;38;2;208;246;226m▀\u001b[49m        \u001b[49;38;2;204;252;227m▀▀\u001b[49m        \u001b[49;38;2;204;252;227m▀▀\u001b[49m     \u001b[49;38;2;204;252;227m▀▀▀▀▀▀\u001b[49m       \u001b[49;38;2;204;252;227m▀▀▀▀▀▀\u001b[49m    \u001b[m",
  "\u001b[49m        \u001b[49;38;2;167;196;181m▀\u001b[49;38;2;209;245;226m▀▀▀\u001b[49;38;2;167;196;181m▀\u001b[49m                                                    \u001b[m",
  "\u001b[49m                                                                 \u001b[m",
] as const;

const LOGO_HEADER_LINE = Math.floor((ANSI_LOGO_LINES.length - 1) / 2) + 1;
export const BRAND_HEADER = "\u001b[38;5;194mby \u001b]8;;https://www.flyweel.co\u0007Flyweel\u001b]8;;\u0007\u001b[m";

// The tall ANSI masthead only appears where there is nothing else to show (setup and
// the config keyword stage) and only when the terminal can afford it — everywhere else
// it would push frames past the terminal height and strand stale rows in scrollback.
export const MASTHEAD_MIN_ROWS = 34;
export const MASTHEAD_MIN_COLS = 78;

export const mastheadVisible = (state: AppState, rows: number, columns = 80): boolean =>
  rows >= MASTHEAD_MIN_ROWS &&
  columns >= MASTHEAD_MIN_COLS &&
  !state.helpOpen &&
  !state.logOpen &&
  (state.step === 'setup' || (state.step === 'config' && state.configStage === 'keyword'));

// Review appears in the trail only when this run actually hit the gate.
const reviewVisible = (state: AppState): boolean =>
  state.step === 'review' || state.progressRows.some(row => row.phase === 'review');

export const JourneyIndicator = ({state}: {state: AppState}) => {
  const theme = useTheme();
  const inSetup = state.step === 'setup';
  const steps = JOURNEY.filter(entry => entry.step !== 'review' || reviewVisible(state));
  const activeIndex = steps.findIndex(entry => entry.step === state.step);
  return (
    <Box>
      {steps.map((entry, index) => {
        const active = !inSetup && entry.step === state.step;
        const done = !inSetup && activeIndex > -1 && index < activeIndex;
        const flyweelActive = active && theme.name === 'flyweel';
        return (
          <React.Fragment key={entry.step}>
            {index > 0 ? <Text color={theme.colors.mutedForeground}> › </Text> : null}
            <Text
              bold={active}
              inverse={active && !flyweelActive}
              backgroundColor={flyweelActive ? '#0A0A0A' : undefined}
              color={active ? theme.colors.selectionForeground : done ? theme.colors.primary : theme.colors.mutedForeground}
            >
              {active ? ` ${entry.label} ` : entry.label}
            </Text>
          </React.Fragment>
        );
      })}
    </Box>
  );
};

export const BrandMasthead = () => (
  <Box marginBottom={1} flexDirection="column">
    {ANSI_LOGO_LINES.map((line, index) => (
      <Text key={index}>{index === LOGO_HEADER_LINE ? `${line}  ${BRAND_HEADER}` : line}</Text>
    ))}
  </Box>
);

export const HeaderBar = ({state, showMasthead = false}: {state: AppState; showMasthead?: boolean}) => {
  const theme = useTheme();
  const {columns} = useLayout();
  const compact = columns < 72;
  const badge = state.outcome && state.step === 'output' ? OUTCOME_BADGE[state.outcome] : undefined;
  return (
    // flexShrink=0: when panel content overflows the root height, Yoga must clip the
    // body (overflowY hidden), never compress the header — otherwise the masthead's
    // logo lines get eaten on start.
    <Box flexDirection="column" flexShrink={0}>
      {showMasthead ? <BrandMasthead /> : null}
      <Box justifyContent="space-between" width={columns}>
        <Box>
          {state.step === 'setup' ? (
            <Text color={theme.colors.warning}>Environment setup</Text>
          ) : compact ? (
            <Text bold>{JOURNEY.find(entry => entry.step === state.step)?.label ?? state.step}</Text>
          ) : (
            <JourneyIndicator state={state} />
          )}
        </Box>
        <Box flexShrink={1}>
          <Text color={theme.colors.mutedForeground} wrap="truncate-end">
            {compact ? state.config.style : state.config.keyword ? `${state.config.keyword} | ${state.config.style}` : state.config.style}
          </Text>
          {badge ? (
            <Text color={theme.colors[badge.tone]}>
              {'  '}
              {badge.glyph} {badge.label}
            </Text>
          ) : null}
          {/* When the masthead is hidden the brand collapses to its single line here. */}
          {!showMasthead && !compact && state.step !== 'running' ? <Text>{'  '}{BRAND_HEADER}</Text> : null}
        </Box>
      </Box>
    </Box>
  );
};

export const Notices = ({state}: {state: AppState}) => {
  const latest = state.notifications.slice(-2);
  if (latest.length === 0) return null;
  return (
    // flexShrink=0: chrome never absorbs body overflow (see HeaderBar).
    <Box flexDirection="column" flexShrink={0}>
      {latest.map(notification => (
        <StatusMessage key={notification.id} tone={notification.tone}>
          {notification.message}
        </StatusMessage>
      ))}
    </Box>
  );
};

// Single dim footer line rendered from the keymap — never hand-written hints.
// Boxed with flexShrink=0 so body overflow can never swallow the hint line.
export const FooterBar = ({state}: {state: AppState}) => {
  const seen = new Set<string>();
  const hints = bindingsFor(state)
    .filter(binding => {
      const key = `${binding.keyLabel}:${binding.label}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .map(binding => `${binding.keyLabel} ${binding.label}`);
  return (
    <Box flexShrink={0}>
      <Text dimColor wrap="truncate-end">
        {hints.join(' · ')}
      </Text>
    </Box>
  );
};

const STEP_TITLE: Record<JourneyStep, string> = {
  setup: 'Setup',
  config: 'Configure',
  context: 'Context',
  running: 'Generate',
  review: 'Review',
  preview: 'Preview',
  output: 'Output',
};

// Full-region help. Grouped from the keymap: current step first, then other steps,
// then global bindings. Replaces the step region while open (Ink cannot float).
// Content is built as one row per line and windowed to the body budget so the
// overlay can never push the frame past the terminal height.
export const HelpOverlay = ({state, viewport}: {state: AppState; viewport?: number}) => {
  const theme = useTheme();
  const {bodyRows} = useLayout();
  const limit = Math.max(3, viewport ?? bodyRows);
  const steps: JourneyStep[] = ['config', 'context', 'running', 'review', 'preview', 'output', 'setup'];
  const ordered = [state.step, ...steps.filter(step => step !== state.step)];
  const globals = KEYMAP.filter(binding => binding.steps === 'all');

  const rows: React.ReactNode[] = [
    <Text key="intro" color={theme.colors.mutedForeground} wrap="truncate-end">
      One journey: Configure › Context › Generate › Review (when the gate opens) › Preview › Output.
    </Text>,
    <Text key="gap-intro"> </Text>,
  ];
  for (const step of ordered) {
    const bindings = KEYMAP.filter(binding => binding.steps !== 'all' && binding.steps.includes(step));
    if (bindings.length === 0) continue;
    const current = step === state.step;
    rows.push(
      <Box key={step}>
        <Box width={12} flexShrink={0}>
          <Text bold={current} color={current ? theme.colors.primary : theme.colors.mutedForeground}>
            {STEP_TITLE[step]}
          </Text>
        </Box>
        <Text color={current ? undefined : theme.colors.mutedForeground} wrap="truncate-end">
          {bindings.map(binding => `${binding.keyLabel} ${binding.label}`).join(' · ')}
        </Text>
      </Box>,
    );
  }
  rows.push(<Text key="gap-anywhere"> </Text>);
  rows.push(
    <Box key="anywhere">
      <Box width={12} flexShrink={0}>
        <Text color={theme.colors.mutedForeground}>Anywhere</Text>
      </Box>
      <Text color={theme.colors.mutedForeground} wrap="truncate-end">
        {globals.map(binding => `${binding.keyLabel} ${binding.label}`).join(' · ')}
      </Text>
    </Box>,
  );
  rows.push(<Text key="gap-close"> </Text>);
  rows.push(
    <Text key="close" color={theme.colors.mutedForeground}>
      Esc or ? to close
    </Text>,
  );

  // Every entry renders exactly one line, so the slice is the height guarantee.
  const clipped = rows.length > limit;
  const visible = clipped ? rows.slice(0, Math.max(1, limit - 1)) : rows;
  return (
    <Box flexDirection="column">
      {visible}
      {clipped ? (
        <Text color={theme.colors.mutedForeground}>… {rows.length - visible.length} more — Esc or ? to close</Text>
      ) : null}
    </Box>
  );
};

const LOG_COLOR: Record<LogEntry['level'], 'mutedForeground' | 'accent' | 'warning' | 'error'> = {
  info: 'mutedForeground',
  tool: 'accent',
  warn: 'warning',
  error: 'error',
};

// Log drawer: the run's full activity stream (bridge stderr, tool calls, warnings,
// errors). Replaces the step region while open; `l` or Esc closes. Windowed to the
// body budget (2 rows reserved for the footer hint + its margin).
export const LogDrawer = ({state, viewport}: {state: AppState; viewport?: number}) => {
  const theme = useTheme();
  const {bodyRows} = useLayout();
  const limit = Math.max(3, viewport ?? bodyRows - 2);
  const entries = state.log.slice(-limit);
  return (
    <Box flexDirection="column">
      {entries.length === 0 ? <Text color={theme.colors.mutedForeground}>Nothing logged yet.</Text> : null}
      {entries.map(entry => (
        <Text key={entry.id} color={theme.colors[LOG_COLOR[entry.level]]} wrap="truncate-end">
          [{entry.level}] {entry.text}
        </Text>
      ))}
      <Box marginTop={1}>
        <Text color={theme.colors.mutedForeground} wrap="truncate-end">
          {state.log.length > entries.length ? `showing last ${entries.length} of ${state.log.length} · ` : ''}l or Esc to close
        </Text>
      </Box>
    </Box>
  );
};
