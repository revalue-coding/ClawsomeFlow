import type { OperationStatus } from "./api";

/** Consecutive absent polls required before treating cancel as converged. */
export const CREATE_CANCEL_VERIFY_REQUIRED_CONSECUTIVE_ABSENT = 2;

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
