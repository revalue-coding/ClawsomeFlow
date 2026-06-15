# common-agent-source

Shared bootstrap assets for backend-created OpenClaw agents.

## Contents

- `agent-common-rules.md`
  - Source text placed at the top of workspace `AGENTS.md`
  - `AGENTS_USER_CUSTOM_SECTION` markers are injected dynamically at creation/deploy
- `skills/`
  - Default common skills copied into workspace `skills/`
  - Includes built-in self-maintenance skills:
    - `self-skills-heartbeats-maintenance`
    - `self-definition-maintenance`
- `cron-jobs/`
  - Source-of-truth definitions for system built-in OpenClaw cron jobs
  - Synced to managed agents during creation and upgrade flows
