import React, {useEffect, useState} from 'react';
import {Box, Text, useInput} from 'ink';
import {TextInput} from './textInput.js';

import {OptionList} from './termcn.js';
import {SPINNER_FRAMES} from './todo.js';
import {useLayout} from '../lib/useTerminalSize.js';
import {useTheme} from './ui/theme-provider.js';
import type {AppAction, AppState, KeywordSuggestion} from '../types.js';

// 60 = the traffic-potential target the bridge is asked for (App's suggest effect
// passes target: 60) — scores at/above it earn the ✓ badge.
const TARGET_SCORE = 60;

// Chrome around the list body, in rendered lines: header (1) + list marginTop gap (1) +
// detail strip (1) + note (1) + steer marginTop gap (1) + steer input (1) + hint (1) = 7,
// plus config.tsx's own "Generation Config" title (1) that sits above us → subtract 8 so
// the whole config body stays inside the bodyRows budget and the frame height never grows.
const CHROME_OVERHEAD = 8;

const scoreBadge = (suggestion: KeywordSuggestion): string => {
  if (suggestion.score === null) return 'unscored';
  if (suggestion.estimated) return `~${suggestion.score}% est`;
  return suggestion.score >= TARGET_SCORE ? `${suggestion.score}% ✓` : `${suggestion.score}%`;
};

// The mode-primary label for a suggestion: the angle (title-seed with a point of view,
// falling back to the keyword) in angle mode, the raw keyword otherwise. This is both the
// row's headline and the exact string picking dispatches into config.keyword.
const primaryText = (suggestion: KeywordSuggestion, mode: AppState['topicMode']): string =>
  mode === 'angle' ? suggestion.angle?.trim() || suggestion.keyword : suggestion.keyword;

// The dim, single-line detail strip for the focused idea: its secondary field (the keyword
// in angle mode, the angle in keyword mode) + expected traffic + rationale. Rationale is
// surfaced nowhere else, so this is the only place a user sees WHY an idea was suggested.
const detailText = (suggestion: KeywordSuggestion, mode: AppState['topicMode']): string => {
  const traffic = suggestion.trafficPotential != null ? `≈${suggestion.trafficPotential} clicks/mo` : 'unscored';
  const lead = mode === 'angle' ? `kw: ${suggestion.keyword}` : `angle: ${suggestion.angle?.trim() || '—'}`;
  const tail = [traffic, suggestion.rationale?.trim()].filter(Boolean).join(' · ');
  return `${lead} · ${tail}`;
};

