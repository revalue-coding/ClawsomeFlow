# openclaw-agent-source

This directory is the source of truth for OpenClaw agent bootstrap materials
managed by ClawsomeFlow.

It is deployed by explicit mapping rules (not mirrored as a whole tree):

- `csflow init`
- `csflow upgrade`
- auto-upgrade path in `csflow start`

## Structure

- `clawsomeflow-agent-tools/`
  - Shared scripts that OpenClaw agents may call
  - Scripts are grouped by module in `scripts/<module>/`
  - Synced to `~/.clawsomeflow/.clawsomeflow-agent-tools/`
- `common-agent-source/`
  - `agent-common-rules.md`, `skills/`, and `cron-jobs/` for ClawsomeFlow-managed agents
  - Includes built-in self-maintenance skills for definition + skills/heartbeat tuning
  - Contains migrated common skills from legacy `skills-source/`
  - Synced to `~/.clawsomeflow/.common-agent-source/`
  - At agent creation:
    - `agent-common-rules.md` -> target workspace `AGENTS.md`
    - `skills/` -> target workspace `skills/`
