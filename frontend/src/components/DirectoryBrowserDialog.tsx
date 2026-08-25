/**
 * WebUI server-side directory browser — the fallback picker / viewer for
 * environments where the native dialog cannot run (WSL2, SSH-forwarded
 * browsers, headless servers). It walks the SERVER filesystem via
 * /api/system/browse-directory, which is exactly the filesystem agents
 * execute in.
 *
 * Imperative usage: mount <DirectoryBrowserHost/> once (AppShell), then call
 * `browseDirectoryViaWebUI(...)` from anywhere; pick mode resolves with the
 * chosen absolute path or null on cancel. `pickDirectoryHybrid` /
 * `openDirectoryHybrid` wrap the native-vs-WebUI decision.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Modal } from "@/components/ui";
import { api, type BrowseDirectoryResult } from "@/lib/api";
import { getNativeDirectoryBlockedMessage } from "@/lib/remoteClient";

export type DirectoryBrowserMode = "pick" | "view";

export interface BrowseDialogOptions {
  title?: string;
  initialPath?: string | null;
  mode?: DirectoryBrowserMode;
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
  return browseDirectoryViaWebUI({ ...opts, mode: "pick" });
}

/**
 * Open a known directory with the same SSH/remote fallback as picking a
 * working directory: native file manager when colocated (or WSL explorer),
 * otherwise a WebUI viewer starting at that path.
 */
export async function openDirectoryHybrid(
  t: (key: string) => string,
  opts: { path: string; title?: string },
): Promise<void> {
  const target = opts.path.trim();
  if (!target) return;
  const blocked = await getNativeDirectoryBlockedMessage(t, "open");
  if (!blocked) {
    try {
      await api.openDirectory({ path: target });
      return;
    } catch {
      // Native open can still fail on a headless host that passed the
      // colocation check. Fall through to the WebUI viewer.
    }
  }
  await browseDirectoryViaWebUI({
    title: opts.title,
    initialPath: target,
    mode: "view",
  });
}

interface DialogState {
  open: boolean;
  title: string;
  mode: DirectoryBrowserMode;
  resolve: ((value: string | null) => void) | null;
}

export function DirectoryBrowserHost() {
  const { t } = useTranslation();
  const [state, setState] = useState<DialogState>({
    open: false,
    title: "",
    mode: "pick",
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
    async (path: string | null, includeHidden: boolean, includeFiles: boolean) => {
      setLoading(true);
      setError(null);
      try {
        const out = await api.browseDirectory({
          path: path || undefined,
          includeHidden,
          includeFiles,
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
        const mode = opts.mode === "view" ? "view" : "pick";
        setState({
          open: true,
          title: opts.title || "",
          mode,
          resolve,
        });
        setShowHidden(false);
        void load(opts.initialPath ?? null, false, mode === "view");
      });
    return () => {
      activeOpener = null;
    };
  }, [load]);

  const finish = useCallback((value: string | null) => {
    stateRef.current.resolve?.(value);
    setState({ open: false, title: "", mode: "pick", resolve: null });
    setListing(null);
    setError(null);
  }, []);

  if (!state.open) return null;

  const viewing = state.mode === "view";
  const includeFiles = viewing;

  return (
    <Modal
      open={state.open}
      onClose={() => finish(null)}
      title={state.title || (viewing ? t("directoryBrowser.viewTitle") : t("directoryBrowser.title"))}
      width="max-w-2xl"
    >
      <div className="space-y-3">
        <p className="text-xs text-ink-500">
          {viewing ? t("directoryBrowser.viewHint") : t("directoryBrowser.hint")}
        </p>
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void load(pathInput.trim() || null, showHidden, includeFiles);
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
              onClick={() => void load(listing.parent, showHidden, includeFiles)}
              disabled={loading}
            >
              ← {t("directoryBrowser.up")}
            </button>
          )}
          {listing && listing.entries.length === 0 && !loading && (
            <div className="px-3 py-4 text-center text-sm text-ink-400">
              {viewing ? t("directoryBrowser.emptyEntries") : t("directoryBrowser.empty")}
            </div>
          )}
          {listing?.entries.map((entry) => {
            const isFile = entry.kind === "file";
            if (isFile) {
              return (
                <div
                  key={entry.path}
                  className="block w-full truncate px-3 py-2 text-left text-sm text-ink-700"
                  title={entry.path}
                >
                  📄 {entry.name}
                </div>
              );
            }
            return (
              <button
                key={entry.path}
                type="button"
                className="block w-full truncate px-3 py-2 text-left text-sm text-ink-800 hover:bg-ink-50 dark:hover:bg-ink-800"
                onClick={() => void load(entry.path, showHidden, includeFiles)}
                disabled={loading}
                title={entry.path}
              >
                📁 {entry.name}
              </button>
            );
          })}
          {loading && (
            <div className="px-3 py-4 text-center text-sm text-ink-400">…</div>
          )}
        </div>
        {listing?.truncated && (
          <p className="text-xs text-amber-600">
            {viewing ? t("directoryBrowser.truncatedEntries") : t("directoryBrowser.truncated")}
          </p>
        )}
        <div className="flex items-center justify-between gap-2 pt-1">
          <label className="flex items-center gap-2 text-xs text-ink-500">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => {
                setShowHidden(e.target.checked);
                if (listing) void load(listing.path, e.target.checked, includeFiles);
              }}
            />
            {viewing ? t("directoryBrowser.showHiddenEntries") : t("directoryBrowser.showHidden")}
          </label>
          <div className="flex items-center gap-2">
            {viewing ? (
              <button type="button" className="btn-primary" onClick={() => finish(null)}>
                {t("common.close")}
              </button>
            ) : (
              <>
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
              </>
            )}
          </div>
        </div>
      </div>
    </Modal>
  );
}
