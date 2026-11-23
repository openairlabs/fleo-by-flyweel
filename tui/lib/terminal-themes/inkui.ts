import {defaultTheme as inkUIDefaultTheme, extendTheme} from '@inkjs/ui';
import type {Theme as InkUITheme} from '@inkjs/ui';

import type {Theme} from './theme.js';

// Map our terminal theme tokens onto @inkjs/ui's component themes so Select/TextInput/
// Spinner/ProgressBar/ConfirmInput follow --theme instead of the library's hardcoded
// blue/green palette. Style keys verified against @inkjs/ui v2 build/components/*/theme.js.
export const buildInkUITheme = (theme: Theme): InkUITheme =>
  extendTheme(inkUIDefaultTheme, {
    components: {
      Select: {
        styles: {
          focusIndicator: () => ({color: theme.colors.focusRing}),
          selectedIndicator: () => ({color: theme.colors.primary}),
          label({isFocused, isSelected}: {isFocused?: boolean; isSelected?: boolean} = {}) {
            let color: string | undefined;
            if (isSelected) color = theme.colors.primary;
            if (isFocused) color = theme.colors.focusRing;
            return {color};
          },
          highlightedText: () => ({bold: true, color: theme.colors.accent}),
        },
      },
      Spinner: {
        styles: {
          frame: () => ({color: theme.colors.accent}),
        },
      },
      ProgressBar: {
        styles: {
          completed: () => ({color: theme.colors.primary}),
        },
      },
    },
  });
