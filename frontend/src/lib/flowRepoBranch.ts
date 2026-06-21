import { api, ApiError } from "@/lib/api";

export type RepoEnsureResult = {
  path: string;
  branches: string[];
  editable: boolean;
};

/** After repo validation: keep a filled branch only when it still exists. */
export function branchAfterRepoCheck(current: string, branches: string[]): string {
  const trimmed = current.trim();
  if (!trimmed) return "";
  return branches.includes(trimmed) ? trimmed : "";
}

type RepoIssueReason =
  | "path_not_found"
  | "not_git_repo"
  | "no_initial_commit"
  | "unknown";

function repoIssueReason(
  checked: Awaited<ReturnType<typeof api.ensureGitRepo>>,
): RepoIssueReason {
  if (!checked.pathExists) return "path_not_found";
  if (!checked.isGitRepo) return "not_git_repo";
  if (!checked.hasInitialCommit) return "no_initial_commit";
  return "unknown";
}

export async function ensureRepoAndListBranches(params: {
  repo: string;
  /** Branch already selected in the form; kept in the list when it still exists. */
  preserveBranch?: string;
  agentLabel: string;
  confirmCreate: (message: string) => Promise<boolean>;
  messages: {
    reasonPathMissing: string;
    reasonNotGitRepo: string;
    reasonNoInitialCommit: string;
    reasonUnknown: string;
    confirmCreate: (args: {
      agentId: string;
      repo: string;
      reason: string;
    }) => string;
    reselectHint: string;
    checkFailed: (args: { message: string }) => string;
    fetchFailed: (args: { message: string }) => string;
    stillInvalid: string;
  };
}): Promise<
  | { ok: true; result: RepoEnsureResult }
  | { ok: false; error: string; cancelled?: boolean }
> {
  const rawRepo = params.repo.trim();
  if (!rawRepo) {
    return { ok: true, result: { path: "", branches: [], editable: false } };
  }

  const errText = (e: unknown) =>
    e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);

  let checked;
  try {
    checked = await api.ensureGitRepo({
      path: rawRepo,
      createDirIfMissing: false,
      initializeIfMissing: false,
    });
  } catch (e) {
    return {
      ok: false,
      error: params.messages.checkFailed({ message: errText(e) }),
    };
  }

  let resolvedPath = checked.path || rawRepo;
  if (checked.pathExists && checked.isGitRepo && checked.hasInitialCommit) {
    try {
      const meta = await api.listRepoBranches({
        path: resolvedPath,
        preserveBranch: params.preserveBranch?.trim() || undefined,
      });
      return {
        ok: true,
        result: {
          path: meta.path || resolvedPath,
          branches: meta.branches,
          editable: meta.editable,
        },
      };
    } catch (e) {
      return {
        ok: false,
        error: params.messages.fetchFailed({ message: errText(e) }),
      };
    }
  }

  const reasonKey = repoIssueReason(checked);
  const reason =
    reasonKey === "path_not_found"
      ? params.messages.reasonPathMissing
      : reasonKey === "not_git_repo"
      ? params.messages.reasonNotGitRepo
      : reasonKey === "no_initial_commit"
      ? params.messages.reasonNoInitialCommit
      : params.messages.reasonUnknown;

  const shouldCreate = await params.confirmCreate(
    params.messages.confirmCreate({
      agentId: params.agentLabel,
      repo: resolvedPath,
      reason,
    }),
  );
  if (!shouldCreate) {
    return { ok: false, error: params.messages.reselectHint, cancelled: true };
  }

  try {
    const ensured = await api.ensureGitRepo({
      path: resolvedPath,
      createDirIfMissing: true,
      initializeIfMissing: true,
      createInitialCommitIfMissing: true,
    });
    if (!ensured.isGitRepo || !ensured.hasInitialCommit) {
      return { ok: false, error: params.messages.stillInvalid };
    }
    resolvedPath = ensured.path || resolvedPath;
    const meta = await api.listRepoBranches({
      path: resolvedPath,
      preserveBranch: params.preserveBranch?.trim() || undefined,
    });
    return {
      ok: true,
      result: {
        path: meta.path || resolvedPath,
        branches: meta.branches,
        editable: meta.editable,
      },
    };
  } catch (e) {
    return {
      ok: false,
      error: params.messages.checkFailed({ message: errText(e) }),
    };
  }
}
