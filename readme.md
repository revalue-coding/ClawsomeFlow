<div align="center">

<h1>⚡ ClawsomeFlow ⚡</h1>

<p>
  🌐 <a href="https://clawsomeflow.com"><b>clawsomeflow.com</b></a> ·
  📖 <a href="https://clawsomeflow.com/docs/"><b>Docs</b></a>
</p>

<p>
  <img src="./docs/assets/readme-hero.svg" alt="Make your Multi-agent Workflow Clawsome" width="960" />
</p>

<p>
  <b>English</b> ·
  <a href="./readme.zh.md">简体中文</a>
</p>

<p><b>Turn your goal into a task flow, and let an active scheduler drive a team of AI agents to execute it — parallel, isolated, observable, and convergent. You orchestrate the work; ClawsomeFlow keeps it under control.</b></p>

<p>
  <b>Full compatibility with</b> OpenClaw, Claude Code, Codex, Cursor, Hermes and other CLI Agents.
</p>

<p>
  <a href="#-quick-start">Quick Start</a> ·
  <a href="https://clawsomeflow.com/docs/">Docs</a> ·
  <a href="#-news">News</a> ·
  <a href="#-who-should-try-it">Who Should Try It</a> ·
  <a href="#-core-features">Core Features</a> ·
  <a href="#%EF%B8%8F-how-it-works">How It Works</a> ·
  <a href="#-why-clawsomeflow">Why ClawsomeFlow</a> ·
  <a href="#-contributor-local-deploy-and-test">Contributor Local Deploy</a> ·
  <a href="#-roadmap">Roadmap</a> ·
  <a href="#-wechat-community">WeChat Community</a>
</p>

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/Backend-FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/Frontend-React_18-61DAFB?style=for-the-badge&logo=react&logoColor=black">
  <img alt="Built on ClawTeam" src="https://img.shields.io/badge/Built_on-ClawTeam-FF6B6B?style=for-the-badge&logo=git&logoColor=white">
  <img alt="License MIT" src="https://img.shields.io/badge/License-MIT-4ECDC4?style=for-the-badge">
</p>

</div>

---

## 📰 News

- **2026-06-02**: ClawsomeFlow public release 🎉

---

## 🎯 Who Should Try It?

- Developers and teams who want **multiple AI agents to genuinely collaborate** — instead of each one running off on its own;
- Engineering-minded folks who are **done with the black box of prompt self-scheduling** and want predictable behavior with controllable cost;
- Power users who need **parallelism + isolation + rollback** to run batch workloads;
- People who aren't deeply versed in driving **OpenClaw / Hermes / Claude Code / Codex** and the like, but still want to **put their capabilities to work** — ClawsomeFlow gives you a natural-language management layer over all of them;
- Builders seriously exploring the **OPC (One-Person Company)** — putting an agent team to work on their behalf.

---

## ✨ Core Features

ClawsomeFlow turns scattered AI agents into a controllable engineering system — from the first instruction to the final, reviewable result.

| 🗣️ Get it done in natural language | 🧠 Precise orchestration, not guesswork | 🚀 Many agents, one graph |
|---|---|---|
| Define flows, create agents, orchestrate tasks, and step in at runtime — all by describing what you want. No glue code, no SDK wrangling. | Control flow lives in code, not in a prompt. The scheduler decides dispatch, retry, timeout and convergence — so behavior is predictable and tokens aren't wasted. | Lay out your work as a DAG and let multiple agents collaborate in parallel; a leader summarizes and converges the results into one deliverable. |

| 🔐 Isolation & rollback by default | 📊 Observability you can audit | 🔄 A system that improves itself |
|---|---|---|
| Every agent runs in its own isolated workspace and branch — parallel work without cross-talk or accidental writes, with checkpoint / merge / cleanup built in. | Every dispatch, completion and failure is recorded as a RunEvent — each run is traceable, replayable and reviewable, with no black boxes. | Not happy with a result? File a complaint and the system reflects, reworks, and writes the lesson back — so the next run is better than the last. |

