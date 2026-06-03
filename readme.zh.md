<div align="center">

<h1>⚡ ClawsomeFlow ⚡</h1>

<h3>Make your multi-agent workflow <em>Clawsome!</em></h3>

<p>
  <a href="./readme.md">English</a> ·
  <b>简体中文</b>
</p>

<p><b>用自然语言描述目标，把多 Agent 协作变成可控、可观测、可收敛的工程系统。</b></p>

<p>
  ClawsomeFlow 是一个面向垂直领域的 <b>Agent 工作流编排平台</b>：以 DAG Flow 定义任务，
  由异步调度器主动驱动多个 Agent 并行协作，并内建 <b>隔离 / 回滚 / 投诉闭环 / 熵管理</b> 等工程化护栏。
</p>

<p>
  <b>Full compatibility with</b> OpenClaw、Claude Code、Codex、Cursor、Hermes等 CLI Agent。
</p>

<p>
  <a href="#-news">News</a> ·
  <a href="#-核心特性">核心特性</a> ·
  <a href="#-支持的-agent-平台">支持的 Agent 平台</a> ·
  <a href="#-为什么是-clawsomeflow">为什么是 ClawsomeFlow</a> ·
  <a href="#-与-clawteam-的关系">与 ClawTeam 的关系</a> ·
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-路线图">路线图</a>
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

- **2026-06-02**：ClawsomeFlow 公开发布 🎉

---

## ✨ 核心特性

ClawsomeFlow 在沿用 ClawTeam 群体智能能力的基础上，补齐了「编排 + 产品」两层工程能力：

| 🌳 OpenClaw 深度适配 | 🧠 AI + 精确编排 | 🗣️ 自然语言完成所有工作 | 🔄 投诉闭环机制 |
|---|---|---|---|
| 针对 OpenClaw 多任务并行的会话边界模糊与 workspace 并发冲突，做了会话与目录双隔离，并将恢复路径纳入标准状态机。 | 把控制流从 Prompt 拿回代码：调度器决定派发、重试、超时与收敛，行为可控，显著减少无效 token。 | Flow 定义、Agent 创建、任务编排、运行干预，都可用自然语言在 Web UI / CLI 完成。 | Run 支持「用户投诉 → 反思处理 → 回写经验」的闭环，让系统持续自增长。 |

| 🚀 多 Agent 协同 | 📊 企业级可观测性 | 🔐 隔离与治理并重 | 🧩 与现有生态兼容 |
|---|---|---|---|
| 支持 OpenClaw / Claude / Codex / Cursor / Hermes  同图协同。 | 每个 dispatch / completion / failure 都记录为 RunEvent，可审计、可回放、可计费。 | team / session / worktree 三层隔离，避免串扰与误写。 | 不重造协议层，沿用 ClawTeam CLI + MCP，复用其群体协作与监控能力。 |

### 🦞 沿用自 ClawTeam 的群体智能基座

