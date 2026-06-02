---
name: ClawsomeFlow Task Decomposer
description: >
  This skill MUST be applied whenever the agent receives a message containing
  the prefix "## ClawsomeFlow Task Decomposition Request". The agent decomposes the user's
  Flow goal into a structured DAG of tasks and posts the result back to
  ClawsomeFlow's loopback API so the front-end can render it for review.
version: 1.1.0
---

# ClawsomeFlow Task Decomposer Protocol

## When this triggers

A message starting with `## ClawsomeFlow Task Decomposition Request` is sent to you by
the ClawsomeFlow backend. It carries:

- **request_id** — identifies the decomposition request (echo back verbatim)
- **goal** — the user's Flow goal in natural language
- **leader_agent_id** — your own agent id (you are the team leader for this Flow)
- **available_agents** — every OpenClaw agent the user owns. **Prefer
  these as task owners**; do not invent new OpenClaw agents.
- **existing_agents** — agents the user has already drafted in the editor
  (already a subset of available_agents in most cases)
- **existing_tasks** — any tasks already drafted (treat as hints; you may
  refine / reorder / replace)
- **ClawsomeFlow API base** + **short-lived callback token** — loopback
  URL + 5-minute bearer token for POSTing the result back
- **required output language** — language to use for task `subject` and
  `description` (Chinese or English depending on UI language)

## What you must produce

Return a JSON document with two arrays — `agents` and `tasks` — that
form a complete, valid Flow:

- Each `task` has: `id` (English short identifier `[A-Za-z0-9_-]+`),
  `subject` (≤80 chars), `description` (1–3 sentences), `ownerAgentId`
  (see Owner assignment policy below), `dependsOn` (array of task ids),
  `isLeaderSummary` (boolean, exactly one task true), optional
  `timeoutSeconds` (default 1800).
- Each `agent` has: `id`, `kind` (`openclaw` for OpenClaw agents,
  `claude` for TUI), optional `repo` (for `claude` kind only — a
  placeholder path the user will fill in), `isLeader` (true exactly for
  the agent that owns the leader-summary task).

### Owner assignment policy

1. **Prefer reusing `available_agents`** for worker tasks — they are
   the agents the user already owns and you should not invent new
   OpenClaw agents the user does not have.
2. If no available OpenClaw agent is a clean fit for a given worker
   task, set `ownerAgentId` to an **empty string** (`""`). The front-end
   will surface that row to the user so they can pick an owner before
   the Flow is saved.
3. You may still propose a NEW `claude` agent (TUI) with a placeholder
   `repo` path if the work is clearly something the user would do via
   Claude Code on their own machine.
4. The `isLeaderSummary` task MUST be owned by `leader_agent_id`
   (i.e. you). Never leave it blank.

### Mandatory invariants (server will reject violations)

1. **Exactly one** task must have `isLeaderSummary: true`, owned by you.
2. The leader CANNOT own any other task — only the summary.
3. Task ids unique. Agent ids unique. `dependsOn` references must resolve.
   DAG must be acyclic.
4. Owner kinds restricted to `openclaw` or `claude`.
5. Each `agent` entry must be referenced by at least one task — i.e.
   don't include an agent in `agents` that no task is owned by.

## How to call back

The **api_base** and **token** are given to you inline in the request
message as literal values (not as bash environment variables). Substitute
them yourself into the URL and `Authorization` header. The endpoint is
loopback-only, so you cannot reach it from outside the server.

Use the **bash** tool to write the JSON body to a temp file and curl
it, so quotes inside `description` don't break:

```bash
cat > /tmp/csflow-decompose-result.json <<'EOF'
{
  "request_id": "<request_id from the request message>",
  "agents": [
    {"id": "writer",  "kind": "openclaw", "isLeader": false},
    {"id": "<your_agent_id>", "kind": "openclaw", "isLeader": true}
  ],
  "tasks": [
    {"id": "draft", "ownerAgentId": "writer",
     "subject": "Draft the report",
     "description": "Compose section 1–3 from sources.",
     "dependsOn": [], "isLeaderSummary": false, "timeoutSeconds": 1800},
    {"id": "review", "ownerAgentId": "",
     "subject": "Manual editorial review",
     "description": "Editor reviews the draft for tone and accuracy.",
     "dependsOn": ["draft"], "isLeaderSummary": false, "timeoutSeconds": 1800},
    {"id": "summary", "ownerAgentId": "<your_agent_id>",
     "subject": "Final review + deliverable wrap-up",
     "description": "Review draft + worker outputs; produce the final deliverable with conclusions, risks, and verification evidence (no merge suggestions).",
     "dependsOn": ["draft", "review"], "isLeaderSummary": true, "timeoutSeconds": 1800}
  ]
}
EOF

# Substitute the literal api_base and token values from the request message:
curl -fsSL -X POST "<api_base from request>/api/internal/task-decompose/commit" \
  -H "Authorization: Bearer <token from request>" \
  -H "Content-Type: application/json" \
  --data @/tmp/csflow-decompose-result.json
```

The endpoint returns `{"request_id": "..."}` on success. Reply to the
user with a one-sentence confirmation (e.g. *"Drafted 5 tasks for review
in the editor."*).

**Important**: do NOT reply "generated N tasks" until you have verified
the curl exited with status 0. If it failed (network / auth / validator
rejection), report the failure to the user verbatim so they know the
result didn't reach the editor.

## On failure

If you genuinely cannot satisfy the goal (ambiguous / contradictory /
out of scope), POST to the failure endpoint instead:

```bash
curl -fsSL -X POST "<api_base from request>/api/internal/task-decompose/fail" \
  -H "Authorization: Bearer <token from request>" \
  -H "Content-Type: application/json" \
  -d '{"request_id": "...", "code": "INSUFFICIENT_INPUT", "message": "<one sentence>"}'
```

## Hard rules

1. Never call any URL other than the api_base provided in the request.
2. Never modify openclaw.json or other agents' workspaces.
3. Never start an actual Flow Run — your job ends at posting JSON back.
4. Keep `description` actionable; the worker will literally see it as the
   task brief at dispatch time.
