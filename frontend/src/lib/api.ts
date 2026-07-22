/**
 * Typed REST client for the ClawsomeFlow backend (see API.md).
 *
 * - Always uses relative URLs so the Vite proxy / production reverse proxy
 *   can route them transparently.
 * - All payloads use camelCase exactly as the backend serialises them
 *   (Pydantic `to_camel` on the wire models — no manual case conversion
 *   here).
 * - Error responses follow the canonical `{error, message, details}`
 *   shape; we wrap them in `ApiError` so callers can `instanceof` check.
 */

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

/** True when the failure is "couldn't reach the ClawsomeFlow service at all"
 * (fetch threw), as opposed to a platform/runtime being unavailable. */
export function isNetworkError(e: unknown): boolean {
  return e instanceof ApiError && e.code === "NETWORK_ERROR";
}

interface ErrorEnvelope {
  error?: string;
  message?: string;
  details?: Record<string, unknown>;
  detail?: unknown;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * FastAPI/Starlette validation errors use `{detail: [{loc, msg, ...}]}`.
 * Convert the first item to a compact human-readable line.
 */
function formatFastApiDetail(detail: unknown): string | null {
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0];
    if (!isRecord(first)) return null;
    const msg = typeof first.msg === "string" ? first.msg.trim() : "";
    const loc = Array.isArray(first.loc)
      ? first.loc
          .map((p) => String(p))
          .filter((seg) => seg && seg !== "body")
          .join(".")
      : "";
    if (msg && loc) return `${loc}: ${msg}`;
    if (msg) return msg;
  }
  return null;
}

async function request<T>(
  method: string,
  url: string,
  body?: unknown,
  init: RequestInit = {},
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(init.headers ?? {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      ...init,
    });
  } catch (e) {
    // fetch only throws on network-level failure (backend/ClawsomeFlow service
    // unreachable) — NOT on HTTP error statuses. Surface this as a typed error
    // so callers can show the real reason instead of "platform unavailable".
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, "NETWORK_ERROR", e instanceof Error ? e.message : String(e));
  }
  if (res.status === 204) {
    // @ts-expect-error — caller should type-guard their T
    return undefined;
  }
  const text = await res.text();
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!res.ok) {
    const err: ErrorEnvelope = isRecord(parsed) ? parsed : {};
    const detailMessage = formatFastApiDetail(err.detail);
    const code =
      (typeof err.error === "string" && err.error.trim())
        ? err.error
        : (detailMessage ? "VALIDATION_ERROR" : "UNKNOWN");
    const message =
      (typeof err.message === "string" && err.message.trim())
        ? err.message
        : (detailMessage ?? `HTTP ${res.status}`);
    const details = isRecord(err.details)
      ? err.details
      : err.detail !== undefined
      ? { detail: err.detail }
      : {};
    throw new ApiError(
      res.status,
      code,
      message,
      details,
    );
  }
  return parsed as T;
}

function apiErrorFromResponse(res: Response, parsed: unknown): ApiError {
  const err: ErrorEnvelope = isRecord(parsed) ? parsed : {};
  const detailMessage = formatFastApiDetail(err.detail);
  const code =
    (typeof err.error === "string" && err.error.trim())
      ? err.error
      : (detailMessage ? "VALIDATION_ERROR" : "UNKNOWN");
  const message =
    (typeof err.message === "string" && err.message.trim())
      ? err.message
      : (detailMessage ?? `HTTP ${res.status}`);
  const details = isRecord(err.details)
    ? err.details
    : err.detail !== undefined
    ? { detail: err.detail }
    : {};
  return new ApiError(res.status, code, message, details);
}

async function uploadBinary<T>(
  url: string,
  file: File,
  init: RequestInit = {},
): Promise<T> {
  let res: Response;
  const contentType = file.type || "application/octet-stream";
  try {
    res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": contentType,
        ...(init.headers ?? {}),
      },
      body: file,
      ...init,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, "NETWORK_ERROR", e instanceof Error ? e.message : String(e));
  }
  const text = await res.text();
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!res.ok) throw apiErrorFromResponse(res, parsed);
  return parsed as T;
}

// ── Domain types ──────────────────────────────────────────────────────

export type AgentKind =
  | "claude"
  | "codex"
  | "cursor"
  | "openclaw"
  | "kimi"
  | "nanobot"
  | "gemini"
  | "qwen"
  | "opencode"
  | "pi"
  | "qoder"
  | "codebuddy"
  | "hermes"
  | "custom"
  | "external";

export type ExternalChannel = "human" | "webhook" | "remote_csflow";

/** Channel configuration for kind=external agents (external execution nodes). */
export interface ExternalNodeConfig {
  channel: ExternalChannel;
  /** webhook channel: outbound dispatch endpoint. */
  endpointUrl?: string | null;
  /** remote_csflow channel: remote instance base URL. */
  baseUrl?: string | null;
  /** remote_csflow channel: remote Flow id to delegate. */
  flowId?: string | null;
  /** remote_csflow channel: outbound credential name in local config. */
  pairTokenRef?: string | null;
  /** remote_csflow channel: user-typed run-input params for the remote Flow
   *  (its param fields); merged (user wins) over upstream-reported values at
   *  dispatch time and sent as the delegate request's `inputs`. */
  inputs?: Record<string, string> | null;
  /** remote_csflow channel: the remote Flow's declared param-field NAMES,
   *  captured from its "remote call info". Names only — never secrets. */
  remoteParamFields?: string[] | null;
  /** remote_csflow channel: peer Flow display name (from remote call info). */
  remoteFlowName?: string | null;
  /** remote_csflow channel: peer Flow overall goal / description. */
  remoteFlowDescription?: string | null;
  /** human channel: display-only assignee hint. */
  assignee?: string | null;
}

/** Paste-able "remote call info" produced by a peer Flow's editor. */
export interface RemoteCallInfo {
  schemaVersion?: number;
  kind?: string;
  baseUrl: string;
  flowId: string;
  flowName: string;
  flowDescription?: string;
  paramFields: string[];
  pairTokenName: string;
  pairSecret: string;
}

/** Origin-side registration result (secret stripped, non-secret fields only). */
export interface RegisterRemoteResponse {
  baseUrl: string;
  flowId: string;
  flowName: string;
  flowDescription?: string;
  paramFields: string[];
  pairTokenRef: string;
}

export type MergeStrategy = "manual" | "auto" | "skip" | "agent_self";
export type OnFailure = "retry" | "skip" | "abort";

export type RunStatus =
  | "pending"
  | "compiling"
  | "running"
  | "awaiting_external"
  | "awaiting_user_checkpoint"
  | "awaiting_user_review"
  | "awaiting_user_complaint"
  | "complaint_processing"
  | "complaint_failed"
  | "completed"
  | "completed_with_conflicts"
  | "failed"
  | "aborted";

export interface FlowAgent {
  id: string;
  kind: AgentKind;
  profile?: string | null;
  command?: string[] | null;
  repo?: string | null;
  targetBranch?: string | null;
  isLeader: boolean;
  /** Temporary (ad-hoc) agent created inline while authoring the Flow, as
   *  opposed to a persistent/managed agent. Temporary agents get no profile
   *  and no `-p`/env injection at run time. OpenClaw cannot be temporary. */
  isTemporary?: boolean;
  mergeStrategy?: MergeStrategy;
  onFailure?: OnFailure;
  maxRetries?: number;
  disposeAfterDone?: boolean;
  /** Channel config for kind=external (required for that kind). */
  external?: ExternalNodeConfig | null;
}

