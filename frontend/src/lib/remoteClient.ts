import { api, type UiCapabilities } from "@/lib/api";

let cachedCaps: UiCapabilities | null = null;
let loadPromise: Promise<UiCapabilities> | null = null;

export function setUiCapabilities(caps: UiCapabilities): void {
  cachedCaps = caps;
}

export function getUiCapabilities(): UiCapabilities | null {
  return cachedCaps;
}

export type NativeDirectoryBlockReason =
  | "remoteHostname"
  | "serverNoGui"
  | "clientNotColocated";

export type NativeDirectoryAction = "pick" | "open";

export function isRemoteHostname(): boolean {
  if (typeof window === "undefined") return false;
  const h = window.location.hostname;
  return h !== "localhost" && h !== "127.0.0.1" && h !== "::1" && h !== "";
}

/** Why native pick/open-directory is blocked, or null when allowed. */
export function getNativeDirectoryBlockReason(
  caps?: UiCapabilities | null,
): NativeDirectoryBlockReason | null {
  if (isRemoteHostname()) return "remoteHostname";
  const resolved = caps !== undefined ? caps : cachedCaps;
  if (!resolved) return null;
  if (!resolved.nativeDirectoryUiAvailable) return "serverNoGui";
  if (resolved.nativeDirectoryClientColocated === false) return "clientNotColocated";
  return null;
}

/**
 * True when the browser should not offer native directory pick/open actions.
 * Combines hostname heuristics with server-reported GUI availability.
 */
export function isRemoteBrowser(caps?: UiCapabilities | null): boolean {
  return getNativeDirectoryBlockReason(caps) !== null;
}

export function nativeDirectoryBlockedMessageKey(
  action: NativeDirectoryAction,
  reason: NativeDirectoryBlockReason,
): string {
  return `remoteClient.nativeDirectory.${action}.${reason}`;
}

export async function getNativeDirectoryBlockedMessage(
  t: (key: string) => string,
  action: NativeDirectoryAction,
): Promise<string | null> {
  await ensureUiCapabilities().catch(() => {});
  const reason = getNativeDirectoryBlockReason();
  if (!reason) return null;
  return t(nativeDirectoryBlockedMessageKey(action, reason));
}

/** Returns true when blocked (and shows an alert with the specific reason). */
export async function alertIfNativeDirectoryBlocked(
  t: (key: string) => string,
  action: NativeDirectoryAction,
): Promise<boolean> {
  const message = await getNativeDirectoryBlockedMessage(t, action);
  if (!message) return false;
  if (typeof window !== "undefined") void alert(message);
  return true;
}

export async function ensureUiCapabilities(): Promise<UiCapabilities> {
  if (cachedCaps) return cachedCaps;
  if (!loadPromise) {
    loadPromise = api
      .getUiCapabilities()
      .then((caps) => {
        cachedCaps = caps;
        return caps;
      })
      .finally(() => {
        loadPromise = null;
      });
  }
  return loadPromise;
}

export function resetUiCapabilitiesCache(): void {
  cachedCaps = null;
  loadPromise = null;
}
