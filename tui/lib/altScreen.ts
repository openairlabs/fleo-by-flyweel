// Alternate-screen lifecycle for the full-screen app. Ink 6 has no alternateScreen
// render option, so the CLI enters/leaves ?1049 itself. Extracted here (stream and
// process injectable) so teardown is unit-testable without a TTY.
export const ALT_SCREEN_ENTER = '\u001b[?1049h\u001b[H';
export const ALT_SCREEN_LEAVE = '\u001b[?1049l';

interface WritableLike {
  write: (chunk: string) => unknown;
}

interface ProcessLike {
  on: (event: 'exit' | NodeJS.Signals, listener: () => void) => unknown;
  removeListener: (event: 'exit' | NodeJS.Signals, listener: () => void) => unknown;
  exit: (code?: number) => void;
}

// Signals that would otherwise kill the process without firing 'exit' handlers
// (signal-exit re-raises them after Ink's unmount, skipping 'exit'). Mapped to the
// conventional 128+signum exit codes.
const SIGNAL_EXIT_CODES: Array<[NodeJS.Signals, number]> = [
  ['SIGHUP', 129],
  ['SIGINT', 130],
  ['SIGTERM', 143],
];

// Enters the alternate screen and returns an idempotent leave(). The leave is also
// registered on process 'exit' plus the fatal signals above — a stranded ?1049h
// corrupts the user's terminal, so it restores exactly once no matter which path
// fires first (finally, exit handler, or signal).
export const enterAltScreen = (stream: WritableLike, proc: ProcessLike = process): (() => void) => {
  stream.write(ALT_SCREEN_ENTER);
  let restored = false;
  const signalHandlers = SIGNAL_EXIT_CODES.map(([signal, code]): [NodeJS.Signals, () => void] => [
    signal,
    () => {
      leave();
      proc.exit(code);
    },
  ]);
  const detach = (): void => {
    proc.removeListener('exit', leave);
    for (const [signal, handler] of signalHandlers) proc.removeListener(signal, handler);
  };
  const leave = (): void => {
    if (restored) return;
    restored = true;
    detach();
    stream.write(ALT_SCREEN_LEAVE);
  };
  proc.on('exit', leave);
  for (const [signal, handler] of signalHandlers) proc.on(signal, handler);
  return leave;
};