export interface FlowTask {
  id: string;
  ownerAgentId: string;
  subject: string;
  /** Detailed task instructions (excluding output-summary requirement; split by backend). */
  description?: string;
  /** Output-summary requirement (backend handles merge + persistence; frontend only renders two fields). */
  outputSummaryRequirement?: string | null;
  requiresHumanCheckpoint?: boolean;
  /** Developer-mode per-task auto-merge switch (only meaningful in 开发者模式).
   *  Omitted / undefined defaults to true (auto-merge enabled). */
  devAutoMerge?: boolean;
  dependsOn?: string[];
  isLeaderSummary?: boolean;
  timeoutSeconds?: number;
}

export interface FlowSpec {
  agents: FlowAgent[];
  tasks: FlowTask[];
  variables?: Record<string, string>;
}

export interface FlowSummary {
  id: string;
  name: string;
  description: string;
  version: number;
  ownerUser: string;
  updatedAt: string;
  /** Distinct agent kinds present in this Flow's spec. Older backends may
   *  omit this. Surfaced through the API but the list UI no longer
   *  renders flow-level kind badges (kind is a per-task concern). */
  agentKinds?: string[];
  leaderAgentId?: string | null;
  leaderKind?: string | null;
  /** True when spec.variables csflow.easy_mode is enabled (省心模式). */
  easyMode?: boolean;
  /** True when spec.variables csflow.dev_mode is enabled (开发者模式). */
  devMode?: boolean;
  /** Number of webhook notification channels configured on this Flow
   *  (spec.variables csflow.notify_webhooks). >0 → highlight the notify
   *  button in the list. Older backends may omit this (treated as 0). */
  notifyChannelCount?: number;
}

export interface FlowDetail extends FlowSummary {
  cleanupTeamOnFinish: boolean;
  spec: FlowSpec;
  createdAt: string;
}

