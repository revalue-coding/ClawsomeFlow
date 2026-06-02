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

export function useSessionBackedState<T>(
  key: string,
  initialValue: T,
  options?: SessionStateOptions<T>,
): [T, Dispatch<SetStateAction<T>>] {
  const storageKey = useMemo(() => `${SESSION_KEY_PREFIX}${key}`, [key]);
  const isClosed = options?.isClosed;
  const initialRef = useRef(initialValue);
  const [value, setValue] = useState<T>(() => readSessionValue(storageKey, initialValue));
  const valueRef = useRef(value);

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
      persistSessionValue(storageKey, next, isClosed);
      setValue(next);
    },
    [isClosed, storageKey],
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