ClawsomeFlow 站在 [ClawTeam](https://github.com/HKUDS/ClawTeam) 的肩膀上，原汁原味继承了它的群体协作内核：

- **Agent 自组织协作**：Leader 派发、Worker 自报告状态与结果， CLI Agent 即插即用，无需自定义 SDK。
- **Git Worktree 工作区隔离**：每个 Agent 拥有独立分支与目录，并行互不干扰，支持 checkpoint / merge / cleanup。
- **Agent 间消息**：点对点 inbox 与广播，团队成员实时共享进展。

> ClawsomeFlow 在此之上，叠加了 **Openclaw深度适配、DAG 编排调度、失败收敛、人工护栏、Web 产品化与多用户治理** 等能力。

---

## 🤖 支持的 Agent 平台

| Agent | Kind | 运行形态 | 状态 |
|---|---|---|---|
| **OpenClaw** | `openclaw` | TUI | ⭐ 深度适配 |
| **Claude Code** | `claude` | TUI | ✅ 完整支持 |
| **Codex** | `codex` | TUI | ✅ 完整支持 |
| **Gemini CLI** | `gemini` | TUI | Testing |
| **Cursor** | `cursor` | TUI | ✅ 完整支持 |
| **Hermes** | `hermes` | TUI | ✅ 完整支持 |
| **Kimi CLI** | `kimi` | TUI | Testing |
| **Qwen Code** | `qwen` | TUI | Testing |
| **OpenCode** | `opencode` | TUI | Testing |
| **nanobot** | `nanobot` | TUI | Testing |

---

## 🤔 为什么是 ClawsomeFlow？

多 Agent 框架常见的痛点不是「模型能力不足」，而是「协作控制流不稳定」：流程写在 Prompt 里，最终行为取决于 Agent 当下的理解和模型质量，系统的可预测性、成本与恢复能力都不够强。

ClawsomeFlow 的方法很直接：**把协调从自然语言迁回代码，把并发隔离做成默认能力，把失败处理做成流程内建。**

### 🆚 与其他 Agent 编排平台的对比

| 维度 | 其他多 Agent 编排平台 | ✅ ClawsomeFlow |
|---|---|---|
| **任务编排适配** | 多为框架特定，绑定单一生态 | 任务编排 **深度适配 OpenClaw Agents**，同时兼容 Claude / Codex / Cursor 等任意 CLI Agent 同图协同 |
| **并发与隔离** | 并行易竞争，workspace 冲突、上下文串扰 | 解决 OpenClaw 协同不稳定：**多任务并行时 workspace 隔离、可回滚，并彻底解决会话冲突** |
| **控制方式** | 纯 Prompt 自调度（黑盒）或纯代码（笨重） | **AI 与精确编排结合**：自然语言完成全部工作，调度器对行为做精准控制（派发 / 重试 / 超时 / 中止） |
| **工程护栏（Harness）** | 普遍缺失，失败靠 Agent 临场发挥 | **Harness engineering**：人工检查点、结果可回滚、投诉闭环机制、定期熵管理 |
| **失败恢复** | 依赖 Agent 自愈，结果不确定 | retry / skip / abort 明确策略，恢复路径纳入标准状态机 |
| **可观测性** | 上下文多为黑盒 | 全链路 RunEvent 可追踪、可审计、可回放 |

#### ✨ The Result?

**你负责目标，ClawsomeFlow 负责把多 Agent 协同执行做成稳定、可控、可收敛的工程系统。**

---

## 🧩 与 ClawTeam 的关系

ClawsomeFlow 构建在 **ClawTeam**  之上

### 🔍 ClawTeam vs ClawsomeFlow 简要对比

| 维度 | ClawTeam | ClawsomeFlow |
|---|---|---|
| **定位** | 群体智能协议底座（Agent 自组织） | Agent 工作流编排平台 |
| **协作驱动** | Agent 在 Prompt 中自轮询、自调度 | 服务端调度器主动派发，确定性执行 |
| **任务模型** | 看板 + 依赖链 | DAG Flow 编译，Leader 汇总收敛 |
| **OpenClaw 适配** | 作为可选 CLI Agent 支持 | 深度适配，解决会话与 workspace 并发冲突 |
| **失败与护栏** | 基础生命周期协议 | 人工检查点 / 回滚 / 投诉闭环 / 熵管理 |
| **使用形态** | CLI + MCP + 监控面板 | Web UI + CLI，自然语言全流程治理 |

---

## 🚀 快速开始

### 安装

```bash
curl -fsSL https://clawsomeflow.com/install.sh | bash

```

### 常用命令

```bash
# 生命周期
csflow start
csflow stop
csflow status
csflow doctor

# Flow / Run
csflow flows list
csflow runs list
csflow runs start <flow-id> --input k=v
csflow runs abort <run-id>

# Agent 治理
csflow agents list
csflow agents create "用自然语言描述你要的 Agent"
csflow agents chat <agent-id> "继续完善该 Agent 的能力"
```

---

## 🗺️ 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | **Agent Store**——可共享的 Agent、Team 与 Flow 模板市场：一键安装、复用并贡献领域专家。 | 🚧 进行中 |
| **P1** | **支持更多 Agent 平台**——接入更多 CLI Agent 运行时，持续兼容新兴生态，让任意 Agent 同图协作。 | 📅 规划 |
| **P2** | **手机端 & Server 模式**——移动端控制台 + 多用户服务端部署，随时随地监控与干预 Run。 | 💡 探索 |
| **P3** | **云端 Agent & SSH Agent**——通过 SSH 驱动远程 / 云端主机上的 Agent，把协作扩展到单机之外。 | 💡 探索 |

---

## 📄 License

MIT
