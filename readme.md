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

<p>Real-world work rarely fits inside a single agent. Someone still has to touch the physical world, sign off on an irreversible action, or make the call that matters — and real collaboration usually spans machines and tools, rather than staying boxed into one computer, one skill, or one chat. ClawsomeFlow breaks agent collaboration out of that box: it reaches across machines and out into the wider world of agent tooling. <b>CLI agents, real people, remote ClawsomeFlow instances, custom agent tools of your own — anything you could reasonably call an "execution unit"</b> can take a seat in one shared collaboration graph, and Harness engineering keeps complex, long-running projects moving in sync.</p>

<p>ClawsomeFlow isn't here to replace the agents or workflows you already have — it just helps them reach further.</p>

<p>
  <a href="#-quick-start">Quick Start</a> ·
  <a href="https://clawsomeflow.com/docs/">Docs</a> ·
  <a href="#-news">News</a> ·
  <a href="#-who-should-try-it">Who Should Try It</a> ·
  <a href="#-why-clawsomeflow">Why ClawsomeFlow</a> ·
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

- **2026-07**: ClawsomeFlow 0.2.0 — agent collaboration finally breaks out of the single machine, bringing every kind of executor into one collaboration graph.
- **2026-06**: ClawsomeFlow public release 🎉

---

## 🎯 Who Should Try It?

- Operators and builders of **AI-native businesses** whose work crosses people, tools, and machines — not just a coding sandbox;
- Practitioners tired of carrying a complex, long-running project on **one endless chat** — who want flows that are reusable, auditable, partially re-runnable, cost-controlled, and dependable in what they ship.

---

## 🤔 Why ClawsomeFlow?

The hard part of multi-agent work is rarely "the model isn't smart enough." It's that **collaboration has no harness**: the process lives inside a prompt, context balloons, people and other machines can't cleanly join in, and long projects drift past the point where anyone can review them.

ClawsomeFlow's take is blunt: **put coordination into a durable harness** — open executors (human / remote execution unit / webhook / agent CLI), short-context nodes, checkpoints, repo locks, observability, and reuse — so the process holds together as capability grows.

| | Typical "one agent + one chat" | ClawsomeFlow |
|---|---|
| **Who can execute** | Mostly the model in front of you | Humans, remote instances, webhooks, many agent platforms |
| **Long projects** | Context rots; hard to pause / resume safely | Flows built for long cycles + human waits + re-runs |
| **Cost** | One context carries everything | Split into nodes → shorter contexts, lower token spend |
| **Control** | Hope the prompt holds | Checkpoints, reviewable & partially re-runnable subtasks, complaint loop, rollback |
| **Concurrency** | Easy to collide on the same repo | Worktree isolation + built-in repo lock |
| **Scope** | Coding demos | Cross-specialty, end-to-end business flows |

---

## ✨ Core Features

**🤝 Fuse many kinds of executor** — collaboration reaches past your laptop, out to remote peers and the wider world of agent tooling. Real people, CLI agents of every flavor, remote ClawsomeFlow instances, and your own custom agent tools all share **one DAG**, with the same dependency and completion semantics. Hand a subtask off to another machine, or wire in an executor you built yourself — a tiny webhook contract is all it takes, and the scheduler never changes.

**🧩 Harness engineering** — what keeps long-running, multi-executor work stable, so capability can grow without the process falling apart: split into nodes for shorter context and lower token spend; human checkpoints, partial subtask re-runs, and a complaint-driven improvement loop; worktree isolation plus a **built-in cross-process repo lock** so parallel edits never corrupt the baseline; every dispatch, hand-off, and failure captured as a replayable RunEvent; and Flows you define once and re-run with parameters — dependable output across anything from hours to weeks.

---

## 🌐 External Execution Nodes

**External execution nodes** are what break local agent collaboration wide open, bringing every kind of executor into one collaboration graph.

| Node type | Who runs it |
|---|---|
| **Human** | A real person |
| **Remote ClawsomeFlow** | A ClawsomeFlow on another machine |
| **Generic interface** | Any general-purpose execution unit |

---

## 🚀 Quick Start

### Install

Linux / macOS
```bash
curl -fsSL https://clawsomeflow.com/install.sh | bash
```

### Common Commands

Most of the time you only need these three:

```bash
csflow start      # start the service, print the console URL
csflow status     # is it running? version, mode, paths
csflow upgrade    # update to the latest release (flows / runs / settings preserved)
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
```

