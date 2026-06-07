import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import {
  AlarmIcon,
  BrandIcon,
  DocsIcon,
  ExternalLinkIcon,
  FlowIcon,
  LobsterIcon,
  RunIcon,
  // SettingsIcon — re-import when restoring the Settings nav group below.
} from "@/components/icons";
import {
  UpgradeModal,
  useUpdateStatus,
  getDismissedVersion,
  buildDismissedVersionKey,
  getUpgradeModalOpen,
  setUpgradeModalOpen,
} from "@/components/UpdateNotice";
import type { UpdateStatus } from "@/lib/api";
import { cn } from "@/lib/cn";

interface NavItem {
  to: string;
  labelKey: string;
  icon: ReactNode;
}

interface NavGroup {
  titleKey: string;
  items: NavItem[];
}

// Public documentation site (opens in a new tab — it is not part of this SPA).
const DOCS_URL = "https://clawsomeflow.com/docs/";

const LAST_MODULE_ROUTE_KEY_PREFIX = "csflow:last-module-route:";

function makeModuleRouteKey(moduleBase: string): string {
  return `${LAST_MODULE_ROUTE_KEY_PREFIX}${moduleBase}`;
}

function readLastModuleRoute(moduleBase: string): string | null {
  try {
    return window.sessionStorage.getItem(makeModuleRouteKey(moduleBase));
  } catch {
    return null;
  }
}

function writeLastModuleRoute(moduleBase: string, route: string): void {
  try {
    window.sessionStorage.setItem(makeModuleRouteKey(moduleBase), route);
  } catch {
    /* sessionStorage disabled / quota — ignore */
  }
}

const NAV: NavGroup[] = [
  {
    titleKey: "nav.groupOrchestration",
    items: [
      { to: "/flows", labelKey: "nav.flows", icon: <FlowIcon className="h-8 w-8" /> },
      { to: "/runs", labelKey: "nav.runs", icon: <RunIcon className="h-8 w-8" /> },
      { to: "/scheduled-flows", labelKey: "nav.scheduledFlows", icon: <AlarmIcon className="h-8 w-8" /> },
    ],
  },
  {
    titleKey: "nav.groupAgents",
    items: [
      { to: "/chat", labelKey: "nav.chat", icon: <LobsterIcon className="h-8 w-8" /> },
    ],
  },
  // ── Settings group temporarily hidden (Profile module).
  //    The /profiles route still resolves so direct links keep working —
  //    only the nav entry is suppressed. Restore by uncommenting below.
  // {
  //   titleKey: "nav.groupSettings",
  //   items: [
  //     { to: "/profiles", labelKey: "nav.profiles", icon: <SettingsIcon className="h-8 w-8" /> },
  //   ],
  // },
];

interface BackendHealth {
  status: string;
  version: string;
}

export function AppShell() {
  const [health, setHealth] = useState<BackendHealth | null>(null);
  const location = useLocation();
  const update = useUpdateStatus();
  const [upgradeOpen, setUpgradeOpen] = useState(() => getUpgradeModalOpen());
  const autoOpenedFor = useRef("");

  useEffect(() => {
    fetch("/health")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setHealth({ status: d.status, version: d.version }))
      .catch(() => setHealth(null));
  }, []);

  // Auto-open once per (current -> latest) pair unless explicitly dismissed.
  useEffect(() => {
    if (!update?.updateAvailable) return;
    const pairKey = buildDismissedVersionKey(
      update.currentVersion,
      update.latestVersion,
    );
    if (!pairKey || autoOpenedFor.current === pairKey) return;
    if (getDismissedVersion() === pairKey) return;
    autoOpenedFor.current = pairKey;
    setUpgradeOpen(true);
  }, [update]);

  // Keep modal open/close state across browser refresh within this tab.
  useEffect(() => {
    setUpgradeModalOpen(upgradeOpen);
  }, [upgradeOpen]);

  // If no update is available anymore, close and clear persisted open state.
  useEffect(() => {
    if (update?.updateAvailable === false && upgradeOpen) {
      setUpgradeOpen(false);
    }
  }, [update?.updateAvailable, upgradeOpen]);

  return (
    <div className="flex h-screen w-screen bg-ink-50 text-ink-900">
      <Sidebar health={health} update={update} onOpenUpgrade={() => setUpgradeOpen(true)} />
      <main className="flex-1 overflow-hidden flex flex-col">
        <TopBar location={location.pathname} />
        <div className="flex-1 overflow-auto">
          <div className="mx-auto max-w-7xl px-6 py-6">
            <Outlet />
          </div>
        </div>
      </main>
      {update?.updateAvailable && (
        <UpgradeModal
          status={update}
          open={upgradeOpen}
          onClose={() => setUpgradeOpen(false)}
        />
      )}
    </div>
  );
}