ClawsomeFlow inherits the following capabilities from ClawTeam:

- **Git Worktree workspace isolation**: each Agent has an independent branch and directory, running in parallel without interference, with checkpoint / merge / cleanup support.
- **Inter-Agent messaging**: point-to-point inbox and broadcast, so team members share progress in real time.

> On top of this, ClawsomeFlow adds **AI combined with precise orchestration, deep OpenClaw adaptation, failure convergence, human guardrails and Web productization**.

---

## 🛠️ How It Works

From a sentence to a shipped result. You stay in charge of the goal; ClawsomeFlow handles the coordination, the parallelism, and the recovery when things go wrong.

1. **Describe your goal** — Tell ClawsomeFlow what you want in plain language, or compose a Flow visually as a graph of tasks and dependencies.
2. **Agents run in parallel** — The scheduler actively dispatches ready tasks to the right agents, each in its own isolated workspace, and drives them to completion.
3. **Watch, steer, recover** — Follow every step live. Retry, skip or abort with clear strategies, and approve results at human checkpoints before anything lands.
4. **Converge & deliver** — A leader merges the parallel work into one reviewed deliverable — and the run history stays fully auditable.

---

## 🤖 Supported Agents

| Agent | Kind | Runtime | Status |
|---|---|---|---|
| **OpenClaw** | `openclaw` | TUI | ⭐ Deeply adapted |
| **Claude Code** | `claude` | TUI | ✅ Full support |
| **Codex** | `codex` | TUI | ✅ Full support |
| **Gemini CLI** | `gemini` | TUI | Testing|
| **Cursor** | `cursor` | TUI | ✅ Full support |
| **Hermes** | `hermes` | TUI | ✅ Full support |
| **Kimi CLI** | `kimi` | TUI | Testing |
| **Qwen Code** | `qwen` | TUI | Testing |
| **OpenCode** | `opencode` | TUI | Testing |
| **nanobot** | `nanobot` | TUI | Testing |

---

## 🤔 Why ClawsomeFlow?

The common pain point of multi-agent frameworks is not "insufficient model capability", but "unstable collaboration control flow": the process is written in the Prompt, the final behavior depends on the Agent's in-the-moment understanding and model quality, and the system's predictability, cost and recoverability are all too weak.

ClawsomeFlow's approach is direct: **migrate coordination from natural language back into code, make concurrency isolation a default capability, and make failure handling a built-in part of the process.**

### 🆚 Comparison with Other Agent Orchestration Platforms

| Dimension | Other Multi-Agent Orchestration Platforms | ✅ ClawsomeFlow |
|---|---|---|
| **Task orchestration fit** | Mostly framework-specific, bound to a single ecosystem | Task orchestration is **deeply adapted to OpenClaw Agents**, while also being compatible with any CLI Agent (Claude / Codex / Cursor, etc.) collaborating in the same graph |
| **Concurrency & isolation** | Easy contention in parallel, workspace conflicts, context cross-talk | Solves OpenClaw collaboration instability: **workspace isolation and rollback under multi-task parallelism, and thoroughly resolves session conflicts** |
| **Control approach** | Pure Prompt self-scheduling (black box) or pure code (heavy) | **AI combined with precise orchestration**: get everything done in natural language while the scheduler precisely controls behavior (dispatch / retry / timeout / abort) |
| **Engineering harness** | Generally missing; failures rely on Agent improvisation | **Harness engineering**: human checkpoints, rollbackable results, complaint-loop mechanism, periodic entropy management |
| **Failure recovery** | Relies on Agent self-healing, uncertain outcome | Clear retry / skip / abort strategies, recovery paths folded into a standard state machine |
| **Observability** | Context is mostly a black box | Full-chain RunEvent — traceable, auditable, replayable |

