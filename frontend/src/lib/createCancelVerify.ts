import type { OperationStatus } from "./api";

/** Consecutive absent polls required before treating cancel as converged. */
export const CREATE_CANCEL_VERIFY_REQUIRED_CONSECUTIVE_ABSENT = 2;

/**
 * True once it is *safe* to cancel an in-flight OpenClaw create.
 *
 * The cancel endpoint purges the agent's row + workspace and then awaits the
 * tracked bootstrap task. That is only race-free once ``commit_agent`` has
 * finished ALL filesystem scaffolding AND the bootstrap task is tracked — which
 * is exactly the instant the op is registered (``reg.start`` runs immediately
 * after ``commit_agent`` returns, with the task tracked synchronously right
 * after, no intervening await). Before that the create reports
 * ``source="in_flight"`` (``_CREATE_IN_PROGRESS`` set, op not yet registered);
 * cancelling then would let the still-running scaffolding re-create artifacts the
 * cleanup just removed → residual data → broken re-create.
 *
 * Hence the discriminator: ``running`` from the **registry** source (not the
 * in-flight fallback).
 */
export function isOpenclawCancelArmed(op: OperationStatus): boolean {
  return op.state === "running" && op.source === "registry";
}

/**
 * True once it is *safe* to cancel an in-flight Hermes create.
 *
 * Hermes registers the op (``reg.start``) in the request handler *before* the
 * executor commit even starts, so ``source="registry"`` is set almost
 * immediately and is NOT a safety signal here. The real boundary is
 * ``_CREATES_IN_FLIGHT``: ``_commit_agent_locked`` first *discards* any stale
 * cancel flag ("fresh attempt") and only then publishes the id as in-flight. A
 * cancel that lands before that discard is silently wiped and the agent gets
 * fully created anyway. Once the id is in-flight (``op.inFlight``) the create has
 * passed the discard, so a cancel flag sticks and rollback/kill always applies.
 */
export function isHermesCancelArmed(op: OperationStatus): boolean {
  return op.state !== "succeeded" && op.state !== "failed" && op.inFlight === true;
}

const ARM_POLL_MS = 800;
/** How long a persistent ``not_found`` is tolerated before giving up arming. */
const ARM_NOT_FOUND_GRACE_MS = 45_000;
/** Absolute safety cap so a wedged create never leaves the poll looping forever. */
const ARM_MAX_MS = 15 * 60_000;

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

/**
 * Poll an operation's status until it is *safe to cancel* per ``isArmed``.
 *
 * Resolves ``true`` the moment the arm predicate holds; resolves ``false`` if
 * the op terminates first, the caller asks to stop (``shouldStop``), the op
 * stays ``not_found`` past the grace window, or the absolute cap is hit. The
 * caller flips its "armed" UI state only on ``true`` — so the cancel button
 * stays disabled until the backend has reached a state where cancel cannot
 * leave residual data.
 */
export async function waitForCancelArmed(
  opId: string,
  isArmed: (op: OperationStatus) => boolean,
  opts: {
    getStatus: (opId: string) => Promise<OperationStatus>;
    shouldStop: () => boolean;
    pollMs?: number;
    notFoundGraceMs?: number;
    maxMs?: number;
  },
): Promise<boolean> {
  const pollMs = opts.pollMs ?? ARM_POLL_MS;
  const grace = opts.notFoundGraceMs ?? ARM_NOT_FOUND_GRACE_MS;
  const max = opts.maxMs ?? ARM_MAX_MS;
  const start = Date.now();
  let notFoundDeadline = start + grace;
  while (Date.now() - start < max) {
    if (opts.shouldStop()) return false;
    let op: OperationStatus | null = null;
    try {
      op = await opts.getStatus(opId);
    } catch {
      /* transient (offline / server blip) — keep waiting */
    }
    if (op) {
      if (op.state === "succeeded" || op.state === "failed") return false;
      if (isArmed(op)) return true;
      if (op.state === "not_found") {
        if (Date.now() > notFoundDeadline) return false;
      } else {
        // Saw a real (running) state — the op exists, keep extending tolerance
        // while it finishes its pre-arm scaffolding.
        notFoundDeadline = Date.now() + grace;
      }
    }
    if (opts.shouldStop()) return false;
    await sleep(pollMs);
  }
  return false;
}

/**
 * True when a create-cancel verify poll indicates the server has fully stopped
 * creating the agent. ``not_found`` alone is NOT enough — the create POST may
 * not have registered the op yet while ``inFlight`` is still true.
 */
export function isCreateCancelConverged(
  absent: boolean,
  op: OperationStatus,
  consecutiveAbsent: number,
): boolean {
  if (!absent || consecutiveAbsent < CREATE_CANCEL_VERIFY_REQUIRED_CONSECUTIVE_ABSENT) {
    return false;
  }
  if (op.inFlight || op.state === "running") {
    return false;
  }
  if (op.state === "failed") {
    return op.detail === "cancelled";
  }
  if (op.state === "not_found") {
    return true;
  }
  return false;
}
