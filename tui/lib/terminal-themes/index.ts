import {flyweelTheme} from './flyweel.js';
import {highContrastTheme} from './high-contrast.js';
import {neutralDarkTheme} from './neutral-dark.js';
import {opencodeTheme} from './opencode.js';
import type {Theme} from './theme.js';

export const themes: Record<string, Theme> = {
  'theme-opencode': opencodeTheme,
  'theme-flyweel': flyweelTheme,
  'theme-high-contrast': highContrastTheme,
  'theme-neutral-dark': neutralDarkTheme,
};

export const getTheme = (name = 'theme-flyweel'): Theme => themes[name] ?? flyweelTheme;
