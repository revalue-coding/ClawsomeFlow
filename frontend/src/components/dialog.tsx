/**
 * App-wide confirm / alert dialogs rendered as the same centered, in-app modal
 * used elsewhere (e.g. the "delete Flow" dialog) — a drop-in replacement for the
 * browser-native `window.confirm` / `window.alert`, which render an OS chrome
 * popup ("<origin> says…") at the top of the page and look out of place.
 *
 * Usage:
 *   const { confirm, alert } = useDialog();
 *   if (!(await confirm(t("...")))) return;       // → boolean
 *   void alert(t("..."));                          // fire-and-forget notice
 *
 * Both are promise-based: `confirm` resolves to the user's choice, `alert`
 * resolves when dismissed. Mount <DialogProvider> once, above the routed pages.
 */
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { Modal } from "@/components/ui";

interface ConfirmOptions {
  title?: string;
  okText?: string;
  cancelText?: string;
  /** Render the confirm button in the danger style (destructive actions). */
  danger?: boolean;
}

interface AlertOptions {
  title?: string;
  okText?: string;
}

interface DialogApi {
  confirm: (message: ReactNode, opts?: ConfirmOptions) => Promise<boolean>;
  alert: (message: ReactNode, opts?: AlertOptions) => Promise<void>;
}

type DialogRequest =
  | {
      kind: "confirm";
      message: ReactNode;
      opts: ConfirmOptions;
      resolve: (value: boolean) => void;
    }
  | {
      kind: "alert";
      message: ReactNode;
      opts: AlertOptions;
      resolve: () => void;
    };

const DialogContext = createContext<DialogApi | null>(null);

export function DialogProvider({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [request, setRequest] = useState<DialogRequest | null>(null);

  const confirm = useCallback(
    (message: ReactNode, opts: ConfirmOptions = {}) =>
      new Promise<boolean>((resolve) => {
        setRequest({ kind: "confirm", message, opts, resolve });
      }),
    [],
  );

  const alert = useCallback(
    (message: ReactNode, opts: AlertOptions = {}) =>
      new Promise<void>((resolve) => {
        setRequest({ kind: "alert", message, opts, resolve });
      }),
    [],
  );

  const api = useMemo<DialogApi>(() => ({ confirm, alert }), [confirm, alert]);

  // Settle the pending promise and close. `result` only matters for confirm.
  const settle = useCallback(
    (result: boolean) => {
      setRequest((cur) => {
        if (!cur) return null;
        if (cur.kind === "confirm") cur.resolve(result);
        else cur.resolve();
        return null;
      });
    },
    [],
  );

  const isConfirm = request?.kind === "confirm";
  const title =
    request?.opts.title ??
    (isConfirm ? t("common.confirmTitle") : t("common.noticeTitle"));

  return (
    <DialogContext.Provider value={api}>
      {children}
      <Modal
        open={!!request}
        // Closing via Esc / × counts as "cancel" for confirm, "dismiss" for alert.
        onClose={() => settle(false)}
        title={title}
        width="max-w-md"
      >
        {request && (
          <div className="space-y-4">
            <div className="text-sm text-ink-700 whitespace-pre-line break-words">
              {request.message}
            </div>
            <div className="flex justify-end gap-2">
              {isConfirm && (
                <button
                  type="button"
                  className="btn-outline"
                  onClick={() => settle(false)}
                >
                  {request.opts.cancelText ?? t("common.cancel")}
                </button>
              )}
              <button
                type="button"
                className={
                  isConfirm && request.opts.danger ? "btn-danger" : "btn-primary"
                }
                onClick={() => settle(true)}
                autoFocus
              >
                {request.opts.okText ?? t("common.confirm")}
              </button>
            </div>
          </div>
        )}
      </Modal>
    </DialogContext.Provider>
  );
}

export function useDialog(): DialogApi {
  const ctx = useContext(DialogContext);
  if (!ctx) {
    throw new Error("useDialog must be used within a <DialogProvider>");
  }
  return ctx;
}
