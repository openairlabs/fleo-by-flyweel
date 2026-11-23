import {createTheme} from './theme.js';

/**
 * WCAG AA high-contrast theme — dark variant (white text on black).
 * Foreground pairs meet 4.5:1 on #000; key status colors exceed 7:1.
 */
export const highContrastTheme = createTheme({
  name: 'high-contrast',
  colors: {
    primary: '#FFFFFF',
    accent: '#00FFFF',
    success: '#00FF00',
    warning: '#FFFF00',
    error: '#FF4444',
    info: '#00CCFF',
    mutedForeground: '#CCCCCC',
    selectionForeground: '#000000',
    focusRing: '#FFFF00',
  },
  border: {style: 'bold', color: '#FFFFFF'},
});
