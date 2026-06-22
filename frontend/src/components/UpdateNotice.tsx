import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Modal } from "@/components/ui";
import { ApiError, api, type UpdateStatus } from "@/lib/api";

const DISMISSED_KEY = "csflow:dismissed-update-version";
const MODAL_OPEN_KEY = "csflow:update-modal-open";
const REFRESH_MS = 6 * 60 * 60 * 1000; // re-check every 6h
const UPGRADE_POLL_MS = 4000;
// The full pipeline (pip install + `csflow upgrade-runtime` re-deploy + service
// restart) routinely takes well over a minute — measured ~100s on a typical
// host, and longer on slow networks or with many agents. The old 90s window
// fired *before* the new build came up, so a successful upgrade was reported as
// a failure. Give it generous headroom; the user can always reload sooner.
const UPGRADE_TIMEOUT_MS = 6 * 60 * 1000;

export function buildDismissedVersionKey(
  currentVersion: string,
  latestVersion: string | null,
): string {
  return latestVersion ? `${currentVersion}->${latestVersion}` : "";
}

export function getDismissedVersion(): string {
  try {
    return localStorage.getItem(DISMISSED_KEY) ?? "";
  } catch {
    return "";
  }
}

export function getUpgradeModalOpen(): boolean {
  try {
    return sessionStorage.getItem(MODAL_OPEN_KEY) === "1";
  } catch {
    return false;
  }
}

export function setUpgradeModalOpen(open: boolean): void {
  try {
    if (open) {
      sessionStorage.setItem(MODAL_OPEN_KEY, "1");
    } else {
      sessionStorage.removeItem(MODAL_OPEN_KEY);
    }
  } catch {
    /* sessionStorage disabled / quota — ignore */
  }
}

function setDismissedVersion(currentVersion: string, latestVersion: string | null): void {
  const key = buildDismissedVersionKey(currentVersion, latestVersion);
  if (!key) return;
  try {
    localStorage.setItem(DISMISSED_KEY, key);
  } catch {
    /* localStorage disabled / quota — ignore */
  }
}

/** Poll the update-status endpoint on mount and on a slow interval. */
export function useUpdateStatus(): UpdateStatus | null {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      api
        .getUpdateStatus()
        .then((s) => {
          if (!cancelled) setStatus(s);
        })
        .catch(() => {
          /* never block the shell on a flaky check */
        });
    };
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);
  return status;
}

type Phase = "idle" | "confirm" | "stopping" | "upgrading" | "done" | "failed";