function Sidebar({
  health,
  update,
  onOpenUpgrade,
}: {
  health: BackendHealth | null;
  update: UpdateStatus | null;
  onOpenUpgrade: () => void;
}) {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();

  const isItemActive = (to: string): boolean =>
    location.pathname === to || location.pathname.startsWith(`${to}/`);

  const currentRoute = `${location.pathname}${location.search}${location.hash}`;

  useEffect(() => {
    for (const group of NAV) {
      for (const item of group.items) {
        if (isItemActive(item.to)) {
          writeLastModuleRoute(item.to, currentRoute);
        }
      }
    }
  }, [currentRoute, location.pathname]);

  function navigateToModule(item: NavItem): void {
    if (isItemActive(item.to)) {
      return;
    }
    const remembered = readLastModuleRoute(item.to);
    const target =
      remembered &&
      (remembered === item.to || remembered.startsWith(`${item.to}/`))
        ? remembered
        : item.to;
    if (target === currentRoute) return;
    navigate(target);
  }

  return (
    <aside className="w-56 shrink-0 border-r border-ink-200 bg-white flex flex-col">
      <div className="px-5 py-5 border-b border-ink-100">
        <div className="flex items-center gap-2">
          <span className="inline-flex h-12 w-12 items-center justify-center rounded-xl border border-brand-200 bg-brand-50 text-brand-500 animate-pulse-glow">
            <BrandIcon className="h-8 w-8" />
          </span>
          <div>
            <div className="text-[15px] uppercase tracking-wide text-ink-500 font-semibold">
              {t("shell.sidebarTopLabel")}
            </div>
            <div className="text-base font-semibold text-brand-600 leading-tight">
              {t("shell.brandTagline")}
            </div>
          </div>
        </div>
        {/* Hype tagline under the "Control" label.
            Two stacked effects:
              1. metallic brand-gradient sweep via ``bg-clip-text`` +
                 ``animate-text-shimmer`` (keyframes shift bg-position)
              2. soft pulsing drop-shadow via ``animate-text-glow``
                 wrapper (one class per element rule — can't stack two
                 ``animation`` shorthands on the same node). */}
        <div className="mt-3 animate-text-glow">
          <div
            className="text-[11px] font-semibold leading-snug
                       bg-gradient-to-r from-brand-600 via-amber-400 to-brand-600
                       bg-[length:200%_auto] bg-clip-text text-transparent
                       animate-text-shimmer"
          >
            {t("shell.brandHype")}
          </div>
        </div>
      </div>
      <nav className="flex-1 overflow-y-auto py-3">
        {NAV.map((group) => (
          <div key={group.titleKey} className="mb-6">
            <div className="px-5 py-1 text-sm font-semibold uppercase tracking-wider text-ink-400">
              {t(group.titleKey)}
            </div>
            {group.items.map((it) => {
              const isActive = isItemActive(it.to);
              return (
                <button
                  key={it.to}
                  type="button"
                  onClick={() => navigateToModule(it)}
                  aria-current={isActive ? "page" : undefined}
                  className={cn(
                    "group mx-2 my-1 flex w-[calc(100%-1rem)] appearance-none items-center gap-3 rounded-md border-0 bg-transparent px-3 py-2 text-left text-sm font-medium transition-all",
                    isActive
                      ? "bg-brand-50 text-brand-700 shadow-[inset_0_0_0_1px_theme(colors.brand.200),0_0_18px_-8px_theme(colors.brand.400)]"
                      : "text-ink-700 hover:bg-ink-100 hover:text-brand-700",
                  )}
                >
                  <span
                    className={cn(
                      "inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-ink-200 bg-white text-brand-500 transition-all",
                      "group-hover:border-brand-200 group-hover:shadow-[0_0_12px_-4px_theme(colors.brand.300)]",
                    )}
                  >
                    {it.icon}
                  </span>
                  <span>{t(it.labelKey)}</span>
                </button>
              );
            })}
          </div>
        ))}

        {/* Resources — external links open in a new tab (not SPA routes). */}
        <div className="mb-6">
          <div className="px-5 py-1 text-sm font-semibold uppercase tracking-wider text-ink-400">
            {t("nav.groupResources")}
          </div>
          <a
            href={DOCS_URL}
            target="_blank"
            rel="noreferrer"
            className={cn(
              "group mx-2 my-1 flex w-[calc(100%-1rem)] appearance-none items-center gap-3 rounded-md border-0 bg-transparent px-3 py-2 text-left text-sm font-medium transition-all",
              "text-ink-700 hover:bg-ink-100 hover:text-brand-700",
            )}
          >
            <span
              className={cn(
                "inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-ink-200 bg-white text-brand-500 transition-all",
                "group-hover:border-brand-200 group-hover:shadow-[0_0_12px_-4px_theme(colors.brand.300)]",
              )}
            >
              <DocsIcon className="h-8 w-8" />
            </span>
            <span className="flex-1">{t("nav.docs")}</span>
            <ExternalLinkIcon className="h-4 w-4 text-ink-400" />
          </a>
        </div>
      </nav>
      <div className="flex items-center justify-between gap-2 border-t border-ink-100 px-4 py-3 text-left">
        <div className="shrink-0 text-xs font-medium text-ink-500">
          {t("shell.sidebarBottomLine")}
        </div>
        {update?.updateAvailable && update.currentVersion ? (
          <button
            type="button"
            onClick={onOpenUpgrade}
            className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-800 ring-1 ring-amber-300 transition-colors hover:bg-amber-100"
            title={
              update.latestVersion
                ? t("shell.updateBadgeTitle", {
                    current: update.currentVersion,
                    latest: update.latestVersion,
                  })
                : undefined
            }
          >
            <span className="relative inline-flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-70 animate-ping" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-500" />
            </span>
            {t("shell.updateBadge", { version: update.currentVersion })}
          </button>
        ) : health ? (
          <div className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200">
            <span className="relative inline-flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-60 animate-ping" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
            </span>
            {t("shell.backendOnline", { version: health.version })}
          </div>
        ) : (
          <span className="pill-danger shrink-0">● {t("shell.backendOffline")}</span>
        )}
      </div>
    </aside>
  );
}

function TopBar({
  location,
}: {
  location: string;
}) {
  const crumbs = location.split("/").filter(Boolean);
  return (
    <div className="border-b border-ink-200 bg-white/80 backdrop-blur supports-[backdrop-filter]:bg-white/60">
      <div className="mx-auto max-w-7xl px-6 h-12 flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm">
          <span className="text-brand-600 font-semibold tracking-wide">ClawsomeFlow</span>
          {crumbs.map((c, i) => (
            <span key={`${c}-${i}`} className="flex items-center gap-2">
              <span className="text-ink-300">›</span>
              <span className="text-ink-600 capitalize">{c.replace(/-/g, " ")}</span>
            </span>
          ))}
        </div>
        <LanguageSwitcher />
      </div>
    </div>
  );
}
