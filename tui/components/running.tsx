import React, {useEffect, useState} from 'react';
import {Box, Text, useInput} from 'ink';
import {ConfirmInput} from '@inkjs/ui';
import {TextInput} from './textInput.js';

import {useLayout} from '../lib/useTerminalSize.js';
import {canonicalPhase} from '../state.js';
import {SPINNER_FRAMES, TodoList} from './todo.js';
import {useTheme} from './ui/theme-provider.js';
import type {AppState} from '../types.js';

const formatElapsed = (startedAt?: number): string => {
  if (!startedAt) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
};

// Confirmation strip shown when the user presses Esc during a run. ConfirmInput owns
// y/n; Esc dismisses. Defaults to "keep running" so a stray Enter never kills a run.
const CancelConfirm = ({onConfirm, onDismiss}: {onConfirm: () => void; onDismiss: () => void}) => {
  const theme = useTheme();
  useInput((_input, key) => {
    if (key.escape) onDismiss();
  });
  return (
    <Box borderStyle="single" borderColor={theme.colors.warning} paddingX={1}>
      <Text color={theme.colors.warning}>Cancel this run? </Text>
      <ConfirmInput defaultChoice="cancel" onConfirm={onConfirm} onCancel={onDismiss} />
    </Box>
  );
};

// Low-GSC-score gate strip. The bridge is paused inside the gsc phase waiting for a
// gsc_decision, so a choice is required — Esc does nothing here (defaults to "produce"
// so a stray Enter never kills a viable run; `n` explicitly stops it).
const GscGateConfirm = ({
  gate,
  onDecide,
}: {
  gate: NonNullable<AppState['gscGate']>;
  onDecide: (decision: 'proceed' | 'abort') => void;
}) => {
  const theme = useTheme();
  // Estimated scores (no direct GSC data — inferred from semantically related queries)
  // render as approximate with their basis; direct scores keep the ≈clicks/mo detail.
  const scoreText = gate.estimated
    ? `~${gate.score}% (${typeof gate.neighborCount === 'number' ? `estimated from ${gate.neighborCount} related queries` : 'estimated'})`
    : `${gate.score}%${typeof gate.trafficPotential === 'number' ? ` (≈${Math.round(gate.trafficPotential)} clicks/mo)` : ''}`;
  return (
    <Box borderStyle="single" borderColor={theme.colors.warning} paddingX={1} flexDirection="column">
      <Text color={theme.colors.warning}>
        GSC score {scoreText} is below {gate.threshold}%{gate.primaryQuery ? ` — best query “${gate.primaryQuery}”` : ''}
      </Text>
      <Box>
        <Text>Still produce this article? </Text>
        <ConfirmInput defaultChoice="confirm" onConfirm={() => onDecide('proceed')} onCancel={() => onDecide('abort')} />
      </Box>
    </Box>
  );
};

// Inline comment bar shown while a run is active and the user pressed `c`.
const CommentBar = ({onSubmit, onCancel}: {onSubmit: (value: string) => void; onCancel: () => void}) => {
  const theme = useTheme();
  useInput((_input, key) => {
    if (key.escape) onCancel();
  });
  return (
    <Box borderStyle="single" borderColor={theme.colors.focusRing} paddingX={1} flexDirection="column">
      <Text color={theme.colors.accent}>comment — Enter to queue · Esc to cancel</Text>
      <TextInput placeholder="note for the editor…" onSubmit={onSubmit} />
    </Box>
  );
};

const TERMINAL_STATUSES = new Set(['complete', 'skipped', 'error', 'warning']);

// Header line: spinner + phase label + step counter + elapsed clock. The
// spinner shares the 250ms tick with the todo list's running-row glyphs so
// all motion is in phase and updates are in-place text swaps (no reflow).
export const RunningHeader = ({state, tick}: {state: AppState; tick: number}) => {
  const theme = useTheme();
  const phaseKey = canonicalPhase(state.activePhase);
  const phaseLabel = state.planPhases.find(plan => plan.phase === phaseKey)?.label ?? state.activePhase ?? 'starting';
  const total = state.progressRows.length;
  const done = state.progressRows.filter(row => TERMINAL_STATUSES.has(row.status)).length;
  const elapsed = formatElapsed(state.startedAt);
  return (
    <Box>
      <Text color={theme.colors.accent} bold>
        {SPINNER_FRAMES[tick % SPINNER_FRAMES.length]}{' '}
      </Text>
      <Text bold>Generating — {phaseLabel}</Text>
      {total > 0 ? (
        <Text color={theme.colors.mutedForeground}>
          {'  '}{done}/{total}
        </Text>
      ) : null}
      {elapsed ? <Text color={theme.colors.mutedForeground}>{'  '}{elapsed}</Text> : null}
    </Box>
  );
};

export const RunningView = ({
  state,
  onSubmitComment,
  onCancelComment,
  onConfirmCancel,
  onDismissCancel,
  onGscDecision,
}: {
  state: AppState;
  onSubmitComment: (value: string) => void;
  onCancelComment: () => void;
  onConfirmCancel: () => void;
  onDismissCancel: () => void;
  onGscDecision: (decision: 'proceed' | 'abort') => void;
}) => {
  const theme = useTheme();
  const {bodyRows} = useLayout();
  // One 250ms interval drives the header spinner, the todo list's running
  // glyphs, and the elapsed clock — scoped to the live run only.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!state.startedAt) return;
    const interval = setInterval(() => setTick(current => current + 1), 250);
    return () => clearInterval(interval);
  }, [state.startedAt]);

  // Budget: header (1) + margins (2) + comment strip / confirm bars (~3 worst case).
  const overlayRows =
    (state.commandMode === 'comment' ? 3 : 0) +
    (state.cancelPending ? 3 : 0) +
    (state.gscGate ? 4 : 0) +
    (state.queuedComments.length > 0 ? 1 : 0);
  const todoViewport = Math.max(5, bodyRows - 3 - overlayRows);

  return (
    <Box flexDirection="column">
      <RunningHeader state={state} tick={tick} />
      <Box marginTop={1} flexDirection="column">
        <TodoList rows={state.progressRows} planPhases={state.planPhases} viewport={todoViewport} tick={tick} />
      </Box>
      {state.queuedComments.length > 0 ? (
        <Box marginTop={1} flexDirection="column">
          <Text color={theme.colors.mutedForeground}>
            {state.queuedComments.length} comment{state.queuedComments.length === 1 ? '' : 's'} queued for the review gate
          </Text>
        </Box>
      ) : null}
      {state.gscGate ? <GscGateConfirm gate={state.gscGate} onDecide={onGscDecision} /> : null}
      {state.commandMode === 'comment' && !state.gscGate ? <CommentBar onSubmit={onSubmitComment} onCancel={onCancelComment} /> : null}
      {/* The GSC gate strip wins while both confirms could show — two live ConfirmInputs
          would double-capture y/n. The cancel confirm resurfaces once the gate settles. */}
      {state.cancelPending && !state.gscGate ? <CancelConfirm onConfirm={onConfirmCancel} onDismiss={onDismissCancel} /> : null}
    </Box>
  );
};