#### ✨ The Result?

**You own the goal, and ClawsomeFlow turns multi-agent collaborative execution into a stable, controllable, convergent engineering system.**

---

## 🧩 Relationship with ClawTeam

ClawsomeFlow is built on top of **ClawTeam**.

### 🔍 ClawTeam vs ClawsomeFlow at a Glance

| Dimension | ClawTeam | ClawsomeFlow |
|---|---|---|
| **Positioning** | Swarm-intelligence protocol foundation (Agent self-organization) | Agent workflow orchestration platform |
| **Collaboration driver** | Agents self-poll and self-schedule in the Prompt | Server-side scheduler actively dispatches, deterministic execution |
| **Task model** | Kanban + dependency chain | DAG Flow compilation, Leader summarizes and converges |
| **OpenClaw adaptation** | Supported as an optional CLI Agent | Deeply adapted, resolving session and workspace concurrency conflicts |
| **Failure & guardrails** | Basic lifecycle protocol | Human checkpoints / rollback / complaint-loop / entropy management |
| **Skill configuration** | Requires extra skill setup on the Agent platform | No extra skill configuration needed, works out of the box |
| **Persistent specialist members** | Swarm intelligence can't flexibly schedule persistent, platform-managed agents | Create persistent members with their own expertise and continuous self-learning on **Hermes / OpenClaw / Claude Code** and more, then invoke them on demand |
| **Usage form** | CLI + MCP + monitoring dashboard | Web UI + CLI, full-flow governance in natural language |

---

## 🚀 Quick Start

> **Before you start — make sure your agent CLIs work.**
> ClawsomeFlow drives external agent CLIs (`claude`, `codex`, `hermes`, …) and
> agents **inherit your global CLI authentication**. So first install and log in
> to each CLI you plan to use and confirm it runs on its own (e.g. `claude -p hi`,
> `hermes` chat, `codex`). If a CLI isn't authenticated, agents using it will stall
> on a login prompt. For Hermes you can also set the model/provider/key per agent
> in **Settings → Model**. If you hit auth errors, verify the CLI's own
> model/provider config first.

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

# Flow / Run
csflow flows list
csflow runs start <flow-id> --input k=v    # trigger a run with parameter fields
csflow runs list
csflow runs abort <run-id>

# Agent governance
csflow agents list
# Creating agents and chatting with them is done in the Web UI ("My Team"),
# not the CLI.
```

Every command accepts `--help`. Full CLI reference: <https://clawsomeflow.com/docs/>

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
| **P1** | **Broader Agent platform support** — onboard more CLI Agent runtimes and keep pace with emerging ecosystems, so any Agent can join the same graph. | 📅 Planned |
| **P2** | **Mobile & server mode** — a mobile-friendly console plus multi-user server deployment, to monitor and intervene in Runs anywhere. | 💡 Exploring |
| **P3** | **Cloud & SSH Agents** — drive Agents on remote / cloud hosts over SSH, scaling collaboration beyond a single machine. | 💡 Exploring |

---

## 🙏 Acknowledgements

- **[ClawTeam]** — the spark that inspired this project. Thank you for showing what Agent self-organization can be.
- **Our Agent platform teammates** — the real "team members" that do the actual work inside every Flow: **Claude**, **OpenClaw**, **Codex**, **Gemini**, and the growing roster of CLI Agents. ClawsomeFlow is only as clawsome as the Agents it coordinates.

---

## 💬 WeChat Community

If ClawsomeFlow helps you coordinate your Agent team, **please give us a ⭐ Star** — it genuinely keeps us going.

Got questions about using ClawsomeFlow, or curious about building an **OPC (One-Person Company)**? Come hang out with us — scan the QR code below to join our WeChat discussion group:

<p align="center">
  <img src="./docs/assets/wechat-group-qr.png" alt="ClawsomeFlow WeChat Group" width="240" />
</p>

---

## 📄 License

MIT
