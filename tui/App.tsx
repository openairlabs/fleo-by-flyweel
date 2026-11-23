import React, {useCallback, useEffect, useMemo, useReducer, useRef, useState} from 'react';
import {Box, Text, useApp, useInput} from 'ink';
import type {Key} from 'ink';
import {ThemeProvider as InkUIThemeProvider} from '@inkjs/ui';

import {runArtifactAction} from './artifactActions.js';
import {spawnBridge, spawnKeywordInterpretation, spawnKeywordSuggestions, spawnSetupStatus} from './bridge.js';
import {ANSI_LOGO_LINES, FooterBar, HeaderBar, HelpOverlay, LogDrawer, Notices, mastheadVisible} from './components/chrome.js';
import {ConfigFlow} from './components/config.js';
import {ContextView} from './components/contextView.js';
import {OutputView} from './components/output.js';
import {PreviewView} from './components/previewView.js';
import {ReviewGateView} from './components/review.js';
import {RunningView} from './components/running.js';
import {SetupView} from './components/setupView.js';
import {Panel} from './components/termcn.js';
import {ThemeProvider} from './components/ui/theme-provider.js';
import {useTheme} from './components/ui/theme-provider.js';
import {bindingsFor} from './keymap.js';
import {createQuitHandler} from './lib/quit.js';
import {getTheme} from './lib/terminal-themes/index.js';
import {buildInkUITheme} from './lib/terminal-themes/inkui.js';
import {LayoutContext, useTerminalSize} from './lib/useTerminalSize.js';
import {createInitialState, reducer} from './state.js';
import {runSetupSaveAction} from './setupActions.js';
import type {BridgeClient} from './bridge.js';
import type {TerminalSize} from './lib/useTerminalSize.js';
import type {AppState, BridgeEvent, GenerationConfig, JourneyStep} from './types.js';

interface AppProps {
  initialStep?: JourneyStep;
  initialState?: Partial<Omit<AppState, 'config'>> & {config?: Partial<GenerationConfig>};
  config?: Partial<GenerationConfig>;
  noBridge?: boolean;
  setupOnly?: boolean;
  themeName?: string;
  // Test-only override; the app measures process.stdout (and tracks resize) by default.
  size?: TerminalSize;
}

const STEP_TITLE: Record<JourneyStep, string> = {
  setup: 'Setup',
  config: 'Configure',
  context: 'Topic Context',
  running: 'Generate',
  review: 'Review Gate',
  preview: 'Preview',
  output: 'Output',
};

