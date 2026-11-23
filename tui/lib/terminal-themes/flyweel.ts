import {createTheme} from './theme.js';

export const flyweelTheme = createTheme({
  name: 'flyweel',
  colors: {
    primary: '#90DAB8',
    accent: '#A4FED3',
    success: '#74BD96',
    warning: '#FB923C',
    error: '#FF8A8A',
    info: '#8FD7FF',
    mutedForeground: '#D0D7D3',
    selectionForeground: '#90DAB8',
    focusRing: '#90DAB8',
  },
  border: {style: 'round', color: '#2D6B4A'},
});
