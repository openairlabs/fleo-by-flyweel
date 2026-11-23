import React from 'react';
import {Box, Text, useInput} from 'ink';
import {Spinner} from '@inkjs/ui';
import {TextInput} from './textInput.js';

import {useLayout} from '../lib/useTerminalSize.js';
import {wrapText} from '../lib/wrap.js';
import {useTheme} from './ui/theme-provider.js';
import type {AppAction, AppState} from '../types.js';

// Rows around the interpretation body: header, margins, steer input + hint line.
const CHROME_OVERHEAD = 6;

// Context step: confirm (or steer) the AI's topic interpretation before the run starts.
// This view is the single keyboard owner while active (App's nav handler is disabled):
// Enter starts the run, typed text steers a regeneration, Ctrl+N regenerates fresh,
// Esc returns to config settings. Unlimited regens; the take counter tracks attempts.
export const ContextView = ({state, dispatch}: {state: AppState; dispatch: React.Dispatch<AppAction>}) => {
  const theme = useTheme();
  const {columns, bodyRows} = useLayout();
  const interpreting = state.interpretState === 'running';
  const context = state.config.keywordContext.trim();

  useInput((input, key) => {
    if (key.escape) {
      dispatch({type: 'set_step', step: 'config'});
      return;
    }
    if (key.ctrl && input === 'n') {
      dispatch({type: 'request_interpret'});
      return;
    }
    // While interpreting there is no steer input, so Enter starts without waiting.
    if (key.return && interpreting) dispatch({type: 'request_run'});
  });

  const lines = context ? wrapText(context, Math.max(20, columns - 6)) : [];
  const viewport = Math.max(3, bodyRows - CHROME_OVERHEAD);
  const visible = lines.slice(0, viewport);
  const clipped = lines.length - visible.length;
  const take = state.interpretAttempt > 1 ? ` · take ${state.interpretAttempt}` : '';

  return (
    <Box flexDirection="column" paddingX={1}>
      <Box>
        <Text color={theme.colors.primary} bold>
          Topic Context
        </Text>
        <Text color={theme.colors.mutedForeground}>
          {'  '}{state.config.keyword} · {state.config.style}{take}
        </Text>
      </Box>
      <Box marginTop={1} flexDirection="column">
        {interpreting ? (
          <Spinner label="Interpreting topic — Gemini Flash…" />
        ) : context ? (
          <>
            {visible.map((line, index) => (
              <Text key={`ctx-${index}`}>{line || ' '}</Text>
            ))}
            {clipped > 0 ? <Text color={theme.colors.mutedForeground}>… {clipped} more lines</Text> : null}
          </>
        ) : state.interpretState === 'failed' ? (
          <Text color={theme.colors.error}>Topic interpretation failed — Ctrl+N to retry, Enter to start without context.</Text>
        ) : (
          <Text color={theme.colors.mutedForeground}>No topic context yet — Ctrl+N to interpret, Enter to start without it.</Text>
        )}
      </Box>
      {interpreting ? (
        <Box marginTop={1}>
          <Text color={theme.colors.mutedForeground}>Enter start without waiting · Esc back</Text>
        </Box>
      ) : (
        <Box marginTop={1} flexDirection="column">
          <TextInput
            key={`steer-${state.interpretRequestId}`}
            placeholder="type to steer the interpretation, Enter alone to start…"
            onSubmit={value => {
              const feedback = value.trim();
              if (feedback) dispatch({type: 'request_interpret', feedback});
              else dispatch({type: 'request_run'});
            }}
          />
          <Text color={theme.colors.mutedForeground}>Enter start · type to steer · Ctrl+N regenerate · Esc back</Text>
        </Box>
      )}
    </Box>
  );
};
