import { useEffect, useRef } from "react";

interface PollingOptions {
  /** Restarting value: changing it tears the loop down and starts a fresh one. */
  key?: string;
  intervalMs?: number;
  /** Backed-off cadence used while the tab is in the background. */
  hiddenIntervalMs?: number;
}

/**
 * Run `task` on an interval while `active`.
 *
 * A rejected task never stops the loop. Migration state is durable in the
 * backend, so a transient localhost hiccup must degrade to "the panel updates
 * a moment later", not to "the run looks dead".
 */
export function usePolling(
  task: () => Promise<unknown>,
  active: boolean,
  { key = "", intervalMs = 2_000, hiddenIntervalMs = 5_000 }: PollingOptions = {}
): void {
  // Kept in a ref so a new closure each render does not restart the timer.
  const latest = useRef(task);
  latest.current = task;

  useEffect(() => {
    if (!active) return;
    let alive = true;
    let timer = 0;

    const tick = async () => {
      try {
        await latest.current();
      } catch {
        // Observational only — swallow and retry on the next tick.
      } finally {
        if (alive) {
          timer = window.setTimeout(tick, document.hidden ? hiddenIntervalMs : intervalMs);
        }
      }
    };

    void tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [active, key, intervalMs, hiddenIntervalMs]);
}