// Keyword ideas: an alternative on the keyword stage. Ctrl+G there fetches 5 AI ideas
// (GSC-scored, angle-framed by default) instead of typing one; unlimited retries, a steer
// text input, and a fast/fresh trends toggle. Modeled on ContextView's take/steer pattern
// — this view is the single keyboard owner while active.
//
// Height discipline (the whole point of this rewrite): the vertical skeleton is FIXED — a
// header line, a list body of exactly `viewport` lines (padded with blanks in every state),
// a detail strip, a note line, and an always-mounted steer block. Because the rendered line
// count is identical across running / loaded / failed / empty, Ink never has to clear+redraw
// the panel, so the container border no longer flickers while ideas generate. The spinner is
// a manual in-place glyph swap (never @inkjs/ui Spinner, whose own render loop forces the
// full-clear that caused the flicker).
export const KeywordIdeasView = ({state, dispatch}: {state: AppState; dispatch: React.Dispatch<AppAction>}) => {
  const theme = useTheme();
  const {bodyRows} = useLayout();

  // Mirrors the TextInput's own value so the OptionList's arrow/Enter capture can be
  // disabled while the user is mid-steer — otherwise Enter would both submit the steer
  // text AND select whatever row happens to be highlighted.
  const [steerDraft, setSteerDraft] = useState('');
  // Only the focused index (a primitive) lives in state — never the suggestion object —
  // so OptionList's onFocusChange can't spin a re-render loop.
  const [focusIndex, setFocusIndex] = useState(0);
  useEffect(() => {
    setSteerDraft('');
    setFocusIndex(0);
  }, [state.suggestRequestId]);

  const running = state.suggestState === 'running';
  const failed = state.suggestState === 'failed';
  const suggestions = state.keywordSuggestions ?? [];
  const take = state.suggestAttempt > 1 ? ` · take ${state.suggestAttempt}` : '';
  const seed = state.config.keyword.trim() || '(blank)';

  // Manual spinner tick: a numeric interval bumped every 250ms, glyph swapped in place.
  // Deliberately NOT @inkjs/ui Spinner — see the header comment.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const interval = setInterval(() => setTick(current => current + 1), 250);
    return () => clearInterval(interval);
  }, [running]);

  useInput((input, key) => {
    if (key.escape) {
      dispatch({type: 'set_config_stage', stage: 'keyword'});
      return;
    }
    if (key.ctrl && input === 'n') {
      dispatch({type: 'request_suggest'});
      return;
    }
    if (key.ctrl && input === 't') {
      dispatch({type: 'toggle_suggest_trends'});
    }
  });

  const viewport = Math.max(3, bodyRows - CHROME_OVERHEAD);

  const options = suggestions.map(suggestion => ({
    value: primaryText(suggestion, state.topicMode),
    label: `${primaryText(suggestion, state.topicMode)}  ${scoreBadge(suggestion)}`,
  }));

  const showList = !running && !failed && options.length > 0;
  // The list body is ALWAYS exactly `viewport` lines: real content, then blank padding.
  // (When windowed — options longer than the viewport — OptionList fills the viewport
  // itself; otherwise it renders one line per option and we pad the remainder.)
  const contentLines = showList ? Math.min(options.length, viewport) : 1;
  const pad = Array.from({length: Math.max(0, viewport - contentLines)}, (_, index) => <Text key={`pad-${index}`}> </Text>);

  const focused = suggestions[focusIndex];
  const detail = showList && focused ? detailText(focused, state.topicMode) : ' ';

  const listBody = running ? (
    <Text color={theme.colors.accent}>
      {SPINNER_FRAMES[tick % SPINNER_FRAMES.length]} Finding high-opportunity ideas…
    </Text>
  ) : failed ? (
    <Text color={theme.colors.error}> Keyword ideas failed — Ctrl+N to retry, Esc back.</Text>
  ) : options.length > 0 ? (
    <OptionList
      key={`ideas-${state.suggestRequestId}`}
      options={options}
      viewport={viewport}
      isActive={steerDraft.trim().length === 0}
      onFocusChange={(_value, index) => setFocusIndex(index)}
      onSelect={(_value, index) => {
        const pick = suggestions[index];
        if (pick) dispatch({type: 'pick_suggestion', keyword: primaryText(pick, state.topicMode)});
      }}
    />
  ) : (
    <Text color={theme.colors.mutedForeground}>No ideas yet — Ctrl+N to generate.</Text>
  );

  return (
    <Box flexDirection="column">
      <Box>
        <Text color={theme.colors.primary} bold>
          Keyword Ideas
        </Text>
        <Text color={theme.colors.mutedForeground}>
          {'  '}
          {seed} · {state.config.style}
          {take} · trends: {state.suggestTrendsFresh ? 'fresh' : 'fast'}
        </Text>
      </Box>
      <Box marginTop={1} flexDirection="column">
        {listBody}
        {pad}
      </Box>
      <Box>
        <Text color={theme.colors.mutedForeground} wrap="truncate-end">
          {detail}
        </Text>
      </Box>
      <Box>
        {state.suggestGscAvailable === false ? (
          <Text color={theme.colors.warning}>GSC unavailable — ideas unscored</Text>
        ) : (
          <Text> </Text>
        )}
      </Box>
      {/* Steer block is ALWAYS mounted so the frame height stays constant. Submitting a
          steer while a fetch is in flight restarts it steered — the same semantics Ctrl+N
          already has mid-run — and the requestId bump remounts the input cleanly (a
          guard-and-ignore here would strand a stale draft that keeps the list inactive). */}
      <Box marginTop={1} flexDirection="column">
        <TextInput
          key={`steer-${state.suggestRequestId}`}
          placeholder="type a broad topic to steer, Ctrl+N for a fresh set…"
          onChange={setSteerDraft}
          onSubmit={value => {
            const steer = value.trim();
            setSteerDraft('');
            if (steer) dispatch({type: 'request_suggest', steer});
          }}
        />
        <Text color={theme.colors.mutedForeground}>↑↓ move · Enter pick · type+Enter steer · Ctrl+N regenerate · Ctrl+T trends · Esc back</Text>
      </Box>
    </Box>
  );
};
