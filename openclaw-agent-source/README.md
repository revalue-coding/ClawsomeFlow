# openclaw-agent-source

This directory is the source of truth for OpenClaw agent bootstrap materials
managed by ClawsomeFlow.

It is deployed by explicit mapping rules (not mirrored as a whole tree):

- `csflow init`
- `csflow upgrade`
- auto-upgrade path in `csflow start`

> Note: the global agent tool scripts moved OUT of this directory to the
> top-level `clawsomeflow-agent-tools/` (they are not OpenClaw-specific and are
> deployed unconditionally). See that directory's README.

## Structure

- `common-agent-source/`
  - `agent-common-rules.md`, `skills/`, and `cron-jobs/` for ClawsomeFlow-managed agents
  - Includes built-in self-maintenance skills for definition + skills/heartbeat tuning
  - Contains migrated common skills from legacy `skills-source/`
  - Synced to `~/.clawsomeflow/.common-agent-source/`
  - At agent creation:
    - `agent-common-rules.md` -> target workspace `AGENTS.md`
    - `skills/` -> target workspace `skills/`
