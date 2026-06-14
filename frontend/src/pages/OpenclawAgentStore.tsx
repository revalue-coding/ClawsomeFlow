import { FormEvent, useEffect, useMemo, useState } from "react";
import { SilentLink } from "@/components/SilentLink";
import { useTranslation } from "react-i18next";

import {
  AgentStoreAccount,
  AgentStoreCatalogItem,
  AgentStoreListingType,
  AgentStoreOwnedItem,
  ApiError,
  api,
} from "@/lib/api";
import { Card, EmptyState, ErrorBox, Loading, Modal } from "@/components/ui";
import { StoreIcon } from "@/components/icons";
import { cn } from "@/lib/cn";
import { useSessionBackedModalFlag } from "@/lib/sessionState";
import { useOpRecovery } from "@/lib/useOpRecovery";

type ActionPhase = "join" | "purchase" | "load";
const STORE_TOKEN_KEY = "csflow-agent-store-token";
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function OpenclawAgentStore() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<AgentStoreListingType>("single");
  const [catalog, setCatalog] = useState<AgentStoreCatalogItem[] | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [owned, setOwned] = useState<AgentStoreOwnedItem[] | null>(null);
  const [ownedLoading, setOwnedLoading] = useState(false);
  const [ownedError, setOwnedError] = useState<string | null>(null);
  const [actionState, setActionState] = useState<Record<string, ActionPhase | null>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [authReady, setAuthReady] = useState(false);
  const [storeToken, setStoreToken] = useState("");
  const [account, setAccount] = useState<AgentStoreAccount | null>(null);
  const [pendingAcquire, setPendingAcquire] = useState<AgentStoreCatalogItem | null>(null);

  // Recover an in-flight "load to local" install across refresh / close+reopen
  // (the per-listing spinner + outcome are otherwise in-memory only). The
  // pointer's agentId field carries the listingId.
  const { track: trackLoadOp, clear: clearLoadOp } = useOpRecovery("openclaw-store:load:op", {
    onRunning: (p) => setRunning(p.agentId, "load"),
    onSucceeded: (p, result) => {
      setRunning(p.agentId, null);
      const count = Array.isArray(result.loadedAgentIds) ? result.loadedAgentIds.length : 0;
      setNotice(t("store.notice.loadRecovered", { count }));
      if (storeToken) void loadOwned(storeToken);
    },
    onFailed: (p, detail) => {
      setRunning(p.agentId, null);
      setActionError(detail);
    },
  });

  const [loginOpen, setLoginOpen] = useSessionBackedModalFlag("agent-store:login-open");
  const [registerOpen, setRegisterOpen] = useSessionBackedModalFlag("agent-store:register-open");
  const [forgotOpen, setForgotOpen] = useSessionBackedModalFlag("agent-store:forgot-open");

  const [loginEmail, setLoginEmail] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [loginLoading, setLoginLoading] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);

  const [registerEmail, setRegisterEmail] = useState("");
  const [registerDisplayName, setRegisterDisplayName] = useState("");
  const [registerPassword, setRegisterPassword] = useState("");
  const [registerConfirmPassword, setRegisterConfirmPassword] = useState("");
  const [registerCode, setRegisterCode] = useState("");
  const [registerLoading, setRegisterLoading] = useState(false);
  const [registerError, setRegisterError] = useState<string | null>(null);
  const [registerNotice, setRegisterNotice] = useState<string | null>(null);

  const [forgotEmail, setForgotEmail] = useState("");
  const [forgotCode, setForgotCode] = useState("");
  const [forgotNewPassword, setForgotNewPassword] = useState("");
  const [forgotLoading, setForgotLoading] = useState(false);
  const [forgotError, setForgotError] = useState<string | null>(null);
  const [forgotNotice, setForgotNotice] = useState<string | null>(null);

  async function loadCatalog(type: AgentStoreListingType) {
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const out = await api.listAgentStoreCatalog(type);
      setCatalog(out.items);
    } catch (e) {
      const text = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setCatalogError(text);
      setCatalog([]);
    } finally {
      setCatalogLoading(false);
    }
  }

  async function loadOwned(token: string) {
    setOwnedLoading(true);
    setOwnedError(null);
    try {
      const out = await api.listAgentStoreOwned(token);
      setOwned(out.items);
    } catch (e) {
      const text = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setOwnedError(text);
      setOwned([]);
    } finally {
      setOwnedLoading(false);
    }
  }

  useEffect(() => {
    void loadCatalog(tab);
  }, [tab]);

  useEffect(() => {
    void (async () => {
      const token = window.localStorage.getItem(STORE_TOKEN_KEY)?.trim() || "";
      if (!token) {
        setAuthReady(true);
        return;
      }
      try {
        const me = await api.getAgentStoreProfile(token);
        setStoreToken(token);
        setAccount(me);
        await loadOwned(token);
      } catch {
        window.localStorage.removeItem(STORE_TOKEN_KEY);
      } finally {
        setAuthReady(true);
      }
    })();
  }, []);

  const ownedSet = useMemo(() => {
    return new Set((owned ?? []).map((item) => item.listingId));
  }, [owned]);

  function setRunning(listingId: string, phase: ActionPhase | null) {
    setActionState((prev) => ({ ...prev, [listingId]: phase }));
  }

  async function acquireWithToken(item: AgentStoreCatalogItem, token: string) {
    const phase: ActionPhase = item.pricing.mode === "free" ? "join" : "purchase";
    setRunning(item.listingId, phase);
    try {
      if (item.pricing.mode === "free") {
        await api.joinAgentStoreListing(item.listingId, token);
      } else {
        await api.purchaseAgentStoreListing(item.listingId, token);
      }
      await loadOwned(token);
      setNotice(
        item.pricing.mode === "free"
          ? t("store.notice.joinSuccess", { title: item.title })
          : t("store.notice.purchaseSuccess", { title: item.title }),
      );
    } finally {
      setRunning(item.listingId, null);
    }
  }

  async function onAcquire(item: AgentStoreCatalogItem) {
    if (ownedSet.has(item.listingId)) return;
    setActionError(null);
    setNotice(null);
    if (!storeToken) {
      setPendingAcquire(item);
      setActionError(t("store.auth.loginRequired"));
      setLoginOpen(true);
      return;
    }
    try {
      await acquireWithToken(item, storeToken);
    } catch (e) {
      const text = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setActionError(text);
    }
  }

  async function onLoadToLocal(item: AgentStoreOwnedItem) {
    setActionError(null);
    setNotice(null);
    if (!storeToken) {
      setActionError(t("store.auth.loginRequired"));
      setLoginOpen(true);
      return;
    }
    setRunning(item.listingId, "load");
    trackLoadOp({ opId: `store_load:${item.listingId}`, agentId: item.listingId });
    try {
      const out = await api.loadAgentStoreListing(item.listingId, storeToken);
      clearLoadOp();
      setNotice(t("store.notice.loadSuccess", { title: item.title, count: out.loadedAgentIds.length }));
      await loadOwned(storeToken);
    } catch (e) {
      clearLoadOp();
      const text = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setActionError(text);
    } finally {
      setRunning(item.listingId, null);
    }
  }

  async function onSubmitLogin(e: FormEvent) {
    e.preventDefault();
    if (loginLoading) return;
    setLoginError(null);
    setLoginLoading(true);
    try {
      const out = await api.loginAgentStore({
        email: loginEmail.trim(),
        password: loginPassword.trim(),
      });
      window.localStorage.setItem(STORE_TOKEN_KEY, out.accessToken);
      setStoreToken(out.accessToken);
      setAccount(out.account);
      setLoginOpen(false);
      setLoginPassword("");
      await loadOwned(out.accessToken);
      if (pendingAcquire) {
        const next = pendingAcquire;
        setPendingAcquire(null);
        await acquireWithToken(next, out.accessToken);
      }
      setNotice(t("store.auth.loginSuccess", { name: out.account.displayName || out.account.id }));
    } catch (e) {
      const text = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setLoginError(text);
    } finally {
      setLoginLoading(false);
    }
  }

  async function onSubmitRegister(e: FormEvent) {
    e.preventDefault();
    if (registerLoading) return;
    if (registerPassword !== registerConfirmPassword) {
      setRegisterError(t("store.auth.passwordMismatch"));
      return;
    }
    if (registerPassword.trim().length < 8) {
      setRegisterError(t("store.auth.passwordTooShort"));
      return;
    }
    if (!registerCode.trim()) {
      setRegisterError(t("store.auth.verifyCodeRequired"));
      return;
    }
    setRegisterLoading(true);
    setRegisterError(null);
    try {
      const out = await api.verifyAgentStoreEmail({
        email: registerEmail.trim(),
        code: registerCode.trim(),
      });
      setRegisterNotice(out.message || t("store.auth.verifySuccess"));
    } catch (e) {
      const text = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setRegisterError(text);
    } finally {
      setRegisterLoading(false);
    }
  }

  async function onSendRegisterCode() {
    if (registerLoading) return;
    const email = registerEmail.trim();
    const password = registerPassword.trim();
    if (!email || !password) {
      setRegisterError(t("store.auth.sendCodeInvalidInput"));
      return;
    }
    if (!EMAIL_RE.test(email)) {
      setRegisterError(t("store.auth.emailInvalid"));
      return;
    }
    if (password.length < 8) {
      setRegisterError(t("store.auth.passwordTooShort"));
      return;
    }
    setRegisterLoading(true);
    setRegisterError(null);
    try {
      const out = await api.registerAgentStore({
        email,
        password,
        displayName: registerDisplayName.trim(),
      });
      setRegisterNotice(out.message || t("store.auth.registerSuccess"));
    } catch (e) {
      const text = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setRegisterError(text);
    } finally {
      setRegisterLoading(false);
    }
  }

  async function onSendForgotEmail() {
    if (forgotLoading) return;
    setForgotLoading(true);
    setForgotError(null);
    try {
      const out = await api.forgotAgentStorePassword({ email: forgotEmail.trim() });
      setForgotNotice(out.message || t("store.auth.forgotSent"));
    } catch (e) {
      const text = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setForgotError(text);
    } finally {
      setForgotLoading(false);
    }
  }

  async function onResetPassword() {
    if (forgotLoading) return;
    if (forgotNewPassword.trim().length < 8) {
      setForgotError(t("store.auth.passwordTooShort"));
      return;
    }
    setForgotLoading(true);
    setForgotError(null);
    try {
      const out = await api.resetAgentStorePassword({
        email: forgotEmail.trim(),
        code: forgotCode.trim(),
        newPassword: forgotNewPassword.trim(),
      });
      setForgotNotice(out.message || t("store.auth.resetSuccess"));
    } catch (e) {
      const text = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setForgotError(text);
    } finally {
      setForgotLoading(false);
    }
  }

  function logoutStoreAccount() {
    window.localStorage.removeItem(STORE_TOKEN_KEY);
    setStoreToken("");
    setAccount(null);
    setOwned(null);
    setNotice(t("store.auth.logoutSuccess"));
  }

  return (
    <div className="-mx-4 -mt-4 min-h-[calc(100vh-4rem)] overflow-hidden bg-[#040714] text-slate-100 md:-mx-6 relative">
      <div className="pointer-events-none absolute inset-0">
        <div className="absolute inset-0 bg-[radial-gradient(1200px_500px_at_0%_-10%,rgba(59,130,246,0.24),transparent_65%),radial-gradient(900px_420px_at_100%_0%,rgba(168,85,247,0.2),transparent_62%),radial-gradient(800px_320px_at_50%_100%,rgba(14,165,233,0.13),transparent_68%)]" />
        <div className="absolute inset-0 opacity-30 [background-image:linear-gradient(rgba(56,189,248,0.12)_1px,transparent_1px),linear-gradient(90deg,rgba(56,189,248,0.12)_1px,transparent_1px)] [background-size:56px_56px]" />
        <div className="absolute left-1/2 top-[-160px] h-[460px] w-[1050px] -translate-x-1/2 rounded-full bg-cyan-400/10 blur-3xl" />
      </div>
      <div className="relative z-10 space-y-5 p-6 md:p-8">
        <header className="flex items-center justify-between gap-4 px-1 py-2">
          <h1 className="text-2xl font-semibold tracking-[0.08em] text-cyan-100 drop-shadow-[0_0_14px_rgba(56,189,248,0.35)]">
            {t("store.title")}
          </h1>
          <div className="flex items-center gap-2">
            {!authReady ? (
              <span className="rounded border border-slate-500/60 bg-slate-900/70 px-2 py-1 text-xs">
                {t("common.loading")}
              </span>
            ) : account ? (
              <>
                <span className="hidden text-xs text-slate-300 md:inline">
                  {t("store.auth.loggedInAs", { name: account.displayName || account.id })}
                </span>
                <button type="button" className="btn-outline !text-xs" onClick={logoutStoreAccount}>
                  {t("store.auth.logout")}
                </button>
              </>
            ) : (
              <>
                <button
                  type="button"
                  className="btn-primary !h-8 !px-3 !py-1 !text-xs"
                  onClick={() => setLoginOpen(true)}
                >
                  {t("store.auth.login")}
                </button>
                <button
                  type="button"
                  className="btn-outline !h-8 !px-3 !py-1 !text-xs"
                  onClick={() => setRegisterOpen(true)}
                >
                  {t("store.auth.register")}
                </button>
              </>
            )}
            <SilentLink className="btn-outline !h-8 !px-3 !py-1 !text-xs" to="/chat">
              {t("common.back")}
            </SilentLink>
          </div>
        </header>

        {notice && (
          <div className="border border-emerald-300/50 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-200">
            {notice}
          </div>
        )}
        {actionError && <ErrorBox>{actionError}</ErrorBox>}

        {authReady && account ? (
          <Card className="!rounded-none space-y-3 border-[#2a2f63] bg-[#0b1230]/95 text-slate-100">
            <div>
              <h2 className="text-base font-semibold text-slate-100">{t("store.owned.title")}</h2>
            </div>
            {ownedLoading && <Loading />}
            {ownedError && <ErrorBox>{ownedError}</ErrorBox>}
            {owned && owned.length === 0 && !ownedLoading && !ownedError && (
              <EmptyState icon={<StoreIcon className="h-10 w-10" />} title={t("store.owned.empty")} />
            )}
            {owned && owned.length > 0 && (
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                {owned.map((item) => {
                  const running = actionState[item.listingId] === "load";
                  return (
                    <div key={item.listingId} className="space-y-3 border border-slate-700 bg-slate-950/70 p-4">
                      <div className="flex items-center gap-3">
                        <ListingAvatar title={item.title} avatarUrl={item.avatarUrl} />
                        <div className="min-w-0">
                          <div className="truncate text-sm font-semibold text-slate-100">{item.title}</div>
                          <div className="text-xs text-slate-400">{t(`store.type.${item.type}`)}</div>
                        </div>
                      </div>
                      {item.description ? (
                        <p className="line-clamp-2 text-xs text-slate-300">{item.description}</p>
                      ) : null}
                      <button
                        type="button"
                        className="inline-flex w-full items-center justify-center border border-indigo-400/70 bg-gradient-to-r from-indigo-500 to-fuchsia-500 px-3 py-2 text-sm font-semibold text-white transition hover:from-indigo-400 hover:to-fuchsia-400 disabled:cursor-not-allowed disabled:opacity-50"
                        onClick={() => void onLoadToLocal(item)}
                        disabled={running}
                      >
                        {running ? t("store.actions.loading") : t("store.actions.loadToLocal")}
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        ) : (
          <Card className="!rounded-none border-[#2a2f63] bg-[#0b1230]/70 text-slate-100">
            <div className="space-y-2">
              <h2 className="text-base font-semibold text-slate-100">{t("store.auth.manageLockedTitle")}</h2>
              <button type="button" className="btn-primary mt-2" onClick={() => setLoginOpen(true)}>
                {t("store.auth.loginToManage")}
              </button>
            </div>
          </Card>
        )}

        <Card className="!rounded-none space-y-4 border-[#2a2f63] bg-[#0b1230]/95 text-slate-100">
          <div className="inline-flex rounded border border-slate-600 bg-slate-950 p-0.5">
            <button
              type="button"
              className={cn(
                "min-w-[84px] px-3 py-1.5 text-xs font-semibold transition-all",
                tab === "single"
                  ? "bg-gradient-to-r from-indigo-500 to-fuchsia-500 text-white"
                  : "text-slate-300 hover:bg-slate-800 hover:text-white",
              )}
              onClick={() => setTab("single")}
            >
              {t("store.tabs.single")}
            </button>
            <button
              type="button"
              className={cn(
                "min-w-[84px] px-3 py-1.5 text-xs font-semibold transition-all",
                tab === "team"
                  ? "bg-gradient-to-r from-indigo-500 to-fuchsia-500 text-white"
                  : "text-slate-300 hover:bg-slate-800 hover:text-white",
              )}
              onClick={() => setTab("team")}
            >
              {t("store.tabs.team")}
            </button>
          </div>

          {catalogLoading && <Loading />}
          {catalogError && <ErrorBox>{catalogError}</ErrorBox>}
          {catalog && catalog.length === 0 && !catalogLoading && !catalogError && (
            <EmptyState icon={<StoreIcon className="h-10 w-10" />} title={t("store.catalog.empty")} />
          )}

          {catalog && catalog.length > 0 && (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
              {catalog.map((item) => {
                const isOwned = ownedSet.has(item.listingId);
                const running = actionState[item.listingId];
                return (
                  <div key={item.listingId} className="space-y-3 border border-slate-700 bg-slate-950/70 p-4">
                    <div className="flex items-center gap-3">
                      <ListingAvatar title={item.title} avatarUrl={item.avatarUrl} />
                      <div className="min-w-0">
                        <div className="truncate text-sm font-semibold text-slate-100">{item.title}</div>
                        <div className="text-xs text-slate-400">{t(`store.type.${item.type}`)}</div>
                      </div>
                    </div>
                    <p className="line-clamp-2 text-xs text-slate-300">{item.description || t("common.none")}</p>
                    {item.type === "team" && item.previewAgents.length > 0 && (
                      <div className="flex -space-x-2">
                        {item.previewAgents.slice(0, 5).map((agent) => (
                          <ListingAvatar key={agent.id} title={agent.name} avatarUrl={agent.avatarUrl} small />
                        ))}
                      </div>
                    )}
                    <div className="flex items-center justify-between gap-2">
                      <PriceBadge mode={item.pricing.mode} />
                      <button
                        type="button"
                        className={
                          isOwned
                            ? "inline-flex items-center justify-center border border-slate-600 bg-slate-800 px-3 py-2 text-sm font-medium text-slate-300"
                            : "inline-flex items-center justify-center border border-indigo-400/70 bg-gradient-to-r from-indigo-500 to-fuchsia-500 px-3 py-2 text-sm font-semibold text-white transition hover:from-indigo-400 hover:to-fuchsia-400"
                        }
                        disabled={isOwned || running === "join" || running === "purchase"}
                        onClick={() => void onAcquire(item)}
                      >
                        {isOwned
                          ? t("store.actions.owned")
                          : running
                          ? t("store.actions.loading")
                          : item.pricing.mode === "free"
                          ? t("store.actions.join")
                          : t("store.actions.purchase")}
                      </button>
                    </div>
                    {!account && <p className="text-[11px] text-cyan-200/70">{t("store.auth.actionNeedsLogin")}</p>}
                  </div>
                );
              })}
            </div>
          )}
        </Card>
      </div>

      <Modal
        open={loginOpen}
        onClose={() => {
          if (loginLoading) return;
          setLoginOpen(false);
          setLoginError(null);
        }}
        title={t("store.auth.loginSimple")}
        width="max-w-md"
      >
        <form className="space-y-3" onSubmit={(e) => void onSubmitLogin(e)}>
          <div>
            <label className="label">{t("store.auth.email")}</label>
            <input
              className="input !text-slate-700"
              value={loginEmail}
              onChange={(e) => setLoginEmail(e.target.value)}
              placeholder={t("store.auth.emailPlaceholder")}
              disabled={loginLoading}
            />
          </div>
          <div>
            <label className="label">{t("store.auth.password")}</label>
            <input
              className="input !text-slate-700"
              type="password"
              value={loginPassword}
              onChange={(e) => setLoginPassword(e.target.value)}
              placeholder={t("store.auth.passwordPlaceholder")}
              disabled={loginLoading}
            />
          </div>
          <div className="flex justify-between">
            <button
              type="button"
              className="text-xs text-slate-400 underline decoration-slate-500 underline-offset-2 hover:text-slate-200"
              onClick={() => {
                setLoginOpen(false);
                setForgotOpen(true);
                setForgotEmail(loginEmail.trim());
              }}
            >
              {t("store.auth.forgotPassword")}
            </button>
            <button
              type="button"
              className="text-xs text-slate-400 underline decoration-slate-500 underline-offset-2 hover:text-slate-200"
              onClick={() => {
                setLoginOpen(false);
                setRegisterOpen(true);
                setRegisterEmail(loginEmail.trim());
              }}
            >
              {t("store.auth.goRegister")}
            </button>
          </div>
          {loginError && <ErrorBox>{loginError}</ErrorBox>}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                if (loginLoading) return;
                setLoginOpen(false);
                setLoginError(null);
              }}
              disabled={loginLoading}
            >
              {t("common.cancel")}
            </button>
            <button
              type="submit"
              className="btn-primary"
              disabled={loginLoading || !loginEmail.trim() || !loginPassword.trim()}
            >
              {loginLoading ? t("store.actions.loading") : t("store.auth.loginSimple")}
            </button>
          </div>
        </form>
      </Modal>

      <Modal
        open={registerOpen}
        onClose={() => {
          if (registerLoading) return;
          setRegisterOpen(false);
          setRegisterError(null);
        }}
        title={t("store.auth.register")}
        width="max-w-md"
      >
        <form className="space-y-3" onSubmit={(e) => void onSubmitRegister(e)}>
          <div>
            <label className="label">{t("store.auth.email")}</label>
            <input
              className="input !text-slate-700"
              value={registerEmail}
              onChange={(e) => setRegisterEmail(e.target.value)}
              placeholder={t("store.auth.emailPlaceholder")}
              disabled={registerLoading}
            />
          </div>
          <div>
            <label className="label">{t("store.auth.displayName")}</label>
            <input
              className="input !text-slate-700"
              value={registerDisplayName}
              onChange={(e) => setRegisterDisplayName(e.target.value)}
              placeholder={t("store.auth.displayNamePlaceholder")}
              disabled={registerLoading}
            />
          </div>
          <div>
            <label className="label">{t("store.auth.password")}</label>
            <input
              className="input !text-slate-700"
              type="password"
              value={registerPassword}
              onChange={(e) => setRegisterPassword(e.target.value)}
              placeholder={t("store.auth.passwordPlaceholder")}
              disabled={registerLoading}
            />
          </div>
          <div>
            <label className="label">{t("store.auth.confirmPassword")}</label>
            <input
              className="input !text-slate-700"
              type="password"
              value={registerConfirmPassword}
              onChange={(e) => setRegisterConfirmPassword(e.target.value)}
              placeholder={t("store.auth.confirmPasswordPlaceholder")}
              disabled={registerLoading}
            />
          </div>
          <div>
            <div className="mb-1 flex items-center justify-between gap-3">
              <label className="label !mb-0">{t("store.auth.verifyCode")}</label>
              <button
                type="button"
                className="btn-outline !h-7 !px-2 !py-0 !text-[11px]"
                disabled={
                  registerLoading ||
                  !registerEmail.trim() ||
                  !registerPassword.trim()
                }
                onClick={() => void onSendRegisterCode()}
              >
                {t("store.auth.sendVerifyCode")}
              </button>
            </div>
            <input
              className="input !text-slate-700"
              value={registerCode}
              onChange={(e) => setRegisterCode(e.target.value)}
              placeholder={t("store.auth.verifyCodePlaceholder")}
              disabled={registerLoading}
            />
          </div>
          {registerNotice && (
            <div className="border border-emerald-300/50 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
              {registerNotice}
            </div>
          )}
          {registerError && <ErrorBox>{registerError}</ErrorBox>}
          <div className="flex justify-end gap-2">
            <button
              type="submit"
              className="btn-primary"
              disabled={
                registerLoading ||
                !registerEmail.trim() ||
                !registerPassword.trim() ||
                !registerConfirmPassword.trim() ||
                !registerCode.trim()
              }
            >
              {registerLoading ? t("store.actions.loading") : t("store.auth.register")}
            </button>
          </div>
        </form>
      </Modal>

      <Modal
        open={forgotOpen}
        onClose={() => {
          if (forgotLoading) return;
          setForgotOpen(false);
          setForgotError(null);
        }}
        title={t("store.auth.forgotPassword")}
        width="max-w-md"
      >
        <div className="space-y-3">
          <div>
            <label className="label">{t("store.auth.email")}</label>
            <input
              className="input !text-slate-700"
              value={forgotEmail}
              onChange={(e) => setForgotEmail(e.target.value)}
              placeholder={t("store.auth.emailPlaceholder")}
              disabled={forgotLoading}
            />
          </div>
          <div className="flex justify-end">
            <button
              type="button"
              className="btn-outline"
              disabled={forgotLoading || !forgotEmail.trim()}
              onClick={() => void onSendForgotEmail()}
            >
              {t("store.auth.sendResetMail")}
            </button>
          </div>
          <div>
            <label className="label">{t("store.auth.verifyCode")}</label>
            <input
              className="input !text-slate-700"
              value={forgotCode}
              onChange={(e) => setForgotCode(e.target.value)}
              placeholder={t("store.auth.verifyCodePlaceholder")}
              disabled={forgotLoading}
            />
          </div>
          <div>
            <label className="label">{t("store.auth.newPassword")}</label>
            <input
              className="input !text-slate-700"
              type="password"
              value={forgotNewPassword}
              onChange={(e) => setForgotNewPassword(e.target.value)}
              placeholder={t("store.auth.newPasswordPlaceholder")}
              disabled={forgotLoading}
            />
          </div>
          {forgotNotice && (
            <div className="border border-emerald-300/50 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-200">
              {forgotNotice}
            </div>
          )}
          {forgotError && <ErrorBox>{forgotError}</ErrorBox>}
          <div className="flex justify-end">
            <button
              type="button"
              className="btn-primary"
              disabled={
                forgotLoading ||
                !forgotEmail.trim() ||
                !forgotCode.trim() ||
                !forgotNewPassword.trim()
              }
              onClick={() => void onResetPassword()}
            >
              {forgotLoading ? t("store.actions.loading") : t("store.auth.resetPassword")}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

function PriceBadge({ mode }: { mode: "free" | "paid" }) {
  const { t } = useTranslation();
  if (mode === "free") {
    return (
      <span className="border border-emerald-400/50 bg-emerald-500/10 px-2 py-0.5 text-xs font-medium text-emerald-200">
        {t("store.pricing.free")}
      </span>
    );
  }
  return (
    <span className="border border-amber-400/50 bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-200">
      {t("store.pricing.paid")}
    </span>
  );
}

function ListingAvatar({
  title,
  avatarUrl,
  small = false,
}: {
  title: string;
  avatarUrl?: string;
  small?: boolean;
}) {
  const base = small
    ? "h-8 w-8 rounded-full border border-slate-500/80"
    : "h-12 w-12 border border-slate-500/80";
  if (avatarUrl) {
    return <img src={avatarUrl} alt={title} className={cn(base, "object-cover bg-slate-800")} />;
  }
  return (
    <span
      className={cn(
        base,
        "inline-flex items-center justify-center bg-gradient-to-br from-indigo-500/30 to-fuchsia-500/30 text-indigo-200",
      )}
    >
      <StoreIcon className={small ? "h-4 w-4" : "h-5 w-5"} />
    </span>
  );
}