export interface FlowSaveWarning {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface FlowSaveResult {
  id: string;
  version: number;
  warnings?: FlowSaveWarning[];
}

export interface RunSummary {
  id: string;
  flowId: string;
  flowVersion: number;
  teamName: string;
  status: RunStatus;
  user: string;
  startedAt: string;
  finishedAt: string | null;
  inputs: Record<string, unknown>;
  /** True only for runs launched by a timed schedule (drives the "Scheduled" tag). */
  isScheduled: boolean;
}

export interface PendingMerge {
  agentId: string;
  branch: string;
  targetBranch?: string;
  diffSummary: Record<string, unknown>;
  leaderSuggestion: string;
}

export interface PendingMergeDiff {
  agentId: string;
  branch: string;
  baseBranch: string;
  targetBranch: string;
  repoRoot: string;
  patch: string;
  patchTruncated: boolean;
  uncommittedPatch: string;
  uncommittedTruncated: boolean;
  baseAhead: number;
  branchAhead: number;
}

export interface RunDetail extends RunSummary {
  inputs: Record<string, unknown>;
  pendingMerges: PendingMerge[] | null;
  clawteamBoardUrl: string | null;
  specSnapshot: FlowSpec;
}

/** One agent's merged-into-baseline summary in the post-run "Run diff" module. */
export interface RunDiffAgent {
  agentId: string;
  branch: string;
  repoRoot: string;
  mergeCount: number;
  commitCount: number;
  filesChanged: number;
  insertions: number;
  deletions: number;
}

/** Full unified diff of what one agent merged into a baseline this run. */
export interface RunAgentDiff extends RunDiffAgent {
  patch: string;
  patchTruncated: boolean;
}

/** Result of the "撤销合入" (revert-merge) action for one agent. */
export interface RunMergeRevert {
  agentId: string;
  ok: boolean;
  targetBranch: string;
  revertedMerges: string[];
  revertHead: string;
  message: string;
}

/** One dev-mode agent whose worktree awaits a PR decision (PR module). */
export interface PendingPrAgent {
  agentId: string;
  branch: string;
  baseBranch: string;
  targetBranch: string;
  repoRoot: string;
  worktreePath: string;
}

/** Result of the one-click "PR to baseline branch" action. */
export interface PendingPrSubmitResult {
  agentId: string;
  success: boolean;
  prUrl: string;
  message: string;
}

export interface RunTaskTerminal {
  taskId: string;
  subject: string;
  ownerAgentId: string;
  ownerKind: string | null;
  tmuxTarget: string;
  workDir: string;
  paneText: string;
  available: boolean;
}

export interface RunTaskTerminalPane {
  ownerAgentId: string;
  paneText: string;
  available: boolean;
}

export interface RunEventView {
  id: number;
  ts: string;
  type: string;
  agentId: string | null;
  taskId: string | null;
  payload: Record<string, unknown>;
}

export interface OperationStatus {
  opId: string;
  state: "running" | "succeeded" | "failed" | "not_found";
  kind: string;
  detail: string;
  result: Record<string, unknown>;
  source: string;
  inFlight: boolean;
}

export interface RunScheduleItem {
  flowId: string;
  inputs: Record<string, unknown>;
}

export interface RunScheduleSummary {
  id: string;
  name: string;
  runMode: "parallel" | "serial";
  executeMode: "once" | "recurring";
  intervalDays: number | null;
  nextRunAt: string;
  items: RunScheduleItem[];
  createdAt: string;
  updatedAt: string;
}

export interface RunScheduleExecutionItem {
  index: number;
  flowId: string;
  flowName: string;
  status: string;
  reason: string;
  reasonCode: string;
  runId: string;
}

export interface RunScheduleExecutionSummary {
  id: string;
  scheduleId: string;
  scheduleName: string;
  runMode: "parallel" | "serial";
  executeMode: "once" | "recurring";
  status: string;
  totalItems: number;
  succeededItems: number;
  failedItems: number;
  skippedItems: number;
  startedAt: string;
  finishedAt: string | null;
}

export interface RunScheduleExecutionDetail extends RunScheduleExecutionSummary {
  runIds: string[];
  itemResults: RunScheduleExecutionItem[];
}

export interface OpenclawAgentSummary {
  id: string;
  name: string;
  description: string;
  teamId: string;
  teamName: string;
  workspacePath: string;
  createdByUser: string;
  createdAt: string;
}

export interface OpenclawAgentDetail extends OpenclawAgentSummary {
  nlPrompt: string;
  openclawConfigSnapshot: Record<string, unknown>;
}

export interface OpenclawRuntimeStatus {
  running: boolean;
  reason: string;
  gatewayUrl?: string | null;
}

export type OpenclawRuntimeProbeMode = "fast" | "strict";

export interface OpenclawAgentSkillSetting {
  name: string;
  description: string;
  content: string;
  path: string;
}

export interface OpenclawAgentCronSetting {
  id: string;
  agentId: string;
  name: string;
  enabled: boolean;
  scheduleExpr: string;
  scheduleTz: string;
  message: string;
  source: string;
  systemBuiltin: boolean;
  canEdit: boolean;
  canDelete: boolean;
}

export interface OpenclawAgentHookSetting {
  name: string;
  description: string;
  source: string;
  events: string[];
  enabled: boolean;
  eligible?: boolean | null;
  requirementsSatisfied?: boolean | null;
  managedByPlugin?: boolean | null;
  systemBuiltin: boolean;
  canEdit: boolean;
  canDelete: boolean;
  hookMd?: string | null;
  handlerTs?: string | null;
}

export interface OpenclawAgentSettings {
  agentId: string;
  skills: OpenclawAgentSkillSetting[];
  cronJobs: OpenclawAgentCronSetting[];
  hooks: OpenclawAgentHookSetting[];
  agentsUserCustomSection: string;
}

export interface OpenclawRestorableAgent {
  id: string;
  name: string;
  description: string;
  teamId: string;
  teamName: string;
  workspacePath: string;
  createdByUser: string;
}

export type OpenclawAgentRemoveMode = "unregister" | "purge";

export interface ExternalOpenclawImportCandidate {
  id: string;
  name: string;
  description: string;
  workspacePath: string;
}

export interface ExternalOpenclawImportResult {
  sourceAgentId: string;
  sourceAgentName: string;
  targetAgentId: string;
  targetAgentName: string;
  targetWorkspacePath: string;
  targetTeamId: string;
  targetTeamName: string;
  optimizationScheduled: boolean;
}

export interface ExternalOpenclawImportFailure {
  sourceAgentId: string;
  errorCode: string;
  message: string;
}

export interface OpenclawTeam {
  id: string;
  name: string;
  createdByUser: string;
  createdAt: string;
}

export type AgentStoreListingType = "single" | "team";
export type AgentStorePricingMode = "free" | "paid";

export interface AgentStorePricing {
  mode: AgentStorePricingMode;
  currency: string;
  amount: number;
}

export interface AgentStorePreviewAgent {
  id: string;
  name: string;
  avatarUrl: string;
}

export interface AgentStoreCatalogItem {
  listingId: string;
  type: AgentStoreListingType;
  title: string;
  description: string;
  avatarUrl: string;
  manifestPath: string;
  pricing: AgentStorePricing;
  previewAgents: AgentStorePreviewAgent[];
}

export interface AgentStoreOwnedItem {
  listingId: string;
  type: AgentStoreListingType;
  title: string;
  description: string;
  avatarUrl: string;
  pricing: AgentStorePricing;
  acquiredVia: "join" | "purchase";
  acquiredAt: string;
  sourceManifestPath: string;
}

export interface AgentStoreAcquireResponse {
  owned: AgentStoreOwnedItem;
  order?: {
    id: string;
    status: "pending" | "succeeded" | "failed";
    currency: string;
    amount: number;
    isMock: boolean;
    paymentProvider?: string | null;
    externalPaymentId?: string | null;
    createdAt: string;
  } | null;
}

export interface AgentStoreLoadResponse {
  listingId: string;
  listingType: AgentStoreListingType;
  loadedAgentIds: string[];
  teamId: string | null;
}

export interface AgentStoreAccount {
  id: string;
  displayName: string;
  email: string;
  avatarUrl: string;
}

export interface AgentStoreLoginResponse {
  accessToken: string;
  tokenType: string;
  expiresAt?: string | null;
  account: AgentStoreAccount;
}

export interface AgentStoreAuthActionResponse {
  status: string;
  message: string;
}

export interface ChatHistoryMessage {
  role: "system" | "user" | "assistant";
  content: string;
  attachments?: ChatAttachmentMeta[];
  /** Epoch ms the message was recorded server-side (authoritative timestamp). */
  ts?: number;
  /** Stable server id — the UI keys render + dedup off it. */
  id?: number;
  /** "session_divider" for the persistent reset marker; normal messages omit it. */
  kind?: string;
}

export type ChatAttachmentRoute = "path_injection" | "native";

export interface ChatAttachmentMeta {
  id: string;
  name: string;
  mimeType: string;
  sizeBytes: number;
  absolutePath: string;
  relativePath: string;
  route: ChatAttachmentRoute;
}

export interface ChatAttachmentUploadResult {
  attachment: ChatAttachmentMeta;
  limits: {
    maxCount: number;
    maxBytesPerFile: number;
    maxTotalBytes: number;
  };
}

/** One progress entry for an in-flight chat turn (formatted via i18n). Shared by
 *  the OpenClaw and Hermes single-agent chat pages. */
export interface ChatStep {
  kind: "tool" | "info";
  name?: string;
  seq: number;
}

export interface ChatProgress {
  toolCalls: number;
  apiCalls: number;
  messageCount: number;
  elapsedSec: number;
}

/** Live turn state for reconnect after a tab switch / refresh. */
export interface ChatStatus {
  status: "idle" | "running" | "done" | "error";
  steps: ChatStep[];
  progress: ChatProgress | null;
  final: string;
  error: string;
  startedAtMono: number | null;
}

export interface ProfileSummary {
  name: string;
  agent?: string | null;
  model?: string | null;
  baseUrl?: string | null;
  description?: string | null;
}

/** Mirror of backend ``ProfileSetPayload`` — all fields optional. */
export interface ProfileSetPayload {
  agent?: string | null;
  description?: string | null;
  command?: string | null;
  model?: string | null;
  baseUrl?: string | null;
  baseUrlEnv?: string | null;
  apiKeyEnv?: string | null;
  apiKeyTargetEnv?: string | null;
  envs?: string[];
  envMaps?: string[];
  args?: string[];
}

export interface DecomposeStartResponse {
  requestId: string;
  status: string;
  tokenTtlSeconds: number;
  statusUrl: string;
}

export interface DecomposeStatus {
  requestId: string;
  status:
    | "pending"
    | "dispatched"
    | "succeeded"
    | "failed"
    | "timed_out";
  goal: string;
  leaderAgentId: string;
  existingAgents: Record<string, unknown>[];
  existingTasks: Record<string, unknown>[];
  resultAgents: Record<string, unknown>[] | null;
  resultTasks: Record<string, unknown>[] | null;
  errorCode: string | null;
  errorMessage: string | null;
  createdAt: string;
  updatedAt: string;
  expiresAt: string;
}

export interface WorkspaceDirectoryList {
  items: string[];
}

export interface UiCapabilities {
  nativeDirectoryUiAvailable: boolean;
  nativeDirectoryClientColocated: boolean;
  userHomeDir: string;
}

export interface OwnerKindsFast {
  persistentKinds: string[];
  temporaryKinds: string[];
}

export interface EnsureGitRepoResult {
  path: string;
  pathExists: boolean;
  isGitRepo: boolean;
  hasInitialCommit: boolean;
  createdDir: boolean;
  initializedRepo: boolean;
  createdInitialCommit: boolean;
  /** Present when `isGitRepo`; reflects the repository's current HEAD branch. */
  currentBranch?: string | null;
}

export interface RepoBranchesResult {
  path: string;
  pathExists: boolean;
  isGitRepo: boolean;
  editable: boolean;
  currentBranch: string;
  branches: string[];
}

// ── API surface ───────────────────────────────────────────────────────

export interface UpdateStatus {
  enabled: boolean;
  currentVersion: string;
  latestVersion: string | null;
  updateAvailable: boolean;
  isPrerelease: boolean;
  upgradeScriptUrl: string;
}

export interface TriggerUpgradeResult {
  started: boolean;
  targetVersion: string | null;
  via: string;
}

export interface ActiveRunView {
  id: string;
  flowId: string;
  status: string;
  startedAt: string;
}

export interface ActiveRunsResult {
  count: number;
  runs: ActiveRunView[];
}

/** One per-Flow webhook channel. `format` null/"auto" = detect by URL host;
 *  `effectiveFormat` is the resolved format the next notification will use. */
export interface FlowWebhookChannel {
  url: string;
  format: string | null;
  effectiveFormat?: string | null;
}

export interface FlowWebhookConfig {
  channels: FlowWebhookChannel[];
}

/** Webhook format ids accepted by the backend ("auto" = detect by URL). */
export const NOTIFY_WEBHOOK_FORMATS = [
  "auto",
  "generic",
  "feishu",
  "dingtalk",
  "wecom",
  "slack",
  "discord",
  "teams",
  "googlechat",
  "telegram",
  "ntfy",
  "bark",
  "serverchan",
  "gotify",
] as const;

// ── Hermes agents ───────────────────────────────────────────────────────
export interface HermesAgentSummary {
  id: string;
  name: string;
  description: string;
  teamId: string;
  teamName: string;
  profileRoot: string;
  createdByUser: string;
  createdAt: string;
}

export interface HermesAgentDetail extends HermesAgentSummary {
  nlPrompt: string;
}

export interface HermesAgentCreateResult extends HermesAgentDetail {
  // Non-empty ⇒ the agent was created but its self-definition bootstrap
  // (`hermes -z`) did not complete (e.g. no inference provider configured).
  bootstrapWarning?: string;
}

export interface HermesClaimableAgent {
  id: string;
  description: string;
}

export interface HermesModelSetting {
  default: string;
  provider: string;
  baseUrl: string;
}

export interface HermesGatewaySetting {
  cwd: string;
}

export interface HermesSecretSetting {
  key: string;
  preview: string;
  isSet: boolean;
}

export interface HermesSkillSetting {
  name: string;
  description: string;
  path: string;
  content?: string | null;
}

export interface HermesCronJob {
  id: string;
  name: string;
  schedule: string;
  enabled: boolean;
  prompt: string;
  deliver: string;
  workdir: string;
  nextRun: string;
  lastRun: string;
  detail: string;
  raw: string;
}

export interface HermesCronDeliveryTarget {
  value: string;
  label: string;
}

export interface HermesMcpServer {
  name: string;
  transport: "http_sse" | "sse" | "local";
  url: string;
  command: string;
  args: string[];
  enabled: boolean;
  envKeys: string[];
}

export const api = {
  // Flows
  listFlows: () =>
    request<{ items: FlowSummary[]; total: number }>("GET", "/api/flows"),
  getFlow: (id: string) => request<FlowDetail>("GET", `/api/flows/${id}`),
  createFlow: (payload: {
    name: string;
    description?: string;
    cleanupTeamOnFinish?: boolean;
    spec: FlowSpec;
  }) =>
    request<FlowSaveResult>("POST", "/api/flows", payload),
  updateFlow: (
    id: string,
    payload: {
      version: number;
      name: string;
      description?: string;
      cleanupTeamOnFinish?: boolean;
      spec: FlowSpec;
    },
  ) =>
    request<FlowSaveResult>("PUT", `/api/flows/${id}`, payload),
  deleteFlow: (id: string) => request<void>("DELETE", `/api/flows/${id}`),

  // External remote-node one-click wiring
  remoteCallInfo: (id: string) =>
    request<RemoteCallInfo>("POST", `/api/flows/${id}/remote-call-info`, {}),
  registerRemoteTarget: (info: RemoteCallInfo) =>
    request<RegisterRemoteResponse>("POST", "/api/flows/remote-targets", info),

  // Runs
  triggerRun: (
    flowId: string,
    payload: { inputs?: Record<string, unknown>; runtimePrompt?: string } = {},
  ) =>
    request<{ id: string; status: string; teamName: string }>(
      "POST",
      `/api/flows/${flowId}/runs`,
      {
        inputs: payload.inputs ?? {},
        runtimePrompt: payload.runtimePrompt,
      },
    ),
  listRuns: (params: { flowId?: string; status?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.flowId) q.set("flowId", params.flowId);
    if (params.status) q.set("status", params.status);
    const qs = q.toString();
    return request<{ items: RunSummary[]; total: number }>(
      "GET",
      `/api/runs${qs ? `?${qs}` : ""}`,
    );
  },
  getRun: (id: string) => request<RunDetail>("GET", `/api/runs/${id}`),
  getRunCheckpoint: (id: string) =>
    request<Record<string, unknown> | null>(
      "GET",
      `/api/runs/${id}/checkpoint`,
    ),
  listRunTerminals: (id: string, historyLines = 120) => {
    const q = new URLSearchParams();
    q.set("historyLines", String(historyLines));
    return request<{ items: RunTaskTerminal[] }>(
      "GET",
      `/api/runs/${id}/terminals?${q.toString()}`,
    );
  },
  listRunTerminalsMeta: (id: string) =>
    request<{ items: RunTaskTerminal[] }>(
      "GET",
      `/api/runs/${id}/terminals/meta`,
    ),
  getRunTerminalPane: (id: string, ownerAgentId: string, historyLines = 120) => {
    const q = new URLSearchParams();
    q.set("historyLines", String(historyLines));
    return request<RunTaskTerminalPane>(
      "GET",
      `/api/runs/${id}/terminals/panes/${encodeURIComponent(ownerAgentId)}?${q.toString()}`,
    );
  },
  listRunEvents: (id: string, sinceId?: number, limit = 200) => {
    const q = new URLSearchParams();
    if (sinceId !== undefined) q.set("sinceId", String(sinceId));
    q.set("limit", String(limit));
    return request<{ items: RunEventView[]; nextSinceId: number | null }>(
      "GET",
      `/api/runs/${id}/events?${q.toString()}`,
    );
  },
  abortRun: (id: string) =>
    request<RunSummary>("POST", `/api/runs/${id}/abort`),
  clearRunHistory: () =>
    request<{ runsDeleted: number; eventsDeleted: number }>(
      "DELETE",
      "/api/runs/history",
    ),
  mergePending: (id: string, agentId: string) =>
    request<{ agentId: string; success: boolean; message: string }>(
      "POST",
      `/api/runs/${id}/merge`,
      { agentId },
    ),
  dismissPending: (id: string, agentId: string) =>
    request<RunSummary>("POST", `/api/runs/${id}/dismiss-merge`, { agentId }),
  getPendingMergeDiff: (id: string, agentId: string) =>
    request<PendingMergeDiff>(
      "GET",
      `/api/runs/${id}/pending-merges/${encodeURIComponent(agentId)}/diff`,
    ),
  getRunDiff: (id: string) =>
    request<{ items: RunDiffAgent[] }>("GET", `/api/runs/${id}/run-diff`),
  getRunAgentDiff: (id: string, agentId: string) =>
    request<RunAgentDiff>(
      "GET",
      `/api/runs/${id}/run-diff/${encodeURIComponent(agentId)}`,
    ),
  revertRunAgentMerge: (id: string, agentId: string) =>
    request<RunMergeRevert>(
      "POST",
      `/api/runs/${id}/run-diff/${encodeURIComponent(agentId)}/revert`,
    ),
  getPendingPrs: (id: string) =>
    request<{ items: PendingPrAgent[] }>("GET", `/api/runs/${id}/pending-prs`),
  getPendingPrDiff: (id: string, agentId: string) =>
    request<PendingMergeDiff>(
      "GET",
      `/api/runs/${id}/pending-prs/${encodeURIComponent(agentId)}/diff`,
    ),
  submitPendingPr: (id: string, agentId: string) =>
    request<PendingPrSubmitResult>(
      "POST",
      `/api/runs/${id}/pending-prs/${encodeURIComponent(agentId)}/submit`,
    ),
  mergePendingPr: (id: string, agentId: string) =>
    request<{ agentId: string; success: boolean; message: string }>(
      "POST",
      `/api/runs/${id}/pending-prs/${encodeURIComponent(agentId)}/merge`,
    ),
  getFailedAutoMerges: (id: string) =>
    request<{ items: PendingPrAgent[] }>("GET", `/api/runs/${id}/failed-auto-merges`),
  getFailedAutoMergeDiff: (id: string, agentId: string) =>
    request<PendingMergeDiff>(
      "GET",
      `/api/runs/${id}/failed-auto-merges/${encodeURIComponent(agentId)}/diff`,
    ),
  mergeFailedAutoMerge: (id: string, agentId: string) =>
    request<{ agentId: string; success: boolean; message: string }>(
      "POST",
      `/api/runs/${id}/failed-auto-merges/${encodeURIComponent(agentId)}/merge`,
    ),
  discardFailedAutoMerge: (id: string, agentId: string) =>
    request<RunSummary>(
      "POST",
      `/api/runs/${id}/failed-auto-merges/${encodeURIComponent(agentId)}/discard`,
    ),
  discardPendingPr: (id: string, agentId: string) =>
    request<RunSummary>(
      "POST",
      `/api/runs/${id}/pending-prs/${encodeURIComponent(agentId)}/discard`,
    ),
  submitRunComplaint: (id: string, message: string) =>
    request<RunSummary>("POST", `/api/runs/${id}/complaint`, { message }),
  skipRunComplaint: (id: string) =>
    request<RunSummary>("POST", `/api/runs/${id}/complaint/skip`),
  retryTask: (id: string, taskId: string) =>
    request<RunSummary>("POST", `/api/runs/${id}/retry-task/${taskId}`),
  approveCheckpointItem: (id: string, taskId: string) =>
    request<RunSummary>(
      "POST",
      `/api/runs/${id}/checkpoint/items/${taskId}/approve`,
    ),
  rerunCheckpointItem: (id: string, taskId: string, feedback: string) =>
    request<RunSummary>(
      "POST",
      `/api/runs/${id}/checkpoint/items/${taskId}/rerun`,
      { feedback },
    ),
  markCheckpointItemRead: (id: string, taskId: string) =>
    request<RunSummary>(
      "POST",
      `/api/runs/${id}/checkpoint/items/${taskId}/mark-read`,
    ),
  completeExternalTask: (
    id: string,
    taskId: string,
    status: "success" | "failed",
    summary: string,
  ) =>
    request<RunSummary>(
      "POST",
      `/api/runs/${id}/external-tasks/${taskId}/complete`,
      { status, summary },
    ),
  redispatchExternalTask: (id: string, taskId: string) =>
    request<RunSummary>(
      "POST",
      `/api/runs/${id}/external-tasks/${taskId}/redispatch`,
    ),
  getCheckpointItemDiff: (id: string, taskId: string) =>
    request<PendingMergeDiff>(
      "GET",
      `/api/runs/${id}/checkpoint/items/${taskId}/diff`,
    ),
  listRunSchedules: () =>
    request<{ items: RunScheduleSummary[]; total: number }>(
      "GET",
      "/api/run-schedules",
    ),
  getRunSchedule: (id: string) =>
    request<RunScheduleSummary>("GET", `/api/run-schedules/${id}`),
  createRunSchedule: (payload: {
    name: string;
    runMode: "parallel" | "serial";
    executeMode: "once" | "recurring";
    intervalDays?: number | null;
    runAt: string;
    items: { flowId: string; inputs?: Record<string, unknown> }[];
  }) =>
    request<RunScheduleSummary>("POST", "/api/run-schedules", payload),
  updateRunSchedule: (
    id: string,
    payload: {
      name: string;
      runMode: "parallel" | "serial";
      executeMode: "once" | "recurring";
      intervalDays?: number | null;
      runAt: string;
      items: { flowId: string; inputs?: Record<string, unknown> }[];
    },
  ) => request<RunScheduleSummary>("PATCH", `/api/run-schedules/${id}`, payload),
  deleteRunSchedule: (id: string) =>
    request<void>("DELETE", `/api/run-schedules/${id}`),
  listRunScheduleExecutions: (params: {
    scheduleId?: string;
    limit?: number;
    offset?: number;
  } = {}) => {
    const q = new URLSearchParams();
    if (params.scheduleId) q.set("scheduleId", params.scheduleId);
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    if (params.offset !== undefined) q.set("offset", String(params.offset));
    const qs = q.toString();
    return request<{ items: RunScheduleExecutionSummary[]; total: number }>(
      "GET",
      `/api/run-schedule-executions${qs ? `?${qs}` : ""}`,
    );
  },
  getRunScheduleExecution: (id: string) =>
    request<RunScheduleExecutionDetail>("GET", `/api/run-schedule-executions/${id}`),
  clearRunScheduleExecutions: () =>
    request<{ deleted: number }>("DELETE", "/api/run-schedule-executions"),

  // OpenClaw agents
  listOpenclawTeams: () =>
    request<{ items: OpenclawTeam[]; total: number }>(
      "GET",
      "/api/openclaw/agents/teams",
    ),
  createOpenclawTeam: (payload: { name: string }) =>
    request<OpenclawTeam>("POST", "/api/openclaw/agents/teams", payload),
  patchOpenclawTeam: (id: string, payload: { name: string }) =>
    request<OpenclawTeam>("PATCH", `/api/openclaw/agents/teams/${id}`, payload),
  createOpenclawAgent: (payload: {
    id: string;
    name: string;
    description?: string;
    teamId?: string | null;
    model?: string | null;
    identityEmoji?: string | null;
    identityTheme?: string | null;
    nlPrompt?: string;
    extraSkills?: string[];
  }, init?: RequestInit) =>
    request<OpenclawAgentDetail>("POST", "/api/openclaw/agents", payload, init),
  cancelOpenclawAgentCreate: (id: string) =>
    request<void>("POST", `/api/openclaw/agents/${id}/cancel-create`),
  listOpenclawAgents: (allUsers = false) =>
    request<{ items: OpenclawAgentSummary[] }>(
      "GET",
      `/api/openclaw/agents${allUsers ? "?allUsers=true" : ""}`,
    ),
  getOpenclawRuntimeStatus: (mode: OpenclawRuntimeProbeMode = "fast") =>
    request<OpenclawRuntimeStatus>(
      "GET",
      `/api/openclaw/agents/runtime/status?mode=${mode}`,
    ),
  listOpenclawRestoreCandidates: () =>
    request<{ items: OpenclawRestorableAgent[]; total: number }>(
      "GET",
      "/api/openclaw/agents/restore/candidates",
    ),
  restoreOpenclawAgent: (id: string) =>
    request<OpenclawAgentDetail>("POST", `/api/openclaw/agents/restore/${id}`),
  listOpenclawImportCandidates: () =>
    request<{ items: ExternalOpenclawImportCandidate[]; total: number }>(
      "GET",
      "/api/openclaw/agents/import/candidates",
    ),
  importOpenclawAgents: (
    payload: {
      agentIds?: string[];
      importAll?: boolean;
      teamId?: string | null;
      batchId?: string;
    },
    init?: RequestInit,
  ) =>
    request<{
      requestedCount: number;
      imported: ExternalOpenclawImportResult[];
      failed: ExternalOpenclawImportFailure[];
      cancelled: boolean;
    }>("POST", "/api/openclaw/agents/import", payload, init),
  cancelOpenclawImport: (batchId: string) =>
    request<void>("POST", `/api/openclaw/agents/import/${batchId}/cancel`),
  getOpenclawAgent: (id: string) =>
    request<OpenclawAgentDetail>("GET", `/api/openclaw/agents/${id}`),
  getOpenclawAgentSettings: (id: string) =>
    request<OpenclawAgentSettings>("GET", `/api/openclaw/agents/${id}/settings`),
  getOpenclawAgentSettingsSkills: (id: string) =>
    request<OpenclawAgentSkillSetting[]>("GET", `/api/openclaw/agents/${id}/settings/skills`),
  getOpenclawAgentSettingsCron: (id: string) =>
    request<OpenclawAgentCronSetting[]>("GET", `/api/openclaw/agents/${id}/settings/cron`),
  getOpenclawAgentSettingsHooks: (id: string) =>
    request<OpenclawAgentHookSetting[]>("GET", `/api/openclaw/agents/${id}/settings/hooks`),
  getOpenclawAgentCustomSection: (id: string) =>
    request<{ content: string }>("GET", `/api/openclaw/agents/${id}/settings/agents-custom-section`),
  createOpenclawAgentSkill: (
    id: string,
    payload: { name: string; description: string; content: string },
  ) =>
    request<OpenclawAgentSkillSetting>(
      "POST",
      `/api/openclaw/agents/${id}/settings/skills`,
      payload,
    ),
  patchOpenclawAgentSkill: (
    id: string,
    skillName: string,
    payload: { name?: string; description?: string; content?: string },
  ) =>
    request<OpenclawAgentSkillSetting>(
      "PATCH",
      `/api/openclaw/agents/${id}/settings/skills/${encodeURIComponent(skillName)}`,
      payload,
    ),
  deleteOpenclawAgentSkill: (id: string, skillName: string) =>
    request<void>(
      "DELETE",
      `/api/openclaw/agents/${id}/settings/skills/${encodeURIComponent(skillName)}`,
    ),
  createOpenclawAgentCron: (
    id: string,
    payload: {
      name: string;
      scheduleMode: "daily" | "weekly" | "monthly";
      scheduleTime: string;
      scheduleWeekday?: number;
      scheduleDayOfMonth?: number;
      message: string;
      enabled?: boolean;
    },
  ) =>
    request<OpenclawAgentCronSetting>(
      "POST",
      `/api/openclaw/agents/${id}/settings/cron`,
      payload,
    ),
  patchOpenclawAgentCron: (
    id: string,
    cronId: string,
    payload: {
      name?: string;
      scheduleMode?: "daily" | "weekly" | "monthly";
      scheduleTime?: string;
      scheduleWeekday?: number;
      scheduleDayOfMonth?: number;
      message?: string;
      enabled?: boolean;
    },
  ) =>
    request<OpenclawAgentCronSetting>(
      "PATCH",
      `/api/openclaw/agents/${id}/settings/cron/${encodeURIComponent(cronId)}`,
      payload,
    ),
  deleteOpenclawAgentCron: (id: string, cronId: string) =>
    request<void>(
      "DELETE",
      `/api/openclaw/agents/${id}/settings/cron/${encodeURIComponent(cronId)}`,
    ),
  enableOpenclawAgentCron: (id: string, cronId: string) =>
    request<OpenclawAgentCronSetting>(
      "POST",
      `/api/openclaw/agents/${id}/settings/cron/${encodeURIComponent(cronId)}/enable`,
    ),
  disableOpenclawAgentCron: (id: string, cronId: string) =>
    request<OpenclawAgentCronSetting>(
      "POST",
      `/api/openclaw/agents/${id}/settings/cron/${encodeURIComponent(cronId)}/disable`,
    ),
  createOpenclawAgentHook: (
    id: string,
    payload: { name: string; hookMd: string; handlerTs: string; enabled?: boolean },
  ) =>
    request<OpenclawAgentHookSetting>(
      "POST",
      `/api/openclaw/agents/${id}/settings/hooks`,
      payload,
    ),
  patchOpenclawAgentHook: (
    id: string,
    hookName: string,
    payload: { name?: string; hookMd?: string; handlerTs?: string; enabled?: boolean },
  ) =>
    request<OpenclawAgentHookSetting>(
      "PATCH",
      `/api/openclaw/agents/${id}/settings/hooks/${encodeURIComponent(hookName)}`,
      payload,
    ),
  deleteOpenclawAgentHook: (id: string, hookName: string) =>
    request<void>(
      "DELETE",
      `/api/openclaw/agents/${id}/settings/hooks/${encodeURIComponent(hookName)}`,
    ),
  enableOpenclawAgentHook: (id: string, hookName: string) =>
    request<OpenclawAgentHookSetting>(
      "POST",
      `/api/openclaw/agents/${id}/settings/hooks/${encodeURIComponent(hookName)}/enable`,
    ),
  disableOpenclawAgentHook: (id: string, hookName: string) =>
    request<OpenclawAgentHookSetting>(
      "POST",
      `/api/openclaw/agents/${id}/settings/hooks/${encodeURIComponent(hookName)}/disable`,
    ),
  updateOpenclawAgentCustomSection: (id: string, content: string) =>
    request<{ content: string }>(
      "PUT",
      `/api/openclaw/agents/${id}/settings/agents-custom-section`,
      { content },
    ),
  patchOpenclawAgent: (
    id: string,
    payload: {
      name?: string;
      description?: string;
      teamId?: string | null;
      model?: string;
      identityEmoji?: string;
      identityTheme?: string;
    },
  ) =>
    request<OpenclawAgentDetail>(
      "PATCH",
      `/api/openclaw/agents/${id}`,
      payload,
    ),
  deleteOpenclawAgent: (
    id: string,
    mode: OpenclawAgentRemoveMode = "unregister",
  ) =>
    request<void>(
      "DELETE",
      `/api/openclaw/agents/${id}?mode=${mode}`,
    ),
  chatWithOpenclawAgent: (
    id: string,
    body: {
      messages: { role: "system" | "user" | "assistant"; content: string }[];
      attachments?: ChatAttachmentMeta[];
      stream?: boolean;
      modelOverride?: string;
    },
    init?: RequestInit,
  ) =>
    fetch(`/api/openclaw/agents/${id}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      ...init,
    }),
  uploadOpenclawChatAttachment: (
    id: string,
    file: File,
    init?: RequestInit,
  ) => {
    const query = new URLSearchParams({ filename: file.name });
    return uploadBinary<ChatAttachmentUploadResult>(
      `/api/openclaw/agents/${id}/chat/attachments?${query.toString()}`,
      file,
      init,
    );
  },
  getOpenclawAgentChatHistory: (id: string) =>
    request<{ messages: ChatHistoryMessage[] }>(
      "GET",
      `/api/openclaw/agents/${id}/chat-history`,
    ),
  getOpenclawChatStatus: (id: string) =>
    request<ChatStatus>(
      "GET",
      `/api/openclaw/agents/${id}/chat/status`,
      undefined,
      { cache: "no-store" },
    ),
  resetOpenclawAgentChat: (id: string) =>
    request<void>("POST", `/api/openclaw/agents/${id}/reset`),
  stopOpenclawAgentChat: (id: string) =>
    request<void>("POST", `/api/openclaw/agents/${id}/chat/stop`),

  // ── Hermes agents ────────────────────────────────────────────────
  listHermesAgents: (mode: "fast" | "full" = "full") =>
    request<{ items: HermesAgentSummary[] }>(
      "GET",
      `/api/hermes/agents?mode=${mode}`,
    ),
  getHermesAgent: (id: string) =>
    request<HermesAgentDetail>("GET", `/api/hermes/agents/${id}`),
  getHermesRuntimeStatus: (mode: "fast" | "full" = "full") =>
    request<{ running: boolean; reason: string }>(
      "GET",
      `/api/hermes/agents/runtime/status?mode=${mode}`,
      undefined,
      { cache: "no-store" },
    ),
  openHermesDashboard: (agentId?: string) =>
    request<{ url: string }>(
      "POST",
      `/api/hermes/agents/dashboard/open${
        agentId ? `?agentId=${encodeURIComponent(agentId)}` : ""
      }`,
    ),
  startHermesAgentGateway: (id: string) =>
    request<{ message: string }>("POST", `/api/hermes/agents/${id}/gateway/start`),
  createHermesAgent: (
    payload: {
      id?: string;
      name?: string;
      responsibility?: string;
      teamId?: string;
      modelInheritFrom?: string;
      cloneFrom?: string;
      cloneAll?: boolean;
    },
    init?: RequestInit,
  ) =>
    request<HermesAgentCreateResult>(
      "POST",
      "/api/hermes/agents",
      payload,
      init,
    ),
  cancelHermesAgentCreate: (id: string) =>
    request<void>("POST", `/api/hermes/agents/${id}/cancel-create`),
  getOperationStatus: (opId: string) =>
    request<OperationStatus>(
      "GET",
      `/api/operations/${encodeURIComponent(opId)}`,
      undefined,
      { cache: "no-store" },
    ),
  patchHermesAgent: (
    id: string,
    payload: { name?: string; description?: string; teamId?: string },
  ) => request<HermesAgentDetail>("PATCH", `/api/hermes/agents/${id}`, payload),
  deleteHermesAgent: (id: string) =>
    request<void>("DELETE", `/api/hermes/agents/${id}`),
  listHermesClaimable: () =>
    request<{ items: HermesClaimableAgent[]; total: number }>(
      "GET",
      "/api/hermes/agents/claimable",
    ),
  claimHermesAgent: (payload: { id: string; name?: string; teamId?: string }) =>
    request<HermesAgentDetail>("POST", "/api/hermes/agents/claim", payload),
  // settings
  getHermesSoul: (id: string) =>
    request<{ content: string }>("GET", `/api/hermes/agents/${id}/settings/soul`),
  putHermesSoul: (id: string, content: string) =>
    request<{ content: string }>("PUT", `/api/hermes/agents/${id}/settings/soul`, { content }),
  getHermesModel: (id: string) =>
    request<HermesModelSetting>("GET", `/api/hermes/agents/${id}/settings/model`),
  putHermesModel: (id: string, payload: HermesModelSetting) =>
    request<HermesModelSetting>("PUT", `/api/hermes/agents/${id}/settings/model`, payload),
  importHermesModel: (id: string, payload: { inheritFrom: string }) =>
    request<HermesModelSetting>("POST", `/api/hermes/agents/${id}/settings/model/import`, payload),
  getHermesGateway: (id: string) =>
    request<HermesGatewaySetting>("GET", `/api/hermes/agents/${id}/settings/gateway`),
  putHermesGateway: (id: string, payload: HermesGatewaySetting) =>
    request<HermesGatewaySetting>("PUT", `/api/hermes/agents/${id}/settings/gateway`, payload),
  getHermesMcpServers: (id: string) =>
    request<HermesMcpServer[]>("GET", `/api/hermes/agents/${id}/settings/mcp`),
  putHermesMcpServer: (
    id: string,
    // environment: omit (or null) to preserve existing env on edit; "" clears; text replaces.
    payload: {
      name: string;
      transport: "http_sse" | "sse" | "local";
      url?: string;
      command?: string;
      args?: string[];
      environment?: string | null;
    },
  ) => request<HermesMcpServer>("PUT", `/api/hermes/agents/${id}/settings/mcp`, payload),
  deleteHermesMcpServer: (id: string, name: string) =>
    request<void>("DELETE", `/api/hermes/agents/${id}/settings/mcp/${encodeURIComponent(name)}`),
  getHermesSecrets: (id: string) =>
    request<HermesSecretSetting[]>("GET", `/api/hermes/agents/${id}/settings/secrets`),
  putHermesSecret: (id: string, payload: { key: string; value: string }) =>
    request<void>("PUT", `/api/hermes/agents/${id}/settings/secrets`, payload),
  deleteHermesSecret: (id: string, key: string) =>
    request<void>("DELETE", `/api/hermes/agents/${id}/settings/secrets/${encodeURIComponent(key)}`),
  getHermesSkills: (id: string) =>
    request<HermesSkillSetting[]>("GET", `/api/hermes/agents/${id}/settings/skills`),
  getHermesSkill: (id: string, name: string) =>
    request<HermesSkillSetting>("GET", `/api/hermes/agents/${id}/settings/skills/${encodeURIComponent(name)}`),
  createHermesSkill: (id: string, payload: { name: string; description?: string; content: string }) =>
    request<HermesSkillSetting>("POST", `/api/hermes/agents/${id}/settings/skills`, payload),
  updateHermesSkill: (
    id: string,
    name: string,
    payload: { description?: string; content: string },
  ) =>
    request<HermesSkillSetting>(
      "PUT",
      `/api/hermes/agents/${id}/settings/skills/${encodeURIComponent(name)}`,
      payload,
    ),
  deleteHermesSkill: (id: string, name: string) =>
    request<void>("DELETE", `/api/hermes/agents/${id}/settings/skills/${encodeURIComponent(name)}`),
  getHermesCron: (id: string) =>
    request<{ available: boolean; items: HermesCronJob[]; deliveryTargets: HermesCronDeliveryTarget[] }>(
      "GET",
      `/api/hermes/agents/${id}/settings/cron`,
    ),
  createHermesCron: (
    id: string,
    payload: { schedule: string; prompt?: string; name?: string; workdir?: string; deliver?: string },
  ) => request<void>("POST", `/api/hermes/agents/${id}/settings/cron`, payload),
  editHermesCron: (
    id: string,
    jobId: string,
    payload: {
      schedule?: string;
      prompt?: string;
      name?: string;
      deliver?: string;
      workdir?: string;
    },
  ) =>
    request<void>(
      "PUT",
      `/api/hermes/agents/${id}/settings/cron/${encodeURIComponent(jobId)}`,
      payload,
    ),
  hermesCronAction: (id: string, jobId: string, action: "pause" | "resume" | "remove") =>
    request<void>(
      "POST",
      `/api/hermes/agents/${id}/settings/cron/${encodeURIComponent(jobId)}/${action}`,
    ),
  chatWithHermesAgent: (
    id: string,
    body: { message: string; workdir: string; attachments?: ChatAttachmentMeta[] },
    init?: RequestInit,
  ) =>
    fetch(`/api/hermes/agents/${id}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      ...init,
    }),
  uploadHermesChatAttachment: (
    id: string,
    workdir: string,
    file: File,
    init?: RequestInit,
  ) => {
    const query = new URLSearchParams({
      filename: file.name,
      workdir,
    });
    return uploadBinary<ChatAttachmentUploadResult>(
      `/api/hermes/agents/${id}/chat/attachments?${query.toString()}`,
      file,
      init,
    );
  },
  getHermesAgentChatHistory: (id: string) =>
    request<{ messages: ChatHistoryMessage[] }>(
      "GET",
      `/api/hermes/agents/${id}/chat-history`,
    ),
  getHermesChatStatus: (id: string) =>
    request<ChatStatus>(
      "GET",
      `/api/hermes/agents/${id}/chat/status`,
      undefined,
      { cache: "no-store" },
    ),
  resetHermesAgentChat: (id: string) =>
    request<void>("POST", `/api/hermes/agents/${id}/reset`),
  stopHermesAgentChat: (id: string) =>
    request<void>("POST", `/api/hermes/agents/${id}/chat/stop`),

