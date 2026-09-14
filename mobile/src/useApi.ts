import { useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { AppState } from 'react-native';

/** What went wrong, in words someone holding the phone can act on. */
export function explain(error: unknown): string {
  // fetch rejects with a TypeError when there is no route to the host at all,
  // which away from home almost always means WireGuard is off.
  if (error instanceof TypeError) {
    return "Can't reach the NUC. Away from home? Turn on WireGuard.";
  }
  return error instanceof Error ? error.message : String(error);
}

/**
 * Load whenever the screen comes into focus. With `refreshMs`, also on a timer
 * and on returning to the foreground, while the screen stays focused: "Checked
 * 2 min ago" left on screen for an hour reads as the scheduler having died.
 *
 * `load` must be memoised, or this refetches on every render.
 */
export function useApi<T>(load: () => Promise<T>, refreshMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setData(await load());
      setError(null);
    } catch (e) {
      setError(explain(e));
    }
  }, [load]);

  useFocusEffect(
    useCallback(() => {
      refresh();
      if (!refreshMs) {
        return;
      }
      const timer = setInterval(() => {
        if (AppState.currentState === 'active') {
          refresh();
        }
      }, refreshMs);
      const subscription = AppState.addEventListener('change', (state) => {
        if (state === 'active') {
          refresh();
        }
      });
      return () => {
        clearInterval(timer);
        subscription.remove();
      };
    }, [refresh, refreshMs]),
  );

  return { data, setData, error, refresh };
}
