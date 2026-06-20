# clawsomeflow-agent-tools

Shared scripts for OpenClaw agents managed by ClawsomeFlow.

Deployment target:

`~/.clawsomeflow/.clawsomeflow-agent-tools/`

Suggested module layout:

- `scripts/heartbeat/` - heartbeat checklist item add/remove helpers for managed workspaces (not cron scheduling)

Current built-in scripts:

- `scripts/heartbeat/add-heartbeat-task.sh`
- `scripts/heartbeat/remove-heartbeat-task.sh`
