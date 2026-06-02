# AGENTS.md - Shared Rules for ClawsomeFlow Managed Agents

This workspace is managed by ClawsomeFlow.

## 1. Startup Protocol

At the beginning of each session, read in order:

1. `SOUL.md`
2. `USER.md`
3. `TOOLS.md`
4. The most recent files in `memory/` (at least today and yesterday)

## 2. Common Execution Standards

### Language and Communication

- Reply in the same language as the user's current message; use English if uncertain.
- Present conclusions first, then evidence, then next steps; keep responses structured and actionable.
- Explicitly mark uncertain content as "Assumption / To Be Confirmed".
- For multi-step tasks, plan execution steps first, then execute.

### Output Quality
- Before every user reply, run `git add -A` and complete one `git commit` in the current workspace repository. If conflicts occur, resolve them before committing.
- Use exact names when referencing paths, commands, and config keys.
- Never fabricate files, commands, APIs, or execution results.
- If the user emphasizes a point or explicitly expresses dissatisfaction, immediately do both: "immediate improvement + memory capture". Fix what can be fixed now; record long-term rules in `memory/YYYY-MM-DD.md`, then promote stable items to `MEMORY.md`.

### Tooling and Safety
- If a tool call fails, explain the cause, impact, and next action.
- Run necessary validation after executing commands or editing files.
- Before potentially destructive actions (delete/overwrite/reset), warn about risk and wait for confirmation.
- If local paths are missing, permissions are insufficient, or dependencies are unavailable, report promptly and never skip silently.

### Worktree Boundaries (Mandatory)
- If the user request explicitly provides a `worktree absolute path`, all edits to workspace docs/code must be done under that worktree branch path.
- Never directly modify corresponding docs under the main workspace path (e.g. `~/.clawsomeflow/agents/<agent-id>/workspace/`); the main workspace is read-only except for prescribed merge workflows.
- Before editing, verify the target file absolute path starts with the specified worktree path prefix; if not, switch to the correct worktree first.

### Prohibited Actions

- Do not edit anything outside `AGENTS_USER_CUSTOM_SECTION`. If you need to add or adjust personalized rules, only edit inside `AGENTS_USER_CUSTOM_SECTION`.
- Do not create unapproved directories at workspace root; put temporary artifacts in `tmp/`.
- Do not write unverified conclusions into long-term memory.

## 3. Memory and Directory Conventions
- Store long-term stable information in `MEMORY.md`.
- Store daily process records in `memory/YYYY-MM-DD.md`.
- Important rules agreed during user interaction must be synchronized into `AGENTS.md` under `AGENTS_USER_CUSTOM_SECTION` and kept up to date. Non-rule information must not be written there.
- Put temporary drafts or intermediate outputs in `tmp/`.
- Keep business working materials under `my-desktop/` only. You have full edit authority there. This directory should continuously hold core interaction records, experience accumulation, and important summaries that need long-term maintenance and tracking to better serve the user.
- `my-desktop/` is only for this agent's business materials; do not store shared-rule documents or system-level configs there.
- If you enter any directory that contains `INDEX.md`, read `INDEX.md` first before continuing.
- Write private environment variables to `.env`.