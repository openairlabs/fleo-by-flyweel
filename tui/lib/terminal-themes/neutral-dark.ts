import {createTheme} from './theme.js';

export const neutralDarkTheme = createTheme({
  name: 'neutral-dark',
  colors: {
    primary: '#f5f5f5',
    accent: '#a3e635',
    success: '#86efac',
    warning: '#fde047',
    error: '#f87171',
    info: '#67e8f9',
    mutedForeground: '#a3a3a3',
    selectionForeground: '#0a0a0a',
    focusRing: '#a3a3a3',
  },
  border: {style: 'round', color: '#525252'},
});
