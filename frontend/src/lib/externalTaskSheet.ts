/** Rebuild / pick the Run-detail external task sheet for the UI language.

 * New dispatches persist ``messageZh`` / ``messageEn`` on the event. Older
 * events only have a single ``message`` (language at dispatch time) plus
 * structured package fields — we rebuild a best-effort sheet from those.
 */

export type ExternalTaskSheetFields = {
  message?: string;
  messageZh?: string;
  messageEn?: string;
  channel?: string;
  subject?: string;
  description?: string;
  outputRequirement?: string;
  flowDescription?: string;
  runtimeInputs?: Record<string, unknown> | null;
  upstreamOutputs?: Array<{
    taskId?: string;
    subject?: string;
    fromAgent?: string;
    summary?: string;
  }> | null;
  taskId?: string;
  runId?: string;
  teamName?: string;
};

const WEBHOOK_NOTES_EN =
  "This is a remote task. Absolute paths mentioned in upstream outputs "
  + "may not exist on your machine — do not open or fetch them locally. "
  + "In your callback summary, do not include local file paths; describe "
  + "necessary results in plain text (links or references are fine).";

const WEBHOOK_NOTES_ZH =
  "这是远程任务。上游产出中出现的绝对路径在你本机上可能不存在——"
  + "请勿在本地打开或拉取。回传摘要时不要写入本机文件路径；"
  + "用纯文本描述必要结果（链接或引用即可）。";

function rebuildExternalTaskSheet(
  fields: ExternalTaskSheetFields,
  lang: "zh" | "en",
): string {
  const zh = lang === "zh";
  const runId = (fields.runId || "").trim();
  const team = (fields.teamName || "").trim();
  const subject = (fields.subject || "").trim();
  const taskId = (fields.taskId || "").trim();
  const flowGoal = (fields.flowDescription || "").trim()
    || (zh ? "_(Flow 无描述)_" : "_(Flow has no description)_");
  const taskDesc = (() => {
    const body = (fields.description || "").trim();
    const req = (fields.outputRequirement || "").trim();
    if (body && req) return `${body}\n\n${req}`;
    return body || req || (zh ? "_(无额外说明)_" : "_(no additional description)_");
  })();
  const inputs = fields.runtimeInputs && typeof fields.runtimeInputs === "object"
    ? Object.entries(fields.runtimeInputs)
    : [];
  const inputsBody = inputs.length
    ? inputs.map(([k, v]) => `  - **${k}**: \`${String(v)}\``).join("\n")
    : (zh ? "  _(无)_" : "  _(none)_");

  const upstream = Array.isArray(fields.upstreamOutputs)
    ? fields.upstreamOutputs
    : [];
  let upstreamBlock = "";
  if (upstream.length > 0) {
    const header = zh
      ? `## 直接上游产出（${upstream.length} 项，仅一级依赖）\n`
        + "上游可能是本地 Agent 或其他外部节点。"
        + "请以下方完成摘要为上下文；本节点没有共享 worktree 路径。"
      : `## Direct Upstream Outputs (${upstream.length} item(s), first-level dependencies only)\n`
        + "Upstream executors may be local agents or other external nodes. "
        + "Use the completion summary below as context; there is no shared "
        + "worktree path for this node.";
    const lines = upstream.map((u) => {
      const tid = String(u.taskId || "");
      const subj = String(u.subject || "");
      const agent = String(u.fromAgent || "");
      const sum = String(u.summary || "").trim();
      const label = zh ? "完成摘要" : "completion summary";
      const by = zh ? "执行者" : "by agent";
      const miss = zh
        ? "_(暂无 — 如需前序交付物请询问 Run 操作者)_"
        : "_(not available yet — ask the run operator if you need prior deliverables)_";
      return (
        `- task \`${tid}\` "${subj}" ${by} \`${agent}\`\n`
        + `  - ${label}: ${sum || miss}`
      );
    });
    upstreamBlock = `${header}\n${lines.join("\n")}`;
  }

  const intro = zh
    ? "## ClawsomeFlow 外部任务\n"
      + "请完成下方任务后，通过 Run 详情页的任务卡片或回调 API 回传结果。\n"
      + `Run：\`${runId || "?"}\`${team ? `  ·  团队：\`${team}\`` : ""}`
    : "## ClawsomeFlow External Task\n"
      + "Complete the work below, then submit the result via the Run "
      + "detail card or the callback API.\n"
      + `Run ID: \`${runId || "?"}\`${team ? `  ·  Team: \`${team}\`` : ""}`;

  const submit = zh
    ? "## 结果提交\n"
      + "- 提交简要完成摘要（含交付物链接/引用）。\n"
      + "- 若无法完成，请提交失败原因，不要一直挂起。"
    : "## Result Submission\n"
      + "- Provide a concise completion summary (links/refs when useful).\n"
      + "- If blocked, submit a failure with the reason — do not leave it open.";

  const blocks = [
    intro,
    `${zh ? "## Flow 目标" : "## Flow Goal"}\n${flowGoal}\n`
      + `${zh ? "## 运行参数" : "## Runtime inputs"}\n${inputsBody}`,
    upstreamBlock,
    `${zh ? `## 任务 #${taskId}：${subject}` : `## Task #${taskId}: ${subject}`}\n${taskDesc}`,
    submit,
  ];
  if (fields.channel === "webhook") {
    blocks.push(
      `${zh ? "## 远程执行备注" : "## Notes for remote executors"}\n`
        + (zh ? WEBHOOK_NOTES_ZH : WEBHOOK_NOTES_EN),
    );
  }
  return blocks.filter(Boolean).join("\n\n").trim() + "\n";
}

/** Prefer persisted bilingual sheets; else rebuild from structured fields. */
export function pickExternalTaskSheet(
  fields: ExternalTaskSheetFields,
  lang: "zh" | "en",
): string {
  const zh = (fields.messageZh || "").trim();
  const en = (fields.messageEn || "").trim();
  if (lang === "zh" && zh) return zh;
  if (lang === "en" && en) return en;
  // Opposite language available but not the requested one — rebuild so the
  // card follows the current pill instead of sticking to dispatch-time lang.
  if (zh || en || fields.description || fields.subject || fields.flowDescription) {
    return rebuildExternalTaskSheet(fields, lang);
  }
  return (fields.message || "").trim();
}