export function UpgradeModal({
  status,
  open,
  onClose,
}: {
  status: UpdateStatus;
  open: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<Phase>("idle");
  const [activeRunCount, setActiveRunCount] = useState(0);
  // Set once the backend stops responding after an upgrade was triggered, so we
  // can switch the message from "stopping the service" to "service stopped".
  const [serviceStopped, setServiceStopped] = useState(false);
  const pollRef = useRef<number | null>(null);
  const timeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
      if (timeoutRef.current !== null) window.clearTimeout(timeoutRef.current);
    };
  }, []);

  // Reopening the modal after a failed upgrade must return to the selection
  // page (so the user can retry) instead of being stuck on the "failed" view.
  // Keyed on `open` only — reacting to `phase` would wipe the failure message
  // the instant it appears. We don't reset while an upgrade is in progress.
  useEffect(() => {
    // Reopening returns to the selection page from a transient/failed state
    // (so "Later" on the active-runs warning doesn't strand the modal there),
    // but never while an upgrade is actually in progress.
    if (open && (phase === "failed" || phase === "confirm")) setPhase("idle");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Once an upgrade is in flight (stopping/upgrading) or finished (done) the
  // modal masks everything and can't be dismissed.
  const dismissible =
    phase !== "done" && phase !== "stopping" && phase !== "upgrading";

  function stopHealthPoll() {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }

  function startHealthPoll() {
    stopHealthPoll();
    timeoutRef.current = window.setTimeout(() => {
      stopHealthPoll();
      // One last check before giving up: the new build may have come up in the
      // gap since the previous poll (the restart and the poll cadence are not
      // synchronised).
      fetch("/health")
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (d && d.version && d.version !== status.currentVersion) {
            setPhase("done");
            window.setTimeout(() => window.location.reload(), 1500);
          } else {
            setPhase("failed");
          }
        })
        .catch(() => setPhase("failed"));
    }, UPGRADE_TIMEOUT_MS);
    pollRef.current = window.setInterval(() => {
      fetch("/health")
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          // The new build is live once the reported version no longer
          // matches the version we started from.
          if (d && d.version && d.version !== status.currentVersion) {
            stopHealthPoll();
            setPhase("done");
            window.setTimeout(() => window.location.reload(), 1500);
          }
        })
        .catch(() => {
          // Backend unreachable → it has begun stopping. Switch the message
          // from "stopping the service" to "service stopped, upgrading".
          setServiceStopped(true);
        });
    }, UPGRADE_POLL_MS);
  }

  function proceedUpgrade(confirmActiveRuns: boolean) {
    setServiceStopped(false);
    setPhase("stopping");
    api
      .triggerUpgrade(confirmActiveRuns)
      .then(() => startHealthPoll())
      .catch((e) => {
        stopHealthPoll();
        // Active runs appeared between the precheck and the trigger — surface
        // the confirmation step instead of failing.
        if (e instanceof ApiError && e.code === "ACTIVE_RUNS_PRESENT") {
          const count = Number(
            (e.details as { active_runs?: number } | undefined)?.active_runs ?? 0,
          );
          setActiveRunCount(count > 0 ? count : 1);
          setPhase("confirm");
          return;
        }
        // "Already on the latest stable release" means a previous attempt
        // actually succeeded — typically the service finished restarting only
        // after our poll window elapsed, so the first attempt was wrongly shown
        // as failed and this is the user clicking "Retry". Treat it as done and
        // reload instead of reporting another failure.
        if (e instanceof ApiError && e.code === "NO_UPGRADE_AVAILABLE") {
          setPhase("done");
          window.setTimeout(() => window.location.reload(), 1500);
          return;
        }
        setPhase("failed");
      });
  }

  function onUpgrade() {
    // Warn before aborting in-flight runs (the pre-stop drain terminates them
    // when the service restarts). On any check error, proceed rather than block.
    api
      .getActiveRuns()
      .then((res) => {
        if (res.count > 0) {
          setActiveRunCount(res.count);
          setPhase("confirm");
        } else {
          proceedUpgrade(false);
        }
      })
      .catch(() => proceedUpgrade(false));
  }

  function onDismiss() {
    setDismissedVersion(status.currentVersion, status.latestVersion);
    onClose();
  }

  return (
    <Modal
      open={open}
      onClose={() => {
        if (dismissible) onClose();
      }}
      title={t("shell.updateModalTitle")}
      dismissible={dismissible}
      // While the upgrade is actually running (stopping the service + the
      // post-upgrade restart wait), fully mask everything behind the modal so
      // the user can't interact with a backend that is restarting. The idle
      // "update available" notice keeps the normal modeless dim.
      fullscreenBackdrop={
        phase === "stopping" || phase === "upgrading" || phase === "done"
      }
    >
      <div className="space-y-4 text-sm text-ink-700">
        <p>{t("shell.updateIntro")}</p>

        <div className="flex items-center gap-3 rounded-md bg-ink-50 px-4 py-3">
          <div>
            <div className="text-xs text-ink-400">{t("shell.updateCurrent")}</div>
            <div className="font-mono font-medium">v{status.currentVersion}</div>
          </div>
          <span className="text-ink-300">→</span>
          <div>
            <div className="text-xs text-ink-400">{t("shell.updateLatest")}</div>
            <div className="font-mono font-medium text-brand-600">
              v{status.latestVersion}
            </div>
          </div>
        </div>

        {phase === "confirm" && (
          <p className="text-amber-700">
            {t("shell.updateActiveRunsWarn", { count: activeRunCount })}
          </p>
        )}
        {phase === "stopping" && (
          <p className="text-amber-700">
            {serviceStopped
              ? t("shell.updateStopped")
              : t("shell.updateStopping")}
          </p>
        )}
        {phase === "upgrading" && (
          <p className="text-amber-700">{t("shell.updateRestartHint")}</p>
        )}
        {phase === "done" && (
          <p className="text-emerald-700">
            {t("shell.updateDone", { version: status.latestVersion })}
          </p>
        )}
        {phase === "failed" && (
          <div className="space-y-2">
            <p className="text-rose-700">{t("shell.updateFailed")}</p>
            <pre className="overflow-x-auto rounded-md bg-ink-900 px-3 py-2 font-mono text-xs text-ink-50">
              {t("shell.updateFailedCommand")}
            </pre>
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          {phase === "idle" && (
            <>
              <button className="btn-ghost" onClick={onDismiss} type="button">
                {t("shell.updateDismiss")}
              </button>
              <button className="btn-outline" onClick={onClose} type="button">
                {t("shell.updateClose")}
              </button>
              <button className="btn-primary" onClick={onUpgrade} type="button">
                {t("shell.updateNow")}
              </button>
            </>
          )}
          {phase === "confirm" && (
            <>
              <button className="btn-outline" onClick={onClose} type="button">
                {t("shell.updateLater")}
              </button>
              <button
                className="btn-primary"
                onClick={() => proceedUpgrade(true)}
                type="button"
              >
                {t("shell.updateAnyway")}
              </button>
            </>
          )}
          {(phase === "stopping" || phase === "upgrading") && (
            <button className="btn-ghost" disabled type="button">
              {t("shell.updateInProgress")}
            </button>
          )}
          {phase === "done" && (
            <button
              className="btn-primary"
              onClick={() => window.location.reload()}
              type="button"
            >
              {t("shell.updateReload")}
            </button>
          )}
          {phase === "failed" && (
            <>
              <button className="btn-outline" onClick={onClose} type="button">
                {t("shell.updateClose")}
              </button>
              <button className="btn-ghost" onClick={onUpgrade} type="button">
                {t("shell.updateRetry")}
              </button>
              <button
                className="btn-primary"
                onClick={() => window.location.reload()}
                type="button"
              >
                {t("shell.updateReload")}
              </button>
            </>
          )}
        </div>
      </div>
    </Modal>
  );
}
