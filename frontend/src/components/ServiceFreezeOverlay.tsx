import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

/** How often to probe ``/health`` while the UI is interactive. */
const POLL_OK_MS = 3000;
/** Faster probe while frozen so unlock / version-reload is snappy. */
const POLL_FROZEN_MS = 1500;
/** Require this many consecutive probe failures before freezing (avoids blips). */
const FAIL_THRESHOLD = 2;

type FreezeReason = "draining" | "offline";

/**
 * Full-viewport lock while the backend is draining (pre-stop) or unreachable
 * (stop → start gap). Prevents clicks that would race the drain finalize or
 * hit a dead API. UpgradeModal may sit on top with its own fullscreen mask;
 * this covers CLI stop/start/upgrade paths that never open that modal.
 */
export function ServiceFreezeOverlay() {
  const { t } = useTranslation();
  const [reason, setReason] = useState<FreezeReason | null>(null);
  const failsRef = useRef(0);
  const lastVersionRef = useRef<string | null>(null);
  const frozenRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = (ms: number) => {
      if (cancelled) return;
      timer = setTimeout(() => {
        void tick();
      }, ms);
    };

    const tick = async () => {
      try {
        const r = await fetch("/health", { cache: "no-store" });
        if (!r.ok) throw new Error(`health ${r.status}`);
        const d = (await r.json()) as {
          draining?: boolean;
          version?: string;
        };
        if (cancelled) return;
        failsRef.current = 0;
        const version = typeof d.version === "string" ? d.version : null;
        if (d.draining === true) {
          frozenRef.current = true;
          setReason("draining");
          schedule(POLL_FROZEN_MS);
          return;
        }
        // Recovered after freeze: if the binary version changed, reload so the
        // SPA picks up the new frontend bundle; otherwise just unlock.
        if (frozenRef.current) {
          const prev = lastVersionRef.current;
          if (prev && version && prev !== version) {
            window.location.reload();
            return;
          }
          frozenRef.current = false;
          setReason(null);
        }
        if (version) lastVersionRef.current = version;
        schedule(POLL_OK_MS);
      } catch {
        if (cancelled) return;
        failsRef.current += 1;
        if (failsRef.current >= FAIL_THRESHOLD) {
          frozenRef.current = true;
          setReason("offline");
        }
        schedule(POLL_FROZEN_MS);
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  if (!reason) return null;

  return (
    <div
      className="fixed inset-0 z-[90] flex items-center justify-center bg-black/80 backdrop-blur-sm"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="csflow-service-freeze-title"
      aria-describedby="csflow-service-freeze-desc"
    >
      <div className="mx-4 max-w-md rounded-lg border border-ink-200 bg-surface px-6 py-5 shadow-xl dark:border-ink-500">
        <h2
          id="csflow-service-freeze-title"
          className="text-base font-semibold text-ink-900"
        >
          {reason === "draining"
            ? t("shell.serviceFreezeDrainingTitle")
            : t("shell.serviceFreezeOfflineTitle")}
        </h2>
        <p
          id="csflow-service-freeze-desc"
          className="mt-2 text-sm text-ink-600"
        >
          {reason === "draining"
            ? t("shell.serviceFreezeDrainingBody")
            : t("shell.serviceFreezeOfflineBody")}
        </p>
        {reason === "draining" ? (
          <p className="mt-3 text-xs text-ink-500">{t("shell.serviceFreezeHint")}</p>
        ) : null}
      </div>
    </div>
  );
}
