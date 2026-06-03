<div align="center">

<h1>⚡ ClawsomeFlow ⚡</h1>

<h3>Make your multi-agent workflow <em>Clawsome!</em></h3>

<p>
  <b>English</b> ·
  <a href="./readme.zh.md">简体中文</a>
</p>

<p>Describe your goal in natural language, and turn multi-agent collaboration into a controllable, observable, convergent engineering system.</p>

<p>
  ClawsomeFlow is a vertical-domain <b>Agent workflow orchestration platform</b>: define tasks as a DAG Flow,
  and let an async scheduler actively drive multiple Agents to collaborate in parallel, with built-in engineering
  guardrails such as <b>isolation / rollback / complaint-loop / entropy management</b>.
</p>

<p>
  <b>Full compatibility with</b> OpenClaw, Claude Code, Codex, Cursor, Hermes and other CLI Agents.
</p>

<p>
  <a href="#-news">News</a> ·
  <a href="#-core-features">Core Features</a> ·
  <a href="#-supported-agents">Supported Agents</a> ·
  <a href="#-why-clawsomeflow">Why ClawsomeFlow</a> ·
  <a href="#-relationship-with-clawteam">Relationship with ClawTeam</a> ·
  <a href="#-quick-start">Quick Start</a> ·
  <a href="#-roadmap">Roadmap</a>
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

## ✨ Core Features

Building on top of ClawTeam's swarm-intelligence capabilities, ClawsomeFlow adds the two missing engineering layers — **orchestration + product**:

| 🌳 Deep OpenClaw Adaptation | 🧠 AI + Precise Orchestration | 🗣️ Get Everything Done in Natural Language | 🔄 Complaint-Loop Mechanism |
|---|---|---|---|
| For OpenClaw's blurry session boundaries and workspace concurrency conflicts under multi-task parallelism, we apply dual session-and-directory isolation and fold recovery paths into a standard state machine. | Take control flow back from the Prompt into code: the scheduler decides dispatch, retry, timeout and convergence — behavior is controllable, with significantly fewer wasted tokens. | Flow definition, Agent creation, task orchestration, and runtime intervention can all be done in natural language via the Web UI / CLI. | A Run supports the "user complaint → reflective processing → write back experience" loop, so the system keeps self-improving. |

| 🚀 Multi-Agent Collaboration | 📊 Enterprise-Grade Observability | 🔐 Isolation & Governance Together | 🧩 Compatible with Existing Ecosystems |
|---|---|---|---|
| Supports OpenClaw / Claude / Codex / Cursor / Hermes collaborating in the same graph. | Every dispatch / completion / failure is recorded as a RunEvent — auditable, replayable, billable. | Three-layer isolation across team / session / worktree, avoiding cross-talk and accidental writes. | We don't reinvent the protocol layer; we reuse ClawTeam CLI + MCP and its swarm collaboration and monitoring. |

### 🦞 The Swarm-Intelligence Foundation Inherited from ClawTeam

ClawsomeFlow stands on the shoulders of [ClawTeam](https://github.com/HKUDS/ClawTeam), faithfully inheriting its swarm-collaboration core:

- **Self-organizing Agent collaboration**: the Leader dispatches, Workers self-report status and results, CLI Agents are plug-and-play with no custom SDK required.
- **Git Worktree workspace isolation**: each Agent has an independent branch and directory, running in parallel without interference, with checkpoint / merge / cleanup support.
- **Inter-Agent messaging**: point-to-point inbox and broadcast, so team members share progress in real time.

> On top of this, ClawsomeFlow layers in **deep OpenClaw adaptation, DAG orchestration scheduling, failure convergence, human guardrails, Web productization and multi-user governance**.

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
| **Usage form** | CLI + MCP + monitoring dashboard | Web UI + CLI, full-flow governance in natural language |

---

## 🚀 Quick Start

### Install

```bash
curl -fsSL https://clawsomeflow.com/install.sh | bash

```

### Common Commands

```bash
# Lifecycle
csflow start
csflow stop
csflow status
csflow doctor

# Flow / Run
csflow flows list
csflow runs list
csflow runs start <flow-id> --input k=v
csflow runs abort <run-id>

# Agent governance
csflow agents list
csflow agents create "Describe the Agent you want in natural language"
csflow agents chat <agent-id> "Keep improving this Agent's capabilities"
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

## 📄 License

MIT