const InnerApp = ({initialStep = 'config', initialState, config, noBridge = false, setupOnly = false, size}: Omit<AppProps, 'themeName'>) => {
  const {exit} = useApp();
  // runRef holds ONLY the generate-run child — the one bridge process that accepts stdin
  // commands (comments, review decisions, cancel). Setup/interpret probes never own it,
  // so review-gate commands can never land on the wrong child.
  const runRef = useRef<BridgeClient | null>(null);
  const interpretRef = useRef<BridgeClient | null>(null);
  const suggestRef = useRef<BridgeClient | null>(null);
  const stateRef = useRef<AppState | null>(null);
  const lastRunRequestRef = useRef(0);
  const lastSetupRequestRef = useRef(0);
  const lastSetupSaveRequestRef = useRef(0);
  const lastInterpretRequestRef = useRef(0);
  const lastSuggestRequestRef = useRef(0);
  const lastSaveRequestRef = useRef(0);
  const lastPublishRequestRef = useRef(0);
  const cancelKillTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const didInitialSetupRef = useRef(false);
  const didAutoOnboardCheckRef = useRef(false);
  const [state, dispatch] = useReducer(
    reducer,
    createInitialState({
      ...initialState,
      step: setupOnly ? 'setup' : initialState?.step ?? initialStep,
      config: {...initialState?.config, ...config},
    }),
  );
  stateRef.current = state;

  // One-time setup-status probe so config readiness (and first-time onboarding) is known up front.
  useEffect(() => {
    if (noBridge || setupOnly || didInitialSetupRef.current) return;
    didInitialSetupRef.current = true;
    const runId = `setupcheck_${Date.now().toString(36)}`;
    let gotSetupStatus = false;
    const client = spawnSetupStatus(runId);
    client.on('events', (events: BridgeEvent[]) => {
      dispatch({type: 'bridge_events', events});
      const statusEv = events.find(e => e.type === 'setup_status');
      if (statusEv) gotSetupStatus = true;
      if (!didAutoOnboardCheckRef.current && !initialState && !config?.keyword) {
        const st = statusEv?.data as {canGenerate?: boolean; onboardingComplete?: boolean} | undefined;
        if (st && (!st.canGenerate || st.onboardingComplete === false)) {
          didAutoOnboardCheckRef.current = true;
          dispatch({type: 'set_step', step: 'setup'});
        }
      }
    });
    client.on('exit', code => {
      if (code !== 0 && !gotSetupStatus && !didAutoOnboardCheckRef.current && !initialState && !config?.keyword) {
        didAutoOnboardCheckRef.current = true;
        dispatch({type: 'set_step', step: 'setup'});
      }
    });
    client.on('diagnostic', message => dispatch({type: 'log', level: 'info', text: message}));
    return () => {
      client.removeAllListeners();
      client.child.kill('SIGTERM');
    };
  }, [noBridge, setupOnly, initialState, config]);

  useEffect(() => {
    if (noBridge) return;
    if (setupOnly) {
      const runId = `setup_${Date.now().toString(36)}`;
      const client = spawnSetupStatus(runId);
      client.on('events', events => dispatch({type: 'bridge_events', events}));
      client.on('diagnostic', message => dispatch({type: 'log', level: 'info', text: message}));
      client.on('exit', (code, signal) => dispatch({type: 'bridge_exit', code, signal}));
      return () => {
        client.removeAllListeners();
        client.child.kill('SIGTERM');
      };
    }
    return undefined;
  }, [noBridge, setupOnly]);

  useEffect(() => {
    if (noBridge || state.setupRequestId <= lastSetupRequestRef.current) return;
    lastSetupRequestRef.current = state.setupRequestId;
    const runId = `setup_${Date.now().toString(36)}`;
    const client = spawnSetupStatus(runId);
    client.on('events', events => dispatch({type: 'bridge_events', events}));
    client.on('diagnostic', message => dispatch({type: 'log', level: 'info', text: message}));
    return () => {
      client.removeAllListeners();
      client.child.kill('SIGTERM');
    };
  }, [noBridge, setupOnly, state.setupRequestId]);

  useEffect(() => {
    const requestState = stateRef.current;
    if (noBridge || setupOnly || !requestState || state.runRequestId <= lastRunRequestRef.current || !requestState.config.keyword) return;
    lastRunRequestRef.current = state.runRequestId;
    const interpretation = interpretRef.current;
    if (interpretation) {
      interpretation.removeAllListeners();
      interpretation.child.kill('SIGTERM');
      interpretRef.current = null;
    }
    const suggestion = suggestRef.current;
    if (suggestion) {
      suggestion.removeAllListeners();
      suggestion.child.kill('SIGTERM');
      suggestRef.current = null;
    }
    const runId = `ui_${Date.now().toString(36)}`;
    dispatch({type: 'start_run', runId});
    const client: BridgeClient = spawnBridge(requestState.config, runId);
    runRef.current = client;
    client.on('events', events => dispatch({type: 'bridge_events', events}));
    client.on('diagnostic', message => dispatch({type: 'log', level: 'info', text: message}));
    client.on('exit', (code, signal) => {
      if (cancelKillTimerRef.current) {
        clearTimeout(cancelKillTimerRef.current);
        cancelKillTimerRef.current = null;
      }
      dispatch({type: 'bridge_exit', code, signal});
    });
    return () => {
      // Detach listeners BEFORE killing so a superseded run's exit can't clobber
      // the state of the run that replaced it.
      client.removeAllListeners();
      if (cancelKillTimerRef.current) {
        clearTimeout(cancelKillTimerRef.current);
        cancelKillTimerRef.current = null;
      }
      if (runRef.current === client) runRef.current = null;
      client.child.kill('SIGTERM');
    };
  }, [noBridge, setupOnly, state.runRequestId]);

  useEffect(() => {
    const requestState = stateRef.current;
    if (noBridge || !requestState || state.setupSaveRequestId <= lastSetupSaveRequestRef.current) return;
    lastSetupSaveRequestRef.current = state.setupSaveRequestId;
    const controller = new AbortController();
    void runSetupSaveAction(requestState, {signal: controller.signal}).then(result => {
      if (controller.signal.aborted) return;
      dispatch({type: 'bridge_events', events: result.events});
      if (result.diagnostic) dispatch({type: 'notify', tone: 'error', message: result.diagnostic});
      else dispatch({type: 'setup_save_complete'});
    });
    return () => {
      controller.abort();
    };
  }, [noBridge, state.setupSaveRequestId]);

  // Async save/publish runners. The bridge subcommand runs in its own short-lived child;
  // the UI stays responsive and failures surface as an error notification + failed status.
  useEffect(() => {
    const requestState = stateRef.current;
    if (noBridge || !requestState || state.saveRequestId <= lastSaveRequestRef.current) return;
    lastSaveRequestRef.current = state.saveRequestId;
    const controller = new AbortController();
    void runArtifactAction(requestState, 'save-artifact', {signal: controller.signal}).then(result => {
      if (controller.signal.aborted) return;
      dispatch({type: 'bridge_events', events: result.events});
      dispatch({type: 'action_finished', kind: 'save', ok: !result.diagnostic, message: result.diagnostic});
    });
    return () => {
      controller.abort();
    };
  }, [noBridge, state.saveRequestId]);

  useEffect(() => {
    const requestState = stateRef.current;
    if (noBridge || !requestState || state.publishRequestId <= lastPublishRequestRef.current) return;
    lastPublishRequestRef.current = state.publishRequestId;
    const controller = new AbortController();
    void runArtifactAction(requestState, 'publish-artifact', {signal: controller.signal}).then(result => {
      if (controller.signal.aborted) return;
      dispatch({type: 'bridge_events', events: result.events});
      dispatch({type: 'action_finished', kind: 'publish', ok: !result.diagnostic, message: result.diagnostic});
    });
    return () => {
      controller.abort();
    };
  }, [noBridge, state.publishRequestId]);

  useEffect(() => {
    const requestState = stateRef.current;
    if (noBridge || setupOnly || !requestState || state.interpretRequestId <= lastInterpretRequestRef.current || !requestState.config.keyword) return;
    lastInterpretRequestRef.current = state.interpretRequestId;
    const runId = `interp_${Date.now().toString(36)}`;
    const client = spawnKeywordInterpretation(requestState.config, runId, {feedback: requestState.interpretFeedback, previous: requestState.interpretPrevious});
    interpretRef.current = client;
    client.on('events', (events: BridgeEvent[]) => {
      dispatch({type: 'bridge_events', events});
      if (events.some(event => event.taskId === 'topic-interpretation' && (event.type === 'artifact_ready' || event.type === 'error'))) {
        setTimeout(() => {
          client.removeAllListeners();
          if (interpretRef.current === client) interpretRef.current = null;
        }, 0);
      }
    });
    client.on('diagnostic', message => dispatch({type: 'log', level: 'info', text: message}));
    client.on('exit', (code, signal) => {
      // Superseded clients never reach here (listeners detach in cleanup before kill).
      dispatch({type: 'interpret_finished', ok: code === 0});
      if (code && code !== 0) dispatch({type: 'notify', tone: 'error', message: `Topic interpretation exited ${code}${signal ? ` (${signal})` : ''}`});
    });
    return () => {
      client.removeAllListeners();
      client.child.kill('SIGTERM');
      if (interpretRef.current === client) interpretRef.current = null;
    };
  }, [noBridge, setupOnly, state.interpretRequestId, state.config.keyword]);

  // Keyword-ideas: unlike interpret, NOT gated on a non-empty keyword — ideas can be
  // requested straight from the blank landing input. Cleaned up when leaving keyword-ideas.
  const inKeywordIdeas = state.step === 'config' && state.configStage === 'keyword-ideas';
  useEffect(() => {
    const requestState = stateRef.current;
    if (noBridge || setupOnly || !inKeywordIdeas || !requestState || state.suggestRequestId <= lastSuggestRequestRef.current) return;
    lastSuggestRequestRef.current = state.suggestRequestId;
    const runId = `sugg_${Date.now().toString(36)}`;
    const client = spawnKeywordSuggestions(requestState.config, runId, {
      steer: requestState.suggestSteer,
      previous: requestState.suggestPrevious,
      trendsFresh: requestState.suggestTrendsFresh,
      mode: requestState.topicMode,
      target: 60,
    });
    suggestRef.current = client;
    client.on('events', (events: BridgeEvent[]) => {
      const cur = stateRef.current;
      if (suggestRef.current !== client || cur?.step !== 'config' || cur.configStage !== 'keyword-ideas') return;
      dispatch({type: 'bridge_events', events});
      if (events.some(event => event.taskId === 'keyword-suggestions' && (event.type === 'artifact_ready' || event.type === 'error'))) {
        setTimeout(() => {
          client.removeAllListeners();
          if (suggestRef.current === client) suggestRef.current = null;
        }, 0);
      }
    });
    client.on('diagnostic', message => dispatch({type: 'log', level: 'info', text: message}));
    client.on('exit', (code, signal) => {
      const cur = stateRef.current;
      if (suggestRef.current !== client || cur?.step !== 'config' || cur.configStage !== 'keyword-ideas') return;
      // Superseded clients never reach here (listeners detach in cleanup before kill).
      dispatch({type: 'suggest_finished', ok: code === 0});
      if (code && code !== 0) dispatch({type: 'notify', tone: 'error', message: `Keyword suggestions exited ${code}${signal ? ` (${signal})` : ''}`});
    });
    return () => {
      client.removeAllListeners();
      client.child.kill('SIGTERM');
      if (suggestRef.current === client) suggestRef.current = null;
    };
  }, [noBridge, setupOnly, inKeywordIdeas, state.suggestRequestId]);

  // A focused widget owns input whenever one of these is active; the global nav handler
  // is disabled then so it never competes with (or double-fires on) a keystroke.
  const captureActive =
    (!state.helpOpen &&
      !state.logOpen &&
      (state.step === 'setup' || state.step === 'config' || state.step === 'context' || state.step === 'review')) ||
    state.commandMode === 'comment' ||
    state.cancelPending ||
    state.publishArmed ||
    Boolean(state.gscGate);

  const navHandler = useCallback(
    (input: string, key: Key) => {
      const current = stateRef.current;
      if (!current) return;
      const binding = bindingsFor(current).find(candidate => candidate.action && candidate.match?.(input, key));
      if (!binding?.action) return;
      switch (binding.action) {
        case 'help':
          dispatch({type: 'toggle_help'});
          return;
        case 'logs':
          dispatch({type: 'toggle_log'});
          return;
        case 'back':
          if (current.helpOpen || current.logOpen) {
            dispatch({type: 'escape'});
          } else if (current.step === 'setup') {
            dispatch({type: 'set_step', step: current.returnStep});
          }
          return;
        case 'recheck_setup':
          dispatch({type: 'request_setup'});
          return;
        case 'cancel_run':
          dispatch({type: 'request_cancel'});
          return;
        case 'comment':
          dispatch({type: 'open_comment'});
          return;
        case 'save':
          dispatch({type: 'request_save'});
          return;
        case 'publish':
          // Publishing always confirms: arm it and (from preview) move to output where
          // the y/n confirm lives.
          dispatch({type: 'arm_publish'});
          if (current.step === 'preview') dispatch({type: 'goto_output'});
          return;
        case 'goto_output':
          dispatch({type: 'goto_output'});
          return;
        case 'goto_preview':
          dispatch({type: 'goto_preview'});
          return;
        case 'new_run':
          dispatch({type: 'new_run'});
          return;
        default:
          return;
      }
    },
    [dispatch],
  );

  useInput(navHandler, {isActive: !captureActive});

  // Double-Ctrl+C quit — always active, even while a widget owns input, so quitting
  // never depends on where focus is. Alt-screen restore lives in cli.tsx's exit path.
  const [quitArmed, setQuitArmed] = useState(false);
  const quitHandler = useMemo(() => createQuitHandler({exit, setArmed: setQuitArmed}), [exit]);
  useEffect(() => () => quitHandler.dispose(), [quitHandler]);
  useInput(quitHandler, {isActive: true});

  const submitComment = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      const current = stateRef.current;
      if (trimmed && current?.runId) runRef.current?.sendCommand({type: 'queue_comment', runId: current.runId, comment: trimmed});
      dispatch({type: 'comment_submitted', text: value});
    },
    [dispatch],
  );

  // GSC-gate decision: tell the bridge, then optimistically close the strip so the
  // UI never waits on the echo of the gsc_gate_closed event.
  const decideGscGate = useCallback(
    (decision: 'proceed' | 'abort') => {
      const current = stateRef.current;
      if (current?.runId) runRef.current?.sendCommand({type: 'gsc_decision', runId: current.runId, decision});
      dispatch({
        type: 'bridge_event',
        event: {
          type: 'gsc_gate_closed',
          runId: current?.runId ?? 'local',
          timestamp: new Date().toISOString(),
          phase: 'gsc',
          message: `GSC gate decision: ${decision}`,
        },
      });
      if (decision === 'abort') dispatch({type: 'notify', tone: 'info', message: 'Stopping run at GSC score gate…'});
    },
    [dispatch],
  );

  const confirmCancel = useCallback(() => {
    const current = stateRef.current;
    dispatch({type: 'dismiss_cancel'});
    const client = runRef.current;
    if (!client || !current?.runId) {
      // Nothing to signal (e.g. the child already died) — settle the UI directly.
      dispatch({type: 'bridge_exit', code: 130, signal: null});
      return;
    }
    const cancelledClient = client;
    cancelledClient.sendCommand({type: 'cancel', runId: current.runId});
    dispatch({type: 'notify', tone: 'info', message: 'Cancelling run…'});
    if (cancelKillTimerRef.current) {
      clearTimeout(cancelKillTimerRef.current);
      cancelKillTimerRef.current = null;
    }
    // If the bridge doesn't exit on its own within 3s, terminate only the cancelled client.
    cancelKillTimerRef.current = setTimeout(() => {
      cancelKillTimerRef.current = null;
      cancelledClient.child.kill('SIGTERM');
    }, 3000);
  }, [dispatch]);

  const overlayOpen = state.helpOpen || state.logOpen;

  // Height discipline: the root Box is pinned to ONE ROW LESS than the terminal height.
  // At exactly `rows`, Ink's renderer enters its fullscreen branch (lastOutputHeight >=
  // stdout.rows) and hard-clears the whole screen (\x1b[2J) on EVERY render — a visible
  // flash per keystroke on terminals without DEC-2026 synchronized updates (Tabby, older
  // VTE). One row of headroom keeps Ink on the flash-free incremental erase path.
  const measured = useTerminalSize();
  const {columns, rows} = size ?? measured;
  const frameColumns = Math.max(20, columns);
  const frameRows = Math.max(4, rows - 1);
  const showMasthead = mastheadVisible(state, rows, frameColumns);
  const headerRows = showMasthead ? ANSI_LOGO_LINES.length + 2 : 1; // logo + margin + journey line
  const noticeRows = Math.min(state.notifications.length, 2);
  const bodyRows = Math.max(4, frameRows - headerRows - noticeRows - 3 - 1); // panel border+title, footer
  const layout = useMemo(
    () => ({columns: frameColumns, rows, bodyRows}),
    [frameColumns, rows, bodyRows],
  );

  const stepView =
    state.step === 'setup' ? (
      <SetupView state={state} dispatch={dispatch} noBridge={noBridge} setupOnly={setupOnly} onExit={() => exit()} />
    ) : state.step === 'config' ? (
      <ConfigFlow state={state} dispatch={dispatch} onExit={() => exit()} />
    ) : state.step === 'context' ? (
      <ContextView state={state} dispatch={dispatch} />
    ) : state.step === 'running' ? (
      <RunningView
        state={state}
        onSubmitComment={submitComment}
        onCancelComment={() => dispatch({type: 'escape'})}
        onConfirmCancel={confirmCancel}
        onDismissCancel={() => dispatch({type: 'dismiss_cancel'})}
        onGscDecision={decideGscGate}
      />
    ) : state.step === 'review' ? (
      <ReviewGateView state={state} dispatch={dispatch} sendCommand={command => runRef.current?.sendCommand(command)} />
    ) : state.step === 'preview' ? (
      <PreviewView state={state} active={!overlayOpen} />
    ) : (
      <OutputView state={state} dispatch={dispatch} onQuit={() => exit()} />
    );

  return (
    <LayoutContext.Provider value={layout}>
      <Box flexDirection="column" height={frameRows} width={frameColumns} overflowX="hidden">
        <HeaderBar state={state} showMasthead={showMasthead} />
        <Notices state={state} />
        <Box flexGrow={1} flexShrink={1} flexDirection="column" overflowY="hidden">
          <Panel title={state.helpOpen ? 'Help' : state.logOpen ? 'Logs' : STEP_TITLE[state.step]}>
            {state.helpOpen ? <HelpOverlay state={state} /> : state.logOpen ? <LogDrawer state={state} /> : stepView}
          </Panel>
        </Box>
        {quitArmed ? <QuitNotice /> : <FooterBar state={state} />}
      </Box>
    </LayoutContext.Provider>
  );
};

// Replaces the footer line while quit is armed so the frame height never changes.
// flexShrink=0 matches FooterBar: chrome never absorbs body overflow.
const QuitNotice = () => {
  const theme = useTheme();
  return (
    <Box flexShrink={0}>
      <Text color={theme.colors.warning}>press Ctrl+C again to quit</Text>
    </Box>
  );
};

export const App = ({themeName = 'theme-flyweel', ...props}: AppProps) => {
  const theme = getTheme(themeName);
  return (
    <InkUIThemeProvider theme={buildInkUITheme(theme)}>
      <ThemeProvider theme={theme}>
        <InnerApp {...props} />
      </ThemeProvider>
    </InkUIThemeProvider>
  );
};
