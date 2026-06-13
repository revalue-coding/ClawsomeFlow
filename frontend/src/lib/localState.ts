import { Dispatch, SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from "react";

/**
 * localStorage-backed React state — the durable sibling of
 * {@link useSessionBackedState}. Unlike sessionStorage (per-tab, cleared on tab
 * close), localStorage survives a tab close + reopen, so this is what long
 * operations use to remember an in-flight op id ("active op pointer") that must
 * be recoverable after the user closes and reopens the page.
 */

const LOCAL_KEY_PREFIX = "csflow:op-state:";

type LocalStateOptions<T> = {
  // When this returns true for the current value, the key is removed from
  // storage (e.g. the op pointer is cleared once the op reaches a terminal
  // state) so a future visit starts clean.
  isClosed?: (value: T) => boolean;
};

function readLocalValue<T>(storageKey: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function persistLocalValue<T>(storageKey: string, value: T, isClosed?: (value: T) => boolean): void {
  try {
    let closed = false;
    try {
      closed = Boolean(isClosed?.(value));
    } catch {
      closed = false;
    }
    if (closed) {
      window.localStorage.removeItem(storageKey);
      return;
    }
    window.localStorage.setItem(storageKey, JSON.stringify(value));
  } catch {
    /* localStorage disabled / quota — ignore */
  }
}

export function useLocalStorageBackedState<T>(
  key: string,
  initialValue: T,
  options?: LocalStateOptions<T>,
): [T, Dispatch<SetStateAction<T>>] {
  const storageKey = useMemo(() => `${LOCAL_KEY_PREFIX}${key}`, [key]);
  const isClosed = options?.isClosed;
  // Keep isClosed behind a ref so the returned setter stays referentially
  // stable even when callers pass an inline arrow (mirrors useSessionBackedState).
  const isClosedRef = useRef(isClosed);
  const initialRef = useRef(initialValue);
  const [value, setValue] = useState<T>(() => readLocalValue(storageKey, initialValue));
  const valueRef = useRef(value);

  useEffect(() => {
    isClosedRef.current = isClosed;
  }, [isClosed]);

  useEffect(() => {
    valueRef.current = value;
  }, [value]);

  useEffect(() => {
    const next = readLocalValue(storageKey, initialRef.current);
    valueRef.current = next;
    setValue(next);
  }, [storageKey]);

  const setLocalValue = useCallback<Dispatch<SetStateAction<T>>>(
    (nextAction) => {
      const prev = valueRef.current;
      const next =
        typeof nextAction === "function" ? (nextAction as (prevState: T) => T)(prev) : nextAction;
      valueRef.current = next;
      persistLocalValue(storageKey, next, isClosedRef.current);
      setValue(next);
    },
    [storageKey],
  );

  return [value, setLocalValue];
}
