<div align="center">

<p>
  <img src="./docs/assets/title.png" alt="ClawsomeFlow" width="960" />
</p>

<p>
  🌐 <a href="https://clawsomeflow.com"><b>clawsomeflow.com</b></a> ·
  📖 <a href="https://clawsomeflow.com/docs/"><b>Docs</b></a>
</p>

<p>
  <b>English</b> ·
  <a href="./readme.zh.md">简体中文</a>
</p>

<p><b>A harness for long-running, multi-party work — humans, machines, and any agent platform in one controllable flow.</b></p>

<p>Production work is never “AI-only”. Someone has to touch the physical world, approve an irreversible action, or make a judgment call. Teams also span machines and tools — not a single laptop or a single chat. ClawsomeFlow treats <b>people, remote ClawsomeFlow instances, custom webhooks, and CLI agents</b> as first-class executors in the same DAG: reusable, observable, checkpointable, and cost-aware.</p>

<p>
  Works with <b>OpenClaw, Hermes, Claude Code, Codex, Cursor</b> and other CLI agents — and with <b>any system you already run</b> via a simple webhook contract.
</p>

<p>
  <a href="#-quick-start">Quick Start</a> ·
  <a href="https://clawsomeflow.com/docs/">Docs</a> ·
  <a href="#-news">News</a> ·
  <a href="#-who-should-try-it">Who Should Try It</a> ·
  <a href="#-core-features">Core Features</a> ·
  <a href="#-external-execution-nodes">External Nodes</a> ·
  <a href="#%EF%B8%8F-how-it-works">How It Works</a> ·
  <a href="#-why-clawsomeflow">Why ClawsomeFlow</a> ·
  <a href="#-contributor-local-deploy-and-test">Contributor Local Deploy</a> ·
  <a href="#-roadmap">Roadmap</a> ·
  <a href="#-community">Community</a>
</p>

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/Backend-FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/Frontend-React_18-61DAFB?style=for-the-badge&logo=react&logoColor=black">
  <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-4ECDC4?style=for-the-badge">
  <a href="https://discord.gg/hcpMwXnrkM"><img alt="Discord" src="https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white"></a>
</p>

</div>

---

## 📰 News

- **2026-06-02**: ClawsomeFlow public release 🎉

---

## 🎯 Who Should Try It?

- Operators building **AI-native businesses** where work crosses people, tools, and machines — not just a coding sandbox;
- Teams that need **human checkpoints**, irreversible approvals, or real-world steps inside an otherwise automated flow;
- Builders who want **OpenClaw / Hermes / Claude / Codex / Cursor** (and custom agent stacks) collaborating in one graph;
- Anyone tired of **one long chat context** for complex, multi-week work — and who wants reusable, auditable, cost-controlled runs.

---

## ✨ Core Features

ClawsomeFlow is a **harness**: it keeps long-cycle, multi-executor work stable — so capability scales without the process collapsing into an unreviewable chat transcript.

| 🤝 Human + machine + any agent platform | 🔗 Cross-machine collaboration | 🧩 Bring your own executor |
|---|---|---|
| People, local agents, remote ClawsomeFlow, and custom systems sit in the **same DAG** with the same dependency and completion semantics. | Delegate a subtask to another machine’s Flow; stitch results back as upstream context. | A minimal webhook contract — or a custom agent platform you build yourself — plugs in without rewriting the scheduler. |

| ♻️ Reusable & reliably repeatable | 🎛️ Controllable process | 💸 Cost-aware by design |
|---|---|---|
| Define a Flow once; re-run with parameters. Same structure, stable output — not a one-off prompt. | Human checkpoints, selective re-run of subtasks, complaint/improve loops. You steer; the harness remembers. | Split work across nodes so each executor sees a short, relevant context — far cheaper than one mega-agent carrying the whole project. |

