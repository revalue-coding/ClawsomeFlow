import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Modal } from "@/components/ui";
import { api, type UpdateStatus } from "@/lib/api";

const DISMISSED_KEY = "csflow:dismissed-update-version";
const MODAL_OPEN_KEY = "csflow:update-modal-open";
const REFRESH_MS = 6 * 60 * 60 * 1000; // re-check every 6h
const UPGRADE_POLL_MS = 4000;
const UPGRADE_TIMEOUT_MS = 90 * 1000;

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

type Phase = "idle" | "upgrading" | "done" | "failed";

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
  const pollRef = useRef<number | null>(null);
  const timeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
      if (timeoutRef.current !== null) window.clearTimeout(timeoutRef.current);
    };
  }, []);

  const dismissible = phase !== "done";

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
      setPhase("failed");
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
          /* service is restarting — keep polling */
        });
    }, UPGRADE_POLL_MS);
  }

  function onUpgrade() {
    setPhase("upgrading");
    api
      .triggerUpgrade()
      .then(() => startHealthPoll())
      .catch(() => {
        stopHealthPoll();
        setPhase("failed");
      });
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
          {phase === "upgrading" && (
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
            <button className="btn-outline" onClick={onClose} type="button">
              {t("shell.updateClose")}
            </button>
          )}
        </div>
      </div>
    </Modal>
  );
}
