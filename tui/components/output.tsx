import React from 'react';
import {Box, Text, useInput} from 'ink';
import {ConfirmInput, Spinner} from '@inkjs/ui';

import {hasFinalArtifact} from '../state.js';
import {KeyValue, OptionList, StatusMessage} from './termcn.js';
import {useTheme} from './ui/theme-provider.js';
import type {AppAction, AppState, ActionStatus} from '../types.js';

const actionLabel = (status: ActionStatus, doneLabel: string): string => {
  if (status === 'running') return 'in progress…';
  if (status === 'done') return doneLabel;
  if (status === 'failed') return 'failed — see notifications';
  return 'not yet';
};

const OutcomeBanner = ({state}: {state: AppState}) => {
  if (state.outcome === 'error') {
    return <StatusMessage tone="error">Generation failed{state.lastError ? `: ${state.lastError}` : '.'}</StatusMessage>;
  }
  if (state.outcome === 'cancelled') {
    return <StatusMessage tone="warning">Run cancelled. Partial results (if any) are listed below.</StatusMessage>;
  }
  return <StatusMessage tone="success">Generation complete.</StatusMessage>;
};

export const OutputView = ({state, dispatch, onQuit}: {state: AppState; dispatch: React.Dispatch<AppAction>; onQuit: () => void}) => {
  const theme = useTheme();
  const siteTarget = state.setupStatus?.siteTarget;
  const legacyBlogCheck = state.setupStatus?.checks.find(check => check.id === 'site_target' || check.id === 'astro_blog_path');
  const publishTargetPath =
    process.env.FLEO_BLOG_CONTENT_PATH ||
    process.env.ASTRO_BLOG_PATH ||
    siteTarget?.contentDir ||
    legacyBlogCheck?.detail ||
    'No repo connected — use Save locally (creates agent-handoff.md)';
  const targetLabel = siteTarget?.frameworkLabel || 'connected site';
  const canAct = hasFinalArtifact(state);

  const fileItems: Array<[string, React.ReactNode]> = [
    ...Object.entries(state.outputPaths).map(([key, value]) => [key, value] as [string, React.ReactNode]),
    ['Saved locally', actionLabel(state.saveState, 'done (includes agent-handoff.md + schema.json)')],
    ['Published', actionLabel(state.publishState, `done → ${publishTargetPath}`)],
    ['Site publish target', publishTargetPath],
  ];

  const options = [
    ...(canAct
      ? [{value: 'save', label: state.saveState === 'done' ? 'Save locally again (+ agent-handoff.md)' : 'Save locally (+ agent-handoff.md & schema.json)'}]
      : []),
    ...(canAct ? [{value: 'publish', label: `Publish directly to ${targetLabel}…`}] : []),
    ...(canAct ? [{value: 'preview', label: 'Back to preview'}] : []),
    {value: 'new', label: 'Start a new article'},
    {value: 'quit', label: 'Quit'},
  ];

  const onSelect = (value: string) => {
    if (value === 'save') dispatch({type: 'request_save'});
    else if (value === 'publish') dispatch({type: 'arm_publish'});
    else if (value === 'preview') dispatch({type: 'goto_preview'});
    else if (value === 'new') dispatch({type: 'new_run'});
    else if (value === 'quit') onQuit();
  };

  useInput(
    (_input, key) => {
      if (key.escape) dispatch({type: 'escape'});
    },
    {isActive: state.publishArmed},
  );

  return (
    <Box flexDirection="column">
      <OutcomeBanner state={state} />
      <Box marginTop={1} flexDirection="column">
        <KeyValue items={fileItems} />
      </Box>
      {state.outputPaths.agentHandoff ? (
        <Box marginTop={1}>
          <Text color={theme.colors.accent}>
            Tip: Drop {state.outputPaths.agentHandoff} into Claude Code, Cursor, or Lovable inside your website project to publish!
          </Text>
        </Box>
      ) : null}
      {state.saveState === 'running' ? <Spinner label="Saving article + agent-handoff.md + schema.json…" /> : null}
      {state.publishState === 'running' ? <Spinner label={`Publishing to ${targetLabel}…`} /> : null}
      <Box marginTop={1} flexDirection="column">
        {state.publishArmed ? (
          <Box borderStyle="single" borderColor={theme.colors.warning} paddingX={1}>
            <Text color={theme.colors.warning}>Publish to {publishTargetPath}? </Text>
            <ConfirmInput
              defaultChoice="cancel"
              onConfirm={() => dispatch({type: 'request_publish'})}
              onCancel={() => dispatch({type: 'escape'})}
            />
          </Box>
        ) : (
          <OptionList options={options} initialIndex={0} onSelect={onSelect} isActive={!state.publishArmed} />
        )}
      </Box>
    </Box>
  );
};