| ⏪ Rollback & repo safety | 👁 Observable end-to-end | 🌱 Self-improving runs |
|---|---|---|
| Worktree isolation plus a **built-in cross-process repo lock** so parallel edits don’t corrupt the baseline; merges and results stay reviewable and revertible. | Every dispatch, hand-off, and failure is a RunEvent — live board, edge inbox messages, full replay. | Not happy? Complain; the system reworks and records the lesson for the next run. |

| ⏳ Built for long cycles | 🏢 Beyond coding |
|---|---|
| Flows can span hours to weeks: human wait times, remote jobs, and multi-specialty hand-offs without losing the thread. | Market, ops, content, support, engineering — any specialty that can report a result can be a node. |

## 🛠️ How It Works

From a sentence to a shipped result. You stay in charge of the goal; ClawsomeFlow handles the coordination, the parallelism, and the recovery when things go wrong.

![ClawsomeFlow task orchestration framework](./docs/assets/flow-orchestration-overview.png)

1. **Describe your goal** — Compose a Flow as a graph: local agents, humans, webhooks, and remote ClawsomeFlow nodes as needed.
2. **Executors run their part** — The harness dispatches ready nodes (parallel where possible), isolates agent workspaces, and waits on people or remote systems without losing the thread.
3. **Watch, steer, recover** — Live board (including inbox hand-offs on dependency edges), checkpoints, selective re-run, retry/skip/abort.
4. **Converge & deliver** — A leader summarizes into one reviewed result; history stays auditable and improvable.

---

## 🧪 Developer Mode

Developer mode offers **software-development collaboration projects** a more flexible way to collaborate.

- **Upstream context for every subtask**: for each direct dependency, a subtask is injected with the upstream **agent id, worktree path, branch and base branch**, so it can flexibly build on that work — inspect it, merge that branch, or raise a PR for it — driven entirely by your task description.
- **Cross-branch collaboration in plain language**: in a downstream task, just write *"merge upstream agent X's worktree branch into branch Y"* or *"open a PR for X"*.
- **Built-in lock = absolute parallel-merge reliability**: many branches can develop and merge in parallel without ever racing or corrupting the repo. Whether the scheduler or an agent merges, every merge is serialized on the same lock — you can even direct cross-branch merges or PRs in plain language without risk.
- **Per-subtask auto-merge control**: each subtask can independently toggle auto-merge into the baseline branch.
- **Unique worktree per agent**: each agent creates its own worktree and independent branch from the baseline branch.
- **PR-friendly flow**: for subtasks you want to land via PR or manual review, turn auto-merge OFF.

![Flow Runtime Collaboration Architecture (EN)](./docs/assets/flow-runtime-collab-en.png)

---

## 🤖 Supported Agents

| Agent | Kind | Runtime | Status |
|---|---|---|---|
| **OpenClaw** | `openclaw` | TUI | ⭐ Deeply adapted |
| **Hermes** | `hermes` | TUI | ⭐ Deeply adapted |
| **Claude Code** | `claude` | TUI | ✅ Full support |
| **Codex** | `codex` | TUI | ✅ Full support |
| **Cursor** | `cursor` | TUI | ✅ Full support |
| **OpenCode** | `opencode` | TUI | Testing |
| **Gemini CLI** | `gemini` | TUI | Testing |
| **Kimi CLI** | `kimi` | TUI | Testing |
| **Qwen Code** | `qwen` | TUI | Testing |
| **Qoder CLI** | `qoder` | TUI | Testing |
| **CodeBuddy Code** | `codebuddy` | TUI | Testing |
| **Pi** | `pi` | TUI | Testing |
| **nanobot** | `nanobot` | TUI | Testing |
| **External executor** | `external` | Human / Webhook / Remote ClawsomeFlow | ✅ Full support |

---

## 🌐 External Execution Nodes

Real work always leaves the “AI-only” box: a person must inspect hardware, approve a payment, or decide under ambiguity; another system must run a black-box step; another machine already hosts the right Flow. **External nodes** put those executors in the same DAG as your agents — same dependencies, same hand-off of completion summaries.

