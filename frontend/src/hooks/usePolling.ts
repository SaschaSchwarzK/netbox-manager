import { useEffect, useRef } from "react";

interface PollingOptions<T> {
  enabled: boolean;
  pollKey: string;
  initialValue: T;
  poll: () => Promise<T>;
  isTerminal: (value: T) => boolean;
  onValue: (value: T, previous: T) => void;
}

export function usePolling<T>({ enabled, pollKey, initialValue, poll, isTerminal, onValue }: PollingOptions<T>) {
  const pollRef = useRef(poll);
  const terminalRef = useRef(isTerminal);
  const valueRef = useRef(onValue);
  pollRef.current = poll;
  terminalRef.current = isTerminal;
  valueRef.current = onValue;

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    let timer: number | null = null;
    let delay = 1000;
    let previous = initialValue;
    let lastSignature = JSON.stringify(initialValue);

    const schedule = (wait: number) => {
      if (!active || document.hidden) return;
      timer = window.setTimeout(tick, wait);
    };
    const tick = async () => {
      if (!active || document.hidden) return;
      try {
        const updated = await pollRef.current();
        if (!active) return;
        const signature = JSON.stringify(updated);
        delay = signature === lastSignature ? Math.min(5000, delay + 1000) : 1000;
        lastSignature = signature;
        valueRef.current(updated, previous);
        previous = updated;
        if (!terminalRef.current(updated)) schedule(delay);
      } catch {
        delay = Math.min(5000, delay + 1000);
        schedule(delay);
      }
    };
    const visibilityChanged = () => {
      if (document.hidden) {
        if (timer !== null) window.clearTimeout(timer);
        timer = null;
      } else {
        schedule(0);
      }
    };
    document.addEventListener("visibilitychange", visibilityChanged);
    schedule(1000);
    return () => {
      active = false;
      document.removeEventListener("visibilitychange", visibilityChanged);
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [enabled, pollKey]);
}
