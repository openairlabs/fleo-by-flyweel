export interface QuitHandlerOptions {
  exit: () => void;
  // Notifies the UI so it can show/hide the "press Ctrl+C again to quit" notice.
  setArmed: (armed: boolean) => void;
  windowMs?: number;
}

export type QuitHandler = ((input: string, key: {ctrl: boolean}) => void) & {dispose: () => void};

// Claude-Code-style double-Ctrl+C quit: the first press arms for `windowMs`, a second
// press inside the window exits. Kept out of React so the timing logic is unit-testable;
// App wires the returned handler into an always-active useInput. Alt-screen restore is
// owned by cli.tsx's exit path — this never writes escape codes.
export const createQuitHandler = ({exit, setArmed, windowMs = 2000}: QuitHandlerOptions): QuitHandler => {
  let armed = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const disarm = () => {
    armed = false;
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    setArmed(false);
  };

  return Object.assign(
    (input: string, key: {ctrl: boolean}): void => {
      if (!(key.ctrl && input === 'c')) return;
      if (armed) {
        disarm();
        exit();
        return;
      }
      armed = true;
      setArmed(true);
      timer = setTimeout(disarm, windowMs);
    },
    {
      // Cleanup for unmount: cancels the pending disarm timer.
      dispose: () => {
        if (timer) clearTimeout(timer);
        timer = null;
      },
    },
  );
};