In the Flow editor: Owner source → **External execution**, then pick an owner kind:

| Owner kind | Who runs it | How the result returns |
|---|---|---|
| **Human** | A person on the Run page | Submit on the todo card |
| **Remote ClawsomeFlow** | A Flow on another machine | Peer finishes → result comes back |
| **Generic interface (webhook)** | Your HTTP service | Receive task package → callback when done |

### Remote ClawsomeFlow (copy-paste wiring — no hand-typed CLI)

1. **Peer**: open the target Flow's editor and click **Copy remote call info** next to the title — you get a JSON blob (base URL, Flow ID, param fields, pairing credential). Send it to the origin.
2. **Origin**: in the subtask pick **Remote ClawsomeFlow**, paste the blob into **Remote Flow call info** and save the subtask — base URL / Flow ID / credential are registered automatically; the secret is stored locally, never in the Flow spec.

Across machines the peer must first run `csflow external expose on` to allow non-loopback access (this changes the service bind, so it stays a CLI step).

**Params flow automatically (only when the remote Flow declares param fields)**: upstream tasks are then asked to report values for them; you may also type known values on the node (they override upstream). Unfilled fields take the union of upstream reports; anything still empty is sent as `参数为空`. If the remote Flow has no param fields, no special handling is applied.

### Generic interface (webhook)

**On ClawsomeFlow:** pick **Generic interface (webhook)** and set your endpoint URL. If the partner must reach you from another host:

```bash
csflow external callback-url http://<origin-host>:17017
csflow external expose on
```

**On your system:** accept the task `POST`, then when done:

```http
POST {callbackUrl}
Authorization: Bearer {callbackToken}

{"status": "success", "summary": "what you delivered"}
```

Two JSON messages — that’s the whole integration.

## 🤔 Why ClawsomeFlow?

The hard part of agentic work is rarely “the model isn’t smart enough”. It’s that **collaboration has no harness**: process lives in a prompt, context balloons, humans and other machines can’t join cleanly, and long projects become unreviewable.

ClawsomeFlow’s bet is simple: **put coordination in a durable harness** — open executors (human / remote / webhook / any agent CLI), short per-node context, checkpoints, locks, observability, and reuse — so capability can grow without the process falling apart.

| | Typical “one agent + one chat” | ClawsomeFlow |
|---|---|---|
| **Who can execute** | Mostly the model in front of you | Humans, remote instances, webhooks, many agent platforms |
| **Long projects** | Context rot; hard to pause / resume safely | Flows built for long cycles + human wait + re-run |
| **Cost** | One context carries everything | Split nodes → shorter contexts, lower token spend |
| **Control** | Hope the prompt holds | Checkpoints, selective re-run, complaint loop, rollback |
| **Concurrency** | Easy to step on the same repo | Worktree isolation + built-in repo lock |
| **Scope** | Coding demos | Cross-specialty, end-to-end operational flows |

**You own the goal. ClawsomeFlow keeps the multi-party execution under control.**

## 🚀 Quick Start

> **Before you start — make sure your agent CLIs work.**
> ClawsomeFlow drives external agent CLIs (`claude`, `codex`, `hermes`, …) and
> agents **inherit your global CLI authentication**. So first install and log in
> to each CLI you plan to use and confirm it runs on its own (e.g. `claude -p hi`,
> `hermes` chat, `codex`). If a CLI isn't authenticated, agents using it will stall
> on a login prompt. For Hermes you can also set the model/provider/key per agent
> in **Settings → Model**. If you hit auth errors, verify the CLI's own
> model/provider config first.
>
> **Qoder / CodeBuddy** need a one-time auth: CodeBuddy via `codebuddy` →
> interactive login; Qoder via `export QODER_PERSONAL_ACCESS_TOKEN=…` (or
> `qodercli` → `/login`). ClawsomeFlow auto-seeds their folder-trust config
> (`trustAll` / `trustDirectories`) so unattended runs don't stall on the
> "trust this folder?" prompt — no action needed there.


