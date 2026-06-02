---
name: self-skills-heartbeats-maintenance
description: Add or adjust custom skills and OpenClaw Cron Jobs for the current agent only (runtime-effective).
---

# Custom Skills and Cron Jobs Maintenance

## When to Use

- The user asks to add/modify/remove skills for the current agent.
- The user asks to add/modify/remove cron jobs.

## Core Principles

1. Operate only in the current agent workspace.
2. New skills must define clear triggers, inputs/outputs, and boundaries.
3. `SKILL.md` must include valid YAML front matter (`name` / `description`).
4. If skills or cron jobs change, also update the relevant notes in `TOOLS.md` or the user section of `AGENTS.md`.
5. `HEARTBEAT.md` is only a heartbeat checklist, not a cron configuration entry point. Do not place cron expressions, runtime job IDs, or schedule times there.

## Flow A: Add or Modify a Skill
1. Add or edit files under `skills/<skill-id>/` (must include `SKILL.md` at minimum).
2. If the skill needs dedicated scripts, place them in the same skill directory (e.g. `skills/<skill-id>/scripts/`).
3. Self-check: ensure the target skill is readable and structurally complete.

## Flow B: Remove a Skill
1. Delete the target skill directory (`skills/<skill-id>/`).
2. Clean related references in `TOOLS.md` / user section of `AGENTS.md`.
3. Self-check: directory is removed and no stale references remain.

## Flow C: Add or Modify Runtime Cron Jobs (Visible in Runtime)
1. First verify runtime cron is available:
   ```bash
   openclaw gateway status
   openclaw cron status
   ```
2. Add a recurring job (recommend `isolated`):
   ```bash
   openclaw cron add \
     --name "<job_name>" \
     --cron "<cron_expr>" \
     --tz "<IANA_timezone>" \
     --session isolated \
     --message "<task_prompt>"
   ```
3. Add a one-time reminder (main session):
   ```bash
   openclaw cron add \
     --name "<job_name>" \
     --at "<ISO8601_or_relative>" \
     --session main \
     --system-event "<event_text>" \
     --wake now
   ```
4. In multi-agent scenarios, append `--agent "<current_agent_id>"` as needed to avoid sending jobs to the wrong agent.
5. Validation is mandatory after creation:
   ```bash
   openclaw cron list
   openclaw cron show <job_id>
   ```
6. Modify an existing job:
   ```bash
   openclaw cron edit <job_id> --cron "<cron_expr>" --tz "<IANA_timezone>" --message "<task_prompt>"
   ```
7. Optional: trigger one immediate validation run:
   ```bash
   openclaw cron run <job_id> --wait --wait-timeout 10m
   ```

## Flow D: Remove Runtime Cron Jobs

1. Remove the cron job from runtime:
   ```bash
   openclaw cron remove <job_id>
   ```
2. Verify again:
   ```bash
   openclaw cron list
   ```

## Output Requirements

- State which flow was executed (A/B/C/D).
- List modified paths, skill IDs, or cron job IDs.
- Describe expected behavior after changes and verification method (include at least one runtime visibility verification command).
