// Terminal theme model. Nine color tokens — exactly the set the TUI actually reads —
// plus one border style/color pair. Anything fancier died in the 2026-07 dead-code purge.
export type BorderStyle = 'single' | 'double' | 'round' | 'bold' | 'singleDouble' | 'doubleSingle' | 'classic';

export interface ThemeColors {
  primary: string;
  accent: string;
  success: string;
  warning: string;
  error: string;
  info: string;
  mutedForeground: string;
  // Inverse text rendered on top of a primary/inverse block (active journey step).
  selectionForeground: string;
  focusRing: string;
}

export interface Theme {
  name: string;
  colors: ThemeColors;
  border: {style: BorderStyle; color: string};
}

import {opencodeTheme} from './opencode.js';

export const createTheme = (overrides: Partial<Theme> & {name: string}): Theme => ({
  ...opencodeTheme,
  ...overrides,
  colors: {...opencodeTheme.colors, ...overrides.colors},
  border: {...opencodeTheme.border, ...overrides.border},
});