### Install

Linux/macOS
```bash
curl -fsSL https://clawsomeflow.com/install.sh | bash
```


### Common Commands

Most of the time you only need these three:

```bash
csflow start      # start the service, print the console URL
csflow status     # is it running? version, mode, paths
csflow upgrade    # update to the latest release (flows/runs/settings preserved)
```

A few more for day-to-day use:

```bash
# Lifecycle
csflow stop
csflow doctor                              # health check (deps + config + gateway)
csflow uninstall --yes                     # stop service + unregister OpenClaw (keep data)
csflow uninstall --purge-data              # full wipe: type PURGE to confirm (irreversible)

# Flow / Run
csflow flows list
csflow runs start <flow-id> --input k=v    # trigger a run with parameter fields
csflow runs list
csflow runs abort <run-id>

# Agent governance
csflow agents list
# Creating agents and chatting with them is done in the Web UI ("My Team"),
# not the CLI.

# MCP: let an agent drive Flows remotely (via its own channels, e.g. Telegram)
csflow mcp install --platform hermes --agent <id>   # per-agent (Hermes; omit --agent for the default profile)
csflow mcp install --platform codex                 # global (codex/claude/cursor/gemini/opencode)
csflow mcp print-config --platform openclaw         # snippet to paste for unsupported platforms
```

Every command accepts `--help`. Full CLI reference: <https://clawsomeflow.com/docs/>

---

## 🔌 MCP: drive Flows from an agent

Run ClawsomeFlow as an **MCP server**. Point one of your agents at it and — through that agent's own channels (Feishu, Telegram, …) — you can, in plain language, ask which Flows exist, run one, and get the **leader's final work report** back. A typical loop: *you send a file + a request over Telegram → the agent picks the right Flow and runs it → reads the leader report → replies over Telegram*.

### Talking to your agent (examples)

- "What ClawsomeFlow flows can I run?"
- "Run the XXXX task."
- "Show me the run result."
- "Cancel the XXX run."

The agent organizes the inputs itself from your request, so you rarely name fields explicitly — describe the task and let it map your words onto the Flow's parameters.

### Register it with your agent

```bash
csflow mcp install --platform hermes                   # Hermes: default profile
csflow mcp install --platform hermes --agent <id>      # Hermes: a specific agent profile
csflow mcp install --platform codex                    # global platforms (see table)
csflow mcp uninstall --platform codex                  # remove it again
```

