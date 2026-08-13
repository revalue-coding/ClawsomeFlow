import type { FlowSpec } from "@/lib/api";

/** Default target branch when no repo is selected yet (non-OpenClaw agents). */
export const DEFAULT_TARGET_BRANCH = "main";

export const FLOW_RUNTIME_REQUIREMENT_KEY = "csflow.runtime.requirement";
export const FLOW_RUNTIME_PARAM_FIELDS_KEY = "csflow.runtime.param_fields";
// "省心模式" (easy mode). Stored in the spec's ``variables`` dict (not a typed
// FlowSpec field) so it survives round-trips through an un-upgraded backend, like
// the run-input-field keys above. When ON, a manual run is created as a scheduled
// run (self-merge in-task, skip user review + complaint). See backend
// app/api/runs.py::trigger_run.
export const FLOW_EASY_MODE_KEY = "csflow.easy_mode";
// "开发者模式" (developer mode). Stored in the spec's ``variables`` dict like easy
// mode. When ON, each subtask decides whether the agent self-merges (see
// FlowTask.devAutoMerge); the user merge-review phase is skipped (a manual run
// still enters the complaint phase). Mutually exclusive with easy mode in the
// UI. See backend app/flow_modes.py.
export const FLOW_DEV_MODE_KEY = "csflow.dev_mode";

function normalizeFields(fields: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of fields) {
    const cleaned = (raw || "").trim();
    if (!cleaned || seen.has(cleaned)) continue;
    seen.add(cleaned);
    out.push(cleaned);
  }
  return out;
}

export function getRunInputRequirement(spec: FlowSpec | null | undefined): string {
  const raw = spec?.variables?.[FLOW_RUNTIME_REQUIREMENT_KEY];
  return typeof raw === "string" ? raw : "";
}

export function setRunInputRequirement(spec: FlowSpec, requirement: string): FlowSpec {
  const cleaned = requirement.trim();
  const nextVariables: Record<string, string> = { ...(spec.variables ?? {}) };
  if (cleaned) nextVariables[FLOW_RUNTIME_REQUIREMENT_KEY] = cleaned;
  else delete nextVariables[FLOW_RUNTIME_REQUIREMENT_KEY];
  return {
    ...spec,
    variables: Object.keys(nextVariables).length > 0 ? nextVariables : {},
  };
}

export function getEasyMode(spec: FlowSpec | null | undefined): boolean {
  return String(spec?.variables?.[FLOW_EASY_MODE_KEY] ?? "").trim().toLowerCase() === "true";
}

export function setEasyMode(spec: FlowSpec, on: boolean): FlowSpec {
  const nextVariables: Record<string, string> = { ...(spec.variables ?? {}) };
  if (on) nextVariables[FLOW_EASY_MODE_KEY] = "true";
  else delete nextVariables[FLOW_EASY_MODE_KEY];
  return {
    ...spec,
    variables: Object.keys(nextVariables).length > 0 ? nextVariables : {},
  };
}

export function getDevMode(spec: FlowSpec | null | undefined): boolean {
  return String(spec?.variables?.[FLOW_DEV_MODE_KEY] ?? "").trim().toLowerCase() === "true";
}

export function setDevMode(spec: FlowSpec, on: boolean): FlowSpec {
  const nextVariables: Record<string, string> = { ...(spec.variables ?? {}) };
  if (on) nextVariables[FLOW_DEV_MODE_KEY] = "true";
  else delete nextVariables[FLOW_DEV_MODE_KEY];
  return {
    ...spec,
    variables: Object.keys(nextVariables).length > 0 ? nextVariables : {},
  };
}

export function getRunInputFields(spec: FlowSpec | null | undefined): string[] {
  const raw = spec?.variables?.[FLOW_RUNTIME_PARAM_FIELDS_KEY];
  if (typeof raw === "string" && raw.trim()) {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        return normalizeFields(parsed.map((x) => String(x)));
      }
    } catch {
      // Backward-compatible parse for early non-JSON experiments.
      const split = raw.split(/\r?\n|,/g).map((x) => x.trim()).filter(Boolean);
      if (split.length > 0) return normalizeFields(split);
    }
  }
  // Legacy fallback: old text-based requirement becomes one param field.
  const legacy = getRunInputRequirement(spec).trim();
  return legacy ? [legacy] : [];
}

export function setRunInputFields(spec: FlowSpec, fields: string[]): FlowSpec {
  const cleaned = normalizeFields(fields);
  const nextVariables: Record<string, string> = { ...(spec.variables ?? {}) };
  if (cleaned.length > 0) {
    nextVariables[FLOW_RUNTIME_PARAM_FIELDS_KEY] = JSON.stringify(cleaned);
  } else {
    delete nextVariables[FLOW_RUNTIME_PARAM_FIELDS_KEY];
  }
  // New model replaces legacy free-text requirement storage.
  delete nextVariables[FLOW_RUNTIME_REQUIREMENT_KEY];
  return {
    ...spec,
    variables: Object.keys(nextVariables).length > 0 ? nextVariables : {},
  };
}
