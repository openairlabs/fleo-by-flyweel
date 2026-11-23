import {createContext, useContext, useEffect, useState} from 'react';

export interface TerminalSize {
  columns: number;
  rows: number;
}

interface SizedStream {
  columns?: number;
  rows?: number;
  on: (event: 'resize', listener: () => void) => unknown;
  off: (event: 'resize', listener: () => void) => unknown;
}

// 80x24 fallbacks cover non-TTY stdout (tests, pipes) where columns/rows are undefined
// — and degenerate ptys that report 0x0 (|| rather than ?? on purpose).
export const readTerminalSize = (stream?: {columns?: number; rows?: number}): TerminalSize => ({
  columns: stream?.columns || 80,
  rows: stream?.rows || 24,
});

export const useTerminalSize = (stream: SizedStream = process.stdout): TerminalSize => {
  const [size, setSize] = useState<TerminalSize>(() => readTerminalSize(stream));
  useEffect(() => {
    const onResize = () => setSize(readTerminalSize(stream));
    stream.on('resize', onResize);
    return () => {
      stream.off('resize', onResize);
    };
  }, [stream]);
  return size;
};

// Layout budget computed once at the App root: bodyRows is the vertical space a step
// view may use inside the panel (rows minus header, notices, panel border+title, footer).
// Views consume it via useLayout() so frames never exceed the terminal height.
export interface Layout extends TerminalSize {
  bodyRows: number;
}

export const defaultLayout: Layout = {...readTerminalSize(), bodyRows: 16};

export const LayoutContext = createContext<Layout>(defaultLayout);

export const useLayout = (): Layout => useContext(LayoutContext);