Every command accepts `--help`. Full CLI reference: <https://clawsomeflow.com/docs/>
> PS: **if your flow includes an Agent CLI execution node, make sure that CLI already works on your machine first.**

---

## 🔌 MCP: drive Flows from an agent

Run ClawsomeFlow as an **MCP server**. Point one of your agents at it and — through that agent's own channels (Feishu, Telegram, …) — you can ask in plain language which Flows exist, run one, and get the **leader's final work report** back. A typical loop: *you send a file + a request over Telegram → the agent picks the right Flow and runs it → reads the leader report → replies over Telegram*.

### Talking to your agent (examples)

- "What ClawsomeFlow flows can I run?"
- "Run the XXXX task."
- "Show me the run result."
- "Cancel the XXX run."

The agent works out the inputs from your request, so you rarely name fields explicitly — just describe the task and it maps your words onto the Flow's parameters.

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

Note: the ClawsomeFlow service must be running (`csflow start`) for MCP to work.

---

## 🧪 Developer Mode

Developer mode gives **software-development collaboration projects** a more flexible way to work together.

- **Upstream context for every subtask**: for each direct dependency, a subtask is handed the upstream **agent id, worktree path, branch, and base branch** — so it can build on that work however it likes: inspect it, merge the branch, or open a PR for it, all driven by your task description.
- **Direct cross-branch collaboration in plain language**: in a downstream task, just write "merge upstream agent X's worktree branch into branch Y" or "open a PR for X."
- **Built-in lock = rock-solid parallel merges**: many branches can develop and merge in parallel without ever racing or corrupting the repo. Whether the scheduler or an agent does the merging, every merge is serialized on the same lock — so you can even direct cross-branch merges / PRs in plain language, risk-free.
- **Per-subtask auto-merge control**: each subtask can independently decide whether to auto-merge into the baseline branch.
- **A unique worktree per agent**: each agent creates its own worktree and independent branch off the baseline branch.
- **PR-friendly**: for subtasks you'd rather land via PR or manual review, just turn auto-merge off.

![Flow runtime collaboration architecture](./docs/assets/flow-runtime-collab-en.png)

---

## 🤖 Supported Agent Platforms (local)

| Agent | Kind | Runtime | Status |
|---|---|---|---|
| **OpenClaw** | `openclaw` | TUI | ⭐ Deeply adapted |
| **Hermes** | `hermes` | TUI | ⭐ Deeply adapted |
| **Claude Code** | `claude` | TUI | ✅ Full support |
| **Codex** | `codex` | TUI | ✅ Full support |
| **Cursor** | `cursor` | TUI | ✅ Full support |
| **OpenCode** | `opencode` | TUI | 🧪 Testing |
| **Gemini CLI** | `gemini` | TUI | 🧪 Testing |
| **Kimi CLI** | `kimi` | TUI | 🧪 Testing ([official install.sh](https://code.kimi.com/kimi-code/install.sh) kimi-code recommended) |
| **Qwen Code** | `qwen` | TUI | 🧪 Testing |
| **Qoder CLI** | `qoder` | TUI | 🧪 Testing |
| **CodeBuddy Code** | `codebuddy` | TUI | 🧪 Testing |
| **Pi** | `pi` | TUI | 🧪 Testing |
| **nanobot** | `nanobot` | TUI | 🧪 Testing |

---

## 🗺️ Roadmap

| Phase | Content | Status |
|---|---|---|
| **P1** | **More ways for agents to collaborate** — push collaboration beyond a single machine, keep pace with emerging ecosystems, and let agents of any kind work in the same graph. | 🚧 In progress |
| **P2** | **Mobile** — a mobile console to monitor and step into Runs from anywhere. | 💡 Exploring |

---

## 💬 Community

If ClawsomeFlow helps you coordinate your team's work, **please give us a ⭐ Star** — it's what keeps us going.

Got questions about using ClawsomeFlow, or curious about building an **OPC (One-Person Company)**? Come hang out with us — join our Discord server, or scan the QR code below to join our WeChat discussion group:

<p align="center">
  <a href="https://discord.gg/hcpMwXnrkM"><img alt="Discord" src="https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white"></a>
</p>

<p align="center">
  <img src="./docs/assets/wechat-group-qr.png?v=5" alt="ClawsomeFlow WeChat Group" width="240" />
</p>

---

## 📄 License

MIT
