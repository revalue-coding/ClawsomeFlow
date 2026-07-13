"""ClawsomeFlow MCP server package.

Exposes ClawsomeFlow's Flow-orchestration as MCP tools so an external agent
(configured with this server) can discover Flows, trigger runs, and read back a
run's status and the leader's work report. Runs as a stdio subprocess launched
via ``csflow mcp serve``; it is a thin client of the local backend HTTP API
(same loopback + api_token contract as the ``csflow`` ops CLIs), so it never
touches the DB or scheduler directly.
"""

from __future__ import annotations
