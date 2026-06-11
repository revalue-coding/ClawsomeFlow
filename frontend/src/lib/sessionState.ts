import { Dispatch, SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from "react";

const SESSION_KEY_PREFIX = "csflow:ui-state:";

type SessionStateOptions<T> = {
  isClosed?: (value: T) => boolean;
};

function readSessionValue<T>(storageKey: string, fallback: T): T {
  try {
    const raw = window.sessionStorage.getItem(storageKey);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function persistSessionValue<T>(
  storageKey: string,
  value: T,
  isClosed?: (value: T) => boolean,
): void {
  try {
    let closed = false;
    try {
      closed = Boolean(isClosed?.(value));
    } catch {
      closed = false;
    }
    if (closed) {
      window.sessionStorage.removeItem(storageKey);
      return;
    }
    window.sessionStorage.setItem(storageKey, JSON.stringify(value));
  } catch {
    /* sessionStorage disabled / quota — ignore */
  }
}

/**
 * Remove every session-backed key whose suffix (after the shared
 * ``csflow:ui-state:`` prefix) starts with ``keyPrefix``. Use this to drop a
 * whole draft once it has been committed — e.g. after a Flow is saved, clear
 * ``flow-editor:<id>:`` so a later visit shows the persisted server state
 * rather than a stale draft.
 */
export function clearSessionBackedKeys(keyPrefix: string): void {
  try {
    const fullPrefix = `${SESSION_KEY_PREFIX}${keyPrefix}`;
    const toRemove: string[] = [];
    for (let i = 0; i < window.sessionStorage.length; i += 1) {
      const k = window.sessionStorage.key(i);
      if (k && k.startsWith(fullPrefix)) toRemove.push(k);
    }
    toRemove.forEach((k) => window.sessionStorage.removeItem(k));
  } catch {
    /* sessionStorage disabled / quota — ignore */
  }
}

export function useSessionBackedState<T>(
  key: string,
  initialValue: T,
  options?: SessionStateOptions<T>,
): [T, Dispatch<SetStateAction<T>>] {
  const storageKey = useMemo(() => `${SESSION_KEY_PREFIX}${key}`, [key]);
  const isClosed = options?.isClosed;
  // Callers almost always pass an inline `isClosed` arrow, so its identity
  // changes every render. Keep it behind a ref so the returned setter can be
  // referentially stable — otherwise any effect that lists the setter in its
  // dependency array re-runs on every render. See the decompose modal, where
  // that churn cancelled the in-flight request before its id was ever stored.
  const isClosedRef = useRef(isClosed);
  const initialRef = useRef(initialValue);
  const [value, setValue] = useState<T>(() => readSessionValue(storageKey, initialValue));
  const valueRef = useRef(value);

  useEffect(() => {
    isClosedRef.current = isClosed;
  }, [isClosed]);

  useEffect(() => {
    valueRef.current = value;
  }, [value]);

  useEffect(() => {
    const next = readSessionValue(storageKey, initialRef.current);
    valueRef.current = next;
    setValue(next);
  }, [storageKey]);

  const setSessionValue = useCallback<Dispatch<SetStateAction<T>>>(
    (nextAction) => {
      const prev = valueRef.current;
      const next =
        typeof nextAction === "function"
          ? (nextAction as (prevState: T) => T)(prev)
          : nextAction;
      valueRef.current = next;
      persistSessionValue(storageKey, next, isClosedRef.current);
      setValue(next);
    },
    [storageKey],
  );

  return [value, setSessionValue];
}

export function useSessionBackedModalFlag(
  key: string,
  initialValue = false,
): [boolean, Dispatch<SetStateAction<boolean>>] {
  return useSessionBackedState<boolean>(key, initialValue, {
    isClosed: (value) => !value,
  });
}