  // Agent Store
  loginAgentStore: (payload: { email: string; password: string }) =>
    request<AgentStoreLoginResponse>("POST", "/api/agent-store/auth/login", payload),
  registerAgentStore: (payload: { email: string; password: string; displayName?: string }) =>
    request<AgentStoreAuthActionResponse>("POST", "/api/agent-store/auth/register", payload),
  verifyAgentStoreEmail: (payload: { email: string; code: string }) =>
    request<AgentStoreAuthActionResponse>("POST", "/api/agent-store/auth/verify-email", payload),
  forgotAgentStorePassword: (payload: { email: string }) =>
    request<AgentStoreAuthActionResponse>("POST", "/api/agent-store/auth/forgot-password", payload),
  resetAgentStorePassword: (payload: { email: string; code: string; newPassword: string }) =>
    request<AgentStoreAuthActionResponse>("POST", "/api/agent-store/auth/reset-password", payload),
  getAgentStoreProfile: (storeToken: string) =>
    request<AgentStoreAccount>("GET", "/api/agent-store/auth/me", undefined, {
      headers: { "X-CSFLOW-Store-Token": storeToken },
    }),
  listAgentStoreCatalog: (listingType?: AgentStoreListingType) => {
    const qs = listingType ? `?type=${encodeURIComponent(listingType)}` : "";
    return request<{ items: AgentStoreCatalogItem[]; total: number }>(
      "GET",
      `/api/agent-store/catalog${qs}`,
    );
  },
  listAgentStoreOwned: (storeToken: string) =>
    request<{ items: AgentStoreOwnedItem[]; total: number }>(
      "GET",
      "/api/agent-store/owned",
      undefined,
      { headers: { "X-CSFLOW-Store-Token": storeToken } },
    ),
  joinAgentStoreListing: (listingId: string, storeToken: string) =>
    request<AgentStoreAcquireResponse>(
      "POST",
      `/api/agent-store/listings/${encodeURIComponent(listingId)}/join`,
      undefined,
      { headers: { "X-CSFLOW-Store-Token": storeToken } },
    ),
  purchaseAgentStoreListing: (listingId: string, storeToken: string) =>
    request<AgentStoreAcquireResponse>(
      "POST",
      `/api/agent-store/listings/${encodeURIComponent(listingId)}/purchase`,
      undefined,
      { headers: { "X-CSFLOW-Store-Token": storeToken } },
    ),
  loadAgentStoreListing: (listingId: string, storeToken: string) =>
    request<AgentStoreLoadResponse>(
      "POST",
      `/api/agent-store/listings/${encodeURIComponent(listingId)}/load`,
      undefined,
      { headers: { "X-CSFLOW-Store-Token": storeToken } },
    ),