| Platform | Scope | Config written |
|---|---|---|
| `hermes` | per-agent (`--agent <id>`; omit → **default profile**) | `~/.hermes/…/config.yaml` `mcp_servers` |
| `claude` | global | `~/.claude.json` `mcpServers` |
| `cursor` | global | `~/.cursor/mcp.json` `mcpServers` |
| `gemini` | global | `~/.gemini/settings.json` `mcpServers` |
| `codex` | global | `~/.codex/config.toml` `[mcp_servers.*]` |
| `opencode` | global | `~/.config/opencode/opencode.json` `mcp` |
| `openclaw`, `kimi`, `qwen`, `nanobot` | manual | print-config only (paste into the platform's own MCP config) |

Writes are **non-destructive** (existing servers and other keys are preserved) and **idempotent**. `install` skips a platform whose CLI isn't on `PATH` unless you pass `--force`.

### Manual configuration

For any platform, print a ready-to-paste snippet instead of writing files:

```bash
csflow mcp print-config --platform claude     # JSON: { "mcpServers": { "clawsomeflow": … } }
csflow mcp print-config --platform codex      # TOML: [mcp_servers.clawsomeflow]
csflow mcp print-config --platform hermes     # YAML: mcp_servers: …
```

The server entry is always the same command — register it manually if your platform isn't listed:

```json
{
  "mcpServers": {
    "clawsomeflow": { "command": "csflow", "args": ["mcp", "serve"] }
  }
}
```

Note: the ClawsomeFlow service must be running (`csflow start`) to use MCP.

---

## 👩‍💻 Contributor Local Deploy and Test

For contributors iterating on source code, use the isolated developer entrypoint:

```bash
bash scripts/deploy-contributor.sh
```

Default behavior of `deploy-contributor.sh`:

- Uses isolated data/runtime under `~/.clawsomeflow-dev` (does not reuse `~/.clawsomeflow`).
- Starts backend on `17117` and Vite on `5174`.
- Keeps ClawTeam runtime isolated via `~/.clawsomeflow-dev/.clawteam-data`.

`bash scripts/deploy-contributor.sh` is recommended for day-to-day source testing because it keeps regular user service state isolated.

Example with custom profile/ports:

```bash
CSFLOW_DEV_HOME=~/.clawsomeflow-dev-alice \
CSFLOW_DEV_BACKEND_PORT=18117 \
CSFLOW_DEV_FRONTEND_PORT=5184 \
bash scripts/deploy-contributor.sh
```

### Running the test suite

Run all tests inside Docker — a separate filesystem and network namespace mean a
test can never touch your real `~/.clawsomeflow` / `~/.openclaw` or a running
gateway:

```bash
scripts/test-in-docker.sh                                       # full backend suite
scripts/test-in-docker.sh -q backend/tests/test_api_guard.py   # subset (args → pytest)
```

Requires Docker and a local ClawTeam checkout (a sibling `../ClawTeam`, or set
`CLAWTEAM_SRC=/path/to/ClawTeam`). Do **not** run `pytest` directly on a host
that has a csflow/openclaw service running — it would reach the real gateway on
`:18789`.

### Stop the contributor service

To stop the contributor profile started by `deploy-contributor.sh`, use the
dedicated stop script:

```bash
bash scripts/stop-contributor.sh
```

Do **not** use `csflow stop` for the contributor profile — that targets
the end-user service. If you used a custom profile, pass the same env overrides:

```bash
CSFLOW_DEV_BACKEND_PORT=18117 CSFLOW_DEV_FRONTEND_PORT=5184 \
bash scripts/stop-contributor.sh
```

---

## 🗺️ Roadmap

| Phase | Content | Status |
|---|---|---|
| **P0** | **Agent Store** — a shareable marketplace for ready-made Agents, Teams and Flow templates: install, reuse, and contribute domain experts in one click. | 🚧 In progress |
| **P1** | **Broader Agent platform support** — onboard more CLI Agent runtimes and keep pace with emerging ecosystems, so any Agent can join the same graph. | 🚧 In progress |
| **P2** | **Mobile console** — a mobile-friendly console to monitor and intervene in Runs anywhere. | 💡 Exploring |
| **P3** | **Cloud & SSH Agents** — drive Agents on remote / cloud hosts over SSH, scaling collaboration beyond a single machine. | 💡 Exploring |

---

## 🙏 Acknowledgements

- **[ClawTeam]** — the spark that inspired this project. Thank you for showing what Agent self-organization can be.
- **Our Agent platform teammates** — the real "team members" that do the actual work inside every Flow: **Claude**, **OpenClaw**, **Codex**, **Gemini**, and the growing roster of CLI Agents. ClawsomeFlow is only as clawsome as the Agents it coordinates.

---

## 💬 Community

If ClawsomeFlow helps you coordinate your Agent team, **please give us a ⭐ Star** — it genuinely keeps us going.

Got questions about using ClawsomeFlow, or curious about building an **OPC (One-Person Company)**? Come hang out with us — join our Discord server or scan the QR code below to join our WeChat discussion group:

<p align="center">
  <a href="https://discord.gg/hcpMwXnrkM"><img alt="Discord" src="https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white"></a>
</p>

<p align="center">
  <img src="./docs/assets/wechat-group-qr.png?v=4" alt="ClawsomeFlow WeChat Group" width="240" />
</p>

---

## 📄 License

MIT
