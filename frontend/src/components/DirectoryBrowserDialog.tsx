/**
 * WebUI server-side directory browser — the fallback picker for environments
 * where the native dialog cannot run (WSL2, SSH-forwarded browsers, headless
 * servers). It walks the SERVER filesystem via /api/system/browse-directory,
 * which is exactly the filesystem agents execute in.
 *
 * Imperative usage: mount <DirectoryBrowserHost/> once (AppShell), then call
 * `browseDirectoryViaWebUI(...)` from anywhere; it resolves with the chosen
 * absolute path or null on cancel. `pickDirectoryHybrid` wraps the whole
 * decision: native picker when allowed, WebUI browser otherwise.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Modal } from "@/components/ui";
import { api, type BrowseDirectoryResult } from "@/lib/api";
import { getNativeDirectoryBlockedMessage } from "@/lib/remoteClient";

export interface BrowseDialogOptions {
  title?: string;
  initialPath?: string | null;
}

type Opener = (opts: BrowseDialogOptions) => Promise<string | null>;

let activeOpener: Opener | null = null;

/** Open the WebUI directory browser; resolves with the picked path or null. */
export function browseDirectoryViaWebUI(
  opts: BrowseDialogOptions = {},
): Promise<string | null> {
  if (!activeOpener) return Promise.resolve(null);
  return activeOpener(opts);
}

/**
 * Pick a directory with automatic fallback: native dialog when the browser is
 * colocated with the server desktop, otherwise the WebUI browser. Native-path
 * API errors still throw so call sites keep their own error handling.
 */
export async function pickDirectoryHybrid(
  t: (key: string) => string,
  opts: BrowseDialogOptions = {},
): Promise<string | null> {
  const blocked = await getNativeDirectoryBlockedMessage(t, "pick");
  if (!blocked) {
    const out = await api.pickDirectory({
      title: opts.title,
      initialPath: opts.initialPath ?? undefined,
    });
    return out.path ?? null;
  }
  return browseDirectoryViaWebUI(opts);
}

interface DialogState {
  open: boolean;
  title: string;
  resolve: ((value: string | null) => void) | null;
}

export function DirectoryBrowserHost() {
  const { t } = useTranslation();
  const [state, setState] = useState<DialogState>({
    open: false,
    title: "",
    resolve: null,
  });
  const [listing, setListing] = useState<BrowseDirectoryResult | null>(null);
  const [pathInput, setPathInput] = useState("");
  const [showHidden, setShowHidden] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;

  const load = useCallback(
    async (path: string | null, includeHidden: boolean) => {
      setLoading(true);
      setError(null);
      try {
        const out = await api.browseDirectory({
          path: path || undefined,
          includeHidden,
        });
        setListing(out);
        setPathInput(out.path);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    activeOpener = (opts: BrowseDialogOptions) =>
      new Promise<string | null>((resolve) => {
        // A pending previous invocation (shouldn't happen, but be safe)
        // resolves as cancelled before the dialog is reused.
        stateRef.current.resolve?.(null);
        setState({
          open: true,
          title: opts.title || "",
          resolve,
        });
        setShowHidden(false);
        void load(opts.initialPath ?? null, false);
      });
    return () => {
      activeOpener = null;
    };
  }, [load]);

  const finish = useCallback((value: string | null) => {
    stateRef.current.resolve?.(value);
    setState({ open: false, title: "", resolve: null });
    setListing(null);
    setError(null);
  }, []);

  if (!state.open) return null;

  return (
    <Modal
      open={state.open}
      onClose={() => finish(null)}
      title={state.title || t("directoryBrowser.title")}
      width="max-w-2xl"
    >
      <div className="space-y-3">
        <p className="text-xs text-ink-500">{t("directoryBrowser.hint")}</p>
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void load(pathInput.trim() || null, showHidden);
          }}
        >
          <input
            className="input flex-1 font-mono text-sm"
            value={pathInput}
            onChange={(e) => setPathInput(e.target.value)}
            placeholder={t("directoryBrowser.pathPlaceholder")}
            spellCheck={false}
          />
          <button type="submit" className="btn-outline shrink-0" disabled={loading}>
            {t("directoryBrowser.go")}
          </button>
        </form>
        {error && (
          <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
            {error}
          </div>
        )}
        <div className="max-h-72 overflow-y-auto rounded border border-ink-200">
          {listing?.parent && (
            <button
              type="button"
              className="block w-full px-3 py-2 text-left text-sm text-ink-600 hover:bg-ink-50 dark:hover:bg-ink-800"
              onClick={() => void load(listing.parent, showHidden)}
              disabled={loading}
            >
              ← {t("directoryBrowser.up")}
            </button>
          )}
          {listing && listing.entries.length === 0 && !loading && (
            <div className="px-3 py-4 text-center text-sm text-ink-400">
              {t("directoryBrowser.empty")}
            </div>
          )}
          {listing?.entries.map((entry) => (
            <button
              key={entry.path}
              type="button"
              className="block w-full truncate px-3 py-2 text-left text-sm text-ink-800 hover:bg-ink-50 dark:hover:bg-ink-800"
              onClick={() => void load(entry.path, showHidden)}
              disabled={loading}
              title={entry.path}
            >
              📁 {entry.name}
            </button>
          ))}
          {loading && (
            <div className="px-3 py-4 text-center text-sm text-ink-400">…</div>
          )}
        </div>
        {listing?.truncated && (
          <p className="text-xs text-amber-600">{t("directoryBrowser.truncated")}</p>
        )}
        <div className="flex items-center justify-between gap-2 pt-1">
          <label className="flex items-center gap-2 text-xs text-ink-500">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => {
                setShowHidden(e.target.checked);
                if (listing) void load(listing.path, e.target.checked);
              }}
            />
            {t("directoryBrowser.showHidden")}
          </label>
          <div className="flex items-center gap-2">
            <button type="button" className="btn-outline" onClick={() => finish(null)}>
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={!listing || loading}
              onClick={() => listing && finish(listing.path)}
            >
              {t("directoryBrowser.selectCurrent")}
            </button>
          </div>
        </div>
      </div>
    </Modal>
  );
}