  // AI task decompose
  startDecompose: (payload: {
    goal: string;
    leaderAgentId: string;
    leaderKind?: AgentKind;
    leaderRepo?: string | null;
    leaderTargetBranch?: string | null;
    existingAgents?: Record<string, unknown>[];
    existingTasks?: Record<string, unknown>[];
    resultLanguage?: "zh" | "en";
  }) =>
    request<DecomposeStartResponse>(
      "POST",
      "/api/flows/decompose",
      payload,
    ),
  decomposeStatus: (requestId: string) =>
    request<DecomposeStatus>(
      "GET",
      `/api/flows/decompose/${requestId}`,
      undefined,
      { cache: "no-store" },
    ),
  cancelDecompose: (requestId: string) =>
    request<void>("POST", `/api/flows/decompose/${requestId}/cancel`),
  applyDecompose: (requestId: string) =>
    request<{
      agents: Record<string, unknown>[];
      tasks: Record<string, unknown>[];
    }>("POST", `/api/flows/decompose/${requestId}/apply`),

  // Local system helpers
  pickDirectory: (payload?: { title?: string; initialPath?: string | null }) =>
    request<{ path: string | null }>(
      "POST",
      "/api/system/pick-directory",
      payload ?? {},
    ),
  openDirectory: (payload: { path: string }) =>
    request<{ opened: boolean; path: string }>(
      "POST",
      "/api/system/open-directory",
      payload,
    ),
  validateDirectory: (payload: { path: string }) =>
    request<{ path: string }>("POST", "/api/system/validate-directory", payload),
  ensureGitRepo: (payload: {
    path: string;
    createDirIfMissing?: boolean;
    initializeIfMissing?: boolean;
    createInitialCommitIfMissing?: boolean;
  }) =>
    request<EnsureGitRepoResult>(
      "POST",
      "/api/system/ensure-git-repo",
      payload,
    ),
  listRepoBranches: (payload: { path: string; preserveBranch?: string }) =>
    request<RepoBranchesResult>(
      "POST",
      "/api/system/git-branches",
      payload,
    ),
  listWorkspaceDirectories: (allUsers = false) =>
    request<WorkspaceDirectoryList>(
      "GET",
      `/api/system/workspace-directories${allUsers ? "?allUsers=true" : ""}`,
    ),
  getOwnerKindsFast: () =>
    request<OwnerKindsFast>("GET", "/api/system/owner-kinds/fast"),
  getUiCapabilities: () =>
    request<UiCapabilities>("GET", "/api/system/ui-capabilities"),
  getUiLanguage: () =>
    request<{ language: "zh" | "en" | null }>("GET", "/api/system/ui-language"),
  setUiLanguage: (language: "zh" | "en") =>
    request<{ language: "zh" | "en" | null }>("PUT", "/api/system/ui-language", {
      language,
    }),

