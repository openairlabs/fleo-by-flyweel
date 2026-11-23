import type {Key as InkKey} from 'ink';

import {hasFinalArtifact} from './state.js';
import type {AppState, JourneyStep} from './types.js';

// Single source of truth for global key bindings. Three consumers render/act from it:
// App's nav handler (action dispatch), FooterBar (hints), and HelpOverlay (reference).
//
// Esc precedence (codified here + in App):
//   1. an open overlay (help / log drawer) closes
//   2. a pending cancel-confirm dismisses (ConfirmInput owns input then)
//   3. a focused widget owns its own Esc (config flow, comment bar, review gate)
//   4. step-level: back (setup) or cancel run (running)
export type KeyActionId =
  | 'help'
  | 'logs'
  | 'back'
  | 'comment'
  | 'save'
  | 'publish'
  | 'cancel_run'
  | 'goto_output'
  | 'goto_preview'
  | 'new_run'
  | 'recheck_setup';

export interface KeyBinding {
  keyLabel: string;
  label: string;
  // undefined action ⇒ display-only entry documenting widget-owned keys.
  action?: KeyActionId;
  match?: (input: string, key: InkKey) => boolean;
  steps: JourneyStep[] | 'all';
  when?: (state: AppState) => boolean;
}

const canPreview = (state: AppState): boolean => hasFinalArtifact(state);

export const KEYMAP: KeyBinding[] = [
  // Step-scoped actions (order matters: first match wins in the nav handler).
  {keyLabel: 'Esc', label: 'back', action: 'back', match: (_input, key) => key.escape, steps: ['setup']},
  {keyLabel: 'r', label: 're-check', action: 'recheck_setup', match: input => input === 'r', steps: ['setup']},
  {keyLabel: 'Esc', label: 'cancel run', action: 'cancel_run', match: (_input, key) => key.escape, steps: ['running']},
  {keyLabel: 'c', label: 'comment', action: 'comment', match: (input, key) => input === 'c' && !key.ctrl, steps: ['running'], when: state => !state.gscGate},
  // Display-only: the GSC-gate ConfirmInput owns y/n while the gate strip is up.
  {keyLabel: 'y/n', label: 'GSC gate', steps: ['running'], when: state => Boolean(state.gscGate)},
  {keyLabel: 's', label: 'save', action: 'save', match: input => input === 's', steps: ['preview', 'output'], when: canPreview},
  {keyLabel: 'P', label: 'publish', action: 'publish', match: input => input === 'P', steps: ['preview', 'output'], when: canPreview},
  // !key.ctrl guards keep ^O (full content) and ^P (its preview alias) from also
  // firing these plain-letter navigations.
  {keyLabel: 'o', label: 'output', action: 'goto_output', match: (input, key) => input === 'o' && !key.ctrl, steps: ['preview']},
  {keyLabel: 'p', label: 'preview', action: 'goto_preview', match: (input, key) => input === 'p' && !key.ctrl, steps: ['output'], when: canPreview},
  {keyLabel: 'n', label: 'new article', action: 'new_run', match: input => input === 'n', steps: ['output']},

  // Global actions. Quit is double-Ctrl+C, handled by App's always-active handler —
  // the ^C entry here is display-only.
  {keyLabel: 'l', label: 'logs', action: 'logs', match: input => input === 'l', steps: 'all'},
  {keyLabel: '?', label: 'help', action: 'help', match: input => input === '?', steps: 'all'},
  {keyLabel: 'Ctrl+C', label: 'quit', steps: 'all'},

  // Display-only entries documenting keys owned by the focused widget on each step.
  // Guarded off the keyword-ideas side-trip so its own rows (below) don't duplicate.
  {keyLabel: '↑↓', label: 'move', steps: ['config'], when: state => state.configStage !== 'keyword-ideas'},
  {keyLabel: 'Enter', label: 'select', steps: ['config'], when: state => state.configStage !== 'keyword-ideas'},
  {keyLabel: 'Esc', label: 'back', steps: ['config'], when: state => state.configStage !== 'keyword-ideas'},
  // Ctrl+G opens the AI keyword-ideas side-trip from any config stage. All Ctrl chords
  // are labeled "Ctrl+X" (never ^X caret notation) so they read unambiguously.
  {keyLabel: 'Ctrl+G', label: 'AI keyword ideas', steps: ['config'], when: state => state.configStage !== 'keyword-ideas'},
  {keyLabel: 'Ctrl+S', label: 'setup / brand', steps: ['config'], when: state => state.configStage !== 'keyword-ideas'},
  // Tab flips the angle/keyword framing chip; Ctrl+T flips the trends preference — both
  // owned by ConfigFlow's useInput on every config stage but the ideas side-trip.
  {keyLabel: 'Tab', label: 'keyword/angle mode', steps: ['config'], when: state => state.configStage !== 'keyword-ideas'},
  {keyLabel: 'Ctrl+T', label: 'trends fast/fresh', steps: ['config'], when: state => state.configStage !== 'keyword-ideas'},
  // Keyword-ideas side-trip: the view owns all its own keys.
  {keyLabel: '↑↓', label: 'move', steps: ['config'], when: state => state.configStage === 'keyword-ideas'},
  {keyLabel: 'Enter', label: 'pick', steps: ['config'], when: state => state.configStage === 'keyword-ideas'},
  {keyLabel: 'type', label: 'steer', steps: ['config'], when: state => state.configStage === 'keyword-ideas'},
  {keyLabel: 'Ctrl+N', label: 'regenerate', steps: ['config'], when: state => state.configStage === 'keyword-ideas'},
  {keyLabel: 'Ctrl+T', label: 'trends fast/fresh', steps: ['config'], when: state => state.configStage === 'keyword-ideas'},
  {keyLabel: 'Esc', label: 'back', steps: ['config'], when: state => state.configStage === 'keyword-ideas'},
  {keyLabel: 'Enter', label: 'start', steps: ['context']},
  {keyLabel: 'type', label: 'steer', steps: ['context']},
  {keyLabel: 'Ctrl+N', label: 'regenerate', steps: ['context']},
  {keyLabel: 'Esc', label: 'back', steps: ['context']},
  {keyLabel: '↑↓', label: 'choose', steps: ['review']},
  {keyLabel: 'Enter', label: 'decide', steps: ['review']},
  {keyLabel: '↑↓ PgUp/PgDn', label: 'scroll', steps: ['preview']},
  {keyLabel: 'i', label: 'info', steps: ['preview']},
  // ^O expands the content (pre-polish draft / final article) to the full panel.
  // ^P stays as a silent alias on preview; both toggles live in the step views.
  {keyLabel: 'Ctrl+O', label: 'full content', steps: ['review', 'preview']},
];

// Bindings shown/active for the current state. When an overlay is open it owns the
// screen, so only the overlay bindings apply.
export const bindingsFor = (state: AppState): KeyBinding[] => {
  if (state.helpOpen || state.logOpen) {
    return [
      {keyLabel: 'Esc', label: 'close', action: 'back', match: (_input, key) => key.escape, steps: 'all'},
      {keyLabel: '?', label: 'help', action: 'help', match: input => input === '?', steps: 'all'},
      {keyLabel: 'l', label: 'logs', action: 'logs', match: input => input === 'l', steps: 'all'},
      {keyLabel: 'Ctrl+C', label: 'quit', steps: 'all'},
    ];
  }
  return KEYMAP.filter(binding => {
    if (binding.steps !== 'all' && !binding.steps.includes(state.step)) return false;
    return binding.when ? binding.when(state) : true;
  });
};
