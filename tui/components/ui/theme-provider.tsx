import * as React from 'react';

import {flyweelTheme} from '../../lib/terminal-themes/flyweel.js';
import type {Theme} from '../../lib/terminal-themes/theme.js';

export type {Theme} from '../../lib/terminal-themes/theme.js';
export {createTheme} from '../../lib/terminal-themes/theme.js';

const ThemeContext = React.createContext<Theme>(flyweelTheme);

export interface ThemeProviderProps {
  children: React.ReactNode;
  theme?: Theme;
}

export const ThemeProvider = ({children, theme = flyweelTheme}: ThemeProviderProps) => (
  <ThemeContext.Provider value={theme}>{children}</ThemeContext.Provider>
);

export const useTheme = (): Theme => React.useContext(ThemeContext);