  // Profiles
  listProfiles: () =>
    request<{ items: ProfileSummary[] }>("GET", "/api/profiles"),
  getProfile: (name: string) =>
    request<{ name: string; raw: Record<string, unknown> }>(
      "GET",
      `/api/profiles/${name}`,
    ),
  testProfile: (name: string, prompt?: string, cwd?: string) =>
    request<{ success: boolean; output: string; name: string }>(
      "POST",
      `/api/profiles/${name}/test`,
      { prompt, cwd },
    ),
  setProfile: (name: string, payload: ProfileSetPayload) =>
    request<{ name: string; raw: Record<string, unknown> }>(
      "POST",
      `/api/profiles/${name}`,
      payload,
    ),
  removeProfile: (name: string) =>
    request<void>("DELETE", `/api/profiles/${name}`),

  // System / self-upgrade
  getUpdateStatus: (force = false) =>
    request<UpdateStatus>(
      "GET",
      `/api/system/update-status${force ? "?force=true" : ""}`,
    ),
  triggerUpgrade: (confirmActiveRuns = false) =>
    request<TriggerUpgradeResult>("POST", "/api/system/upgrade", {
      confirmActiveRuns,
    }),
  getActiveRuns: () =>
    request<ActiveRunsResult>("GET", "/api/system/active-runs"),

  // Per-Flow webhook notifications (opt-in; empty channel list = disabled).
  // Each channel's `format` null = auto-detect the chat platform by URL host.
  getFlowNotifyWebhooks: (flowId: string) =>
    request<FlowWebhookConfig>("GET", `/api/flows/${flowId}/notify-webhooks`),
  setFlowNotifyWebhooks: (flowId: string, channels: FlowWebhookChannel[]) =>
    request<FlowWebhookConfig>("PUT", `/api/flows/${flowId}/notify-webhooks`, {
      channels: channels.map((c) => ({ url: c.url, format: c.format })),
    }),
  // Test a single ad-hoc channel (url provided) or every saved channel.
  testFlowNotifyWebhooks: (
    flowId: string,
    channel?: { url: string; format: string | null },
  ) =>
    request<{ success: boolean; message: string }>(
      "POST",
      `/api/flows/${flowId}/notify-webhooks/test`,
      channel ? { url: channel.url, format: channel.format } : {},
    ),
};
