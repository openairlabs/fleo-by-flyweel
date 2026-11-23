import React from 'react';
import {Box, Text, useInput} from 'ink';
import {TextInput} from './textInput.js';

import {useLayout} from '../lib/useTerminalSize.js';
import {wrapText} from '../lib/wrap.js';
import {latestArtifactByName} from '../preview.js';
import {MultiProgress, OptionList, StatusMessage} from './termcn.js';
import {useTheme} from './ui/theme-provider.js';
import type {AppAction, AppState, BridgeCommand} from '../types.js';

const EXCERPT_LINES = 12;
const VISIBLE_COMMENTS = 3;

// Review-gate decision view. Built on OptionList so the choice is unambiguous and
// keystrokes are owned by one widget (App's nav handler is disabled on the review step).
export const ReviewGateView = ({
  state,
  dispatch,
  sendCommand,
  width,
}: {
  state: AppState;
  dispatch: React.Dispatch<AppAction>;
  sendCommand: (command: BridgeCommand) => void;
  width?: number;
}) => {
  const theme = useTheme();
  const {columns, bodyRows} = useLayout();
  const contentWidth = width ?? Math.max(8, columns - 6);
  const [phase, setPhase] = React.useState<'menu' | 'add' | 'rerun'>('menu');
  const [draftNote, setDraftNote] = React.useState('');
  // ^O: replace the whole gate body with a scrollable full-height draft view.
  const [expanded, setExpanded] = React.useState(false);
  const [offset, setOffset] = React.useState(0);
  const prePolish = latestArtifactByName(state, ['pre_polish_mdx']);
  const prePolishData = prePolish?.data as {content?: string} | undefined;
  const allLines = React.useMemo(
    () => (prePolishData?.content ? wrapText(prePolishData.content, contentWidth) : []),
    [prePolishData?.content, contentWidth],
  );
  const fullViewport = Math.max(6, bodyRows - 2);
  const maxOffset = Math.max(0, allLines.length - fullViewport);
  React.useEffect(() => {
    setOffset(current => Math.min(current, maxOffset));
  }, [maxOffset]);
  // Plan-seeded runs park the gate on the refinement Review Gate row; legacy
  // bridges still emit phase:"review" rows.
  const allReviewRows = state.progressRows
    .filter(row => row.phase === 'review' || row.id === 'task:refinement:review-gate');
  const allVisibleComments = state.queuedComments.slice(-VISIBLE_COMMENTS);
  const menuRowsBudget = phase === 'menu' ? 6 : 4; // marginTop (1) + 5 menu options (or 3 note rows)
  // Status banner (1) + Queued Comments heading (1) + Pre-polish Draft heading (1) + min 1 draft row = 4
  const commentRowsCap = Math.max(1, Math.min(VISIBLE_COMMENTS, bodyRows - menuRowsBudget - 4));
  // Reserve the "+N more" row when comments are hidden.
  const hiddenMarkerRows = state.queuedComments.length > commentRowsCap ? 1 : 0;
  const maxCommentRows = Math.max(1, Math.min(VISIBLE_COMMENTS, bodyRows - menuRowsBudget - 4 - hiddenMarkerRows));
  const visibleComments = allVisibleComments.slice(-maxCommentRows);
  const hiddenComments = state.queuedComments.length - visibleComments.length;
  const commentSectionRows = 1 + Math.max(1, visibleComments.length) + (hiddenComments > 0 ? 1 : 0);
  const maxProgressRows = Math.max(0, Math.min(6, bodyRows - menuRowsBudget - commentSectionRows - 4));
  const reviewRows = maxProgressRows > 0 ? allReviewRows.slice(-maxProgressRows) : [];
  const progressSectionRows = reviewRows.length > 0 ? 1 + reviewRows.length : 0;
  // Reserve exact space consumed by status banner (1), comments, draft heading (1), progress, and menu.
  const reservedRows = 1 + commentSectionRows + 1 + progressSectionRows + menuRowsBudget;
  const excerptLines = Math.max(1, Math.min(EXCERPT_LINES, bodyRows - reservedRows));
  const excerpt = allLines.slice(0, excerptLines);
  const runId = state.runId ?? 'local';

  useInput((input, key) => {
    if (key.ctrl && input === 'o') {
      setOffset(0);
      setExpanded(current => !current);
      return;
    }
    if (expanded) {
      // Expanded-view Esc collapses first — it never falls through to the menu reset.
      if (key.escape) {
        setExpanded(false);
        return;
      }
      if (key.upArrow) setOffset(current => Math.max(0, current - 1));
      if (key.downArrow) setOffset(current => Math.min(maxOffset, current + 1));
      if (key.pageUp) setOffset(current => Math.max(0, current - fullViewport));
      if (key.pageDown) setOffset(current => Math.min(maxOffset, current + fullViewport));
      if (input === 'g') setOffset(0);
      if (input === 'G') setOffset(maxOffset);
      return;
    }
    if (key.escape && phase !== 'menu') setPhase('menu');
  });

  const decide = (value: string) => {
    if (value === 'approve') {
      sendCommand({type: 'review_decision', runId, decision: 'approve'});
      dispatch({type: 'bridge_event', event: {type: 'review_gate_closed', runId, timestamp: new Date().toISOString(), phase: 'review', message: 'Approved'}});
    } else if (value === 'apply') {
      sendCommand({type: 'review_decision', runId, decision: 'apply_comments'});
    } else if (value === 'add') {
      setPhase('add');
    } else if (value === 'rerun') {
      setPhase('rerun');
    } else if (value === 'cancel') {
      sendCommand({type: 'cancel', runId});
      dispatch({type: 'notify', tone: 'info', message: 'Cancelling run…'});
    }
  };

  const submitNote = (decision: 'add_final_comment' | 'rerun_comment_edit') => (value: string) => {
    const note = value.trim();
    sendCommand({type: 'review_decision', runId, decision, comment: note || undefined});
    setDraftNote('');
    setPhase('menu');
  };

  if (expanded) {
    const visible = allLines.slice(offset, offset + fullViewport);
    return (
      <Box flexDirection="column">
        {visible.length > 0 ? (
          visible.map((line, index) => <Text key={`full-${offset + index}`}>{line || ' '}</Text>)
        ) : (
          <Text dimColor>Waiting for pre-polish MDX artifact.</Text>
        )}
        <Text color={theme.colors.mutedForeground}>
          lines {allLines.length === 0 ? 0 : offset + 1}–{Math.min(allLines.length, offset + fullViewport)} of {allLines.length} · Ctrl+O or Esc to exit full content
        </Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column">
      <StatusMessage tone="warning">Pre-polish gate is open. Choose how to proceed.</StatusMessage>
      <Text bold>Queued Comments</Text>
      {visibleComments.length > 0 ? (
        visibleComments.map((comment, index) => (
          <Text key={`${comment}-${index}`} wrap="truncate-end">
            - {comment}
          </Text>
        ))
      ) : (
        <Text dimColor>No queued comments.</Text>
      )}
      {hiddenComments > 0 ? <Text dimColor>+{hiddenComments} more</Text> : null}
      <Text bold>Pre-polish Draft</Text>
      {excerpt.length > 0 ? (
        excerpt.map((line, index) => (
          <Text key={`excerpt-${index}`} dimColor>
            {line || ' '}
          </Text>
        ))
      ) : (
        <Text dimColor>Waiting for pre-polish MDX artifact.</Text>
      )}
      {reviewRows.length > 0 ? (
        <>
          <Text bold>Gate Progress</Text>
          <MultiProgress rows={reviewRows} />
        </>
      ) : null}
      <Box marginTop={1}>
        {phase === 'menu' ? (
          <OptionList
            options={[
              {value: 'approve', label: 'approve — continue to final polish'},
              {value: 'apply', label: 'apply queued comments, then polish'},
              {value: 'add', label: 'add a final note…'},
              {value: 'rerun', label: 'rerun comment edit…'},
              {value: 'cancel', label: 'cancel generation'},
            ]}
            onSelect={decide}
          />
        ) : (
          <Box flexDirection="column">
            <Text>{phase === 'add' ? 'Final note:' : 'Rerun note:'}</Text>
            <TextInput
              defaultValue={draftNote}
              placeholder="type a note…"
              onChange={setDraftNote}
              onSubmit={submitNote(phase === 'add' ? 'add_final_comment' : 'rerun_comment_edit')}
            />
            <Text color={theme.colors.mutedForeground}>Enter send · Esc back</Text>
          </Box>
        )}
      </Box>
    </Box>
  );
};
