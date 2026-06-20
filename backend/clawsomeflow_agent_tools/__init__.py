"""Packaged global ClawsomeFlow agent tool scripts.

This package is resource-only; its directory contents are populated by Hatch
`force-include` rules at build time from the repo-level
`clawsomeflow-agent-tools/` tree. These tools are global (used by every agent
kind, not just OpenClaw) and are deployed unconditionally into
`~/.clawsomeflow/.clawsomeflow-agent-tools/` at init/upgrade.
"""
