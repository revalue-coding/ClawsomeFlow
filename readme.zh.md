<div align="center">

<p>
  <img src="./docs/assets/title.png" alt="ClawsomeFlow" width="960" />
</p>

<p>
  🌐 <a href="https://clawsomeflow.com"><b>clawsomeflow.com</b></a> ·
  📖 <a href="https://clawsomeflow.com/docs/"><b>Docs</b></a>
</p>

<p>
  <a href="./readme.md">English</a> ·
  <b>简体中文</b>
</p>

<p><b>把目标编排成一张任务流程图，由调度器主动驱动一支 AI Agent 团队去执行——并行推进、彼此隔离、全程可观测、稳定收敛地交付。你负责编排，掌控交给 ClawsomeFlow。</b></p>

<p>
  <b>Full compatibility with</b> OpenClaw、Claude Code、Codex、Cursor、Hermes等 CLI Agent。
</p>

<p>
  <a href="#-快速开始">快速开始</a> ·
  <a href="https://clawsomeflow.com/docs/">Docs</a> ·
  <a href="#-news">News</a> ·
  <a href="#-谁最该试试它">谁最该试试它</a> ·
  <a href="#-核心特性">核心特性</a> ·
  <a href="#%EF%B8%8F-工作原理">工作原理</a> ·
  <a href="#-为什么是-clawsomeflow">为什么是 ClawsomeFlow</a> ·
  <a href="#-贡献者本地部署与测试">贡献者开发</a> ·
  <a href="#-路线图">路线图</a> ·
  <a href="#-微信交流群">微信交流群</a>
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

## 🎯 谁最该试试它？

- 想打造一支可在**本地多分支并行开发**的软件工程 Agent 团队；
- 需要让多个 Agent 像真实团队一样分工协作（规划、实现、校验、汇总），而不是只多开几个聊天窗口的开发者与团队；
- 正在构建 **AI Native 企业**、希望把可重复执行的工作系统化交给 Agent 团队的创业者与运营者；
- 想成为“**超级个体**”，以一人之力调度多个专业 Agent 持续放大产出的创造者；
- 受够了 **Prompt 自调度黑盒**、追求可预测、成本可控、可回滚工程流程的实践派。

---

## ✨ 核心特性

ClawsomeFlow 把零散的 AI Agent 变成一套可控的工程系统——从第一条指令，到最终可审阅的交付结果。

| 🗣️ 用自然语言搞定一切 | 🧠 精准编排，而非碰运气 | 🚀 众多 Agent，同一张图 |
|---|---|---|
| 定义 Flow、创建 Agent、编排任务、运行中实时干预——只需描述你想要什么。无需胶水代码，也无需折腾 SDK。 | 控制流写在代码里，而不是塞进 Prompt。调度器负责派发、重试、超时与收敛——行为可预测，Token 不浪费。 | 把工作编排成 DAG，让多个 Agent 并行协作；由 Leader 汇总并将结果收敛为一份交付物。 |

| 🔐 默认隔离与回滚 | 📊 可审计及可观测性 | 🔄 会自我进化的系统 |
|---|---|---|
| 基于 Git worktree 的底层隔离机制，结合内置跨进程仓库锁，确保Agent各类协作行为的绝对可靠性；支持智能合入与回滚，并可内置人工检查点，随时进行行为纠正。 | 每一次 dispatch / completion / failure 都记录为 RunEvent——每次运行都可追溯、可回放、可审阅，绝不是黑盒。 | 对结果不满意？发起一次「投诉」，系统会反思、返工，并把经验写回——让下一次比上一次更好。 |

ClawsomeFlow继承了Clawteam如下特色：

- **Git Worktree 并行隔离底座**：每个 Agent 拥有独立分支与目录，天然适合多 Agent 并行开发
- **Agent 间消息**：点对点 inbox 与广播，团队成员实时共享进展。

> ClawsomeFlow 在此之上，增加了 **AI与精确编排结合、增强Harness工程（内置跨进程仓库锁让多分支并行开发绝对可靠，支持智能合入与回滚，支持投诉机制，并可内置人工检查点，随时进行行为纠正）、Openclaw/Hermes深度适配、Web 产品化** 等能力。

---

## 🛠️ 工作原理

从一句话，到交付成果。目标始终由你掌控；协作、并行，以及出错时的恢复，都交给 ClawsomeFlow。

![ClawsomeFlow 任务编排总体框架图](./docs/assets/flow-orchestration-overview.png)

1. **描述你的目标** —— 用自然语言告诉 ClawsomeFlow 你想要什么，或在画布上把 Flow 编排成任务与依赖关系图。
2. **Agent 并行执行** —— 调度器主动把就绪任务派发给合适的 Agent，每个 Agent 在独立工作区中运行，并被驱动至完成。
3. **观察、干预、恢复** —— 实时跟踪每一步。以清晰的策略重试、跳过或中止，并在人工检查点确认结果后再落地。
4. **收敛并交付** —— 由 Leader 将并行的工作合并为一份经过审阅的交付物，运行记录全程可审计。

---

## 🧪 开发者模式

开发者模式为**软件开发协作项目**提供更灵活的协作方式。

- **每个子任务都拿到上游上下文**:对每个直接依赖,子任务都会被注入上游的 **Agent id、worktree 路径、分支与基线分支**,从而可以灵活地在其成果之上继续——查看、合入该分支、或为其提 PR,完全由你的任务说明驱动。
- **自然语言直接指挥跨分支协作**:在下游任务里只需写一句「把上游 Agent X 的 worktree 分支合入 Y 分支」或「为 X 提交 PR」。
- **内置锁 = 并行合入绝对可靠**:多个分支可以并行开发与合入,永不竞争、永不损坏仓库。无论调度器还是 Agent，每次合入都在同一把锁上串行，你甚至可以用自然语言指挥跨分支合入/ 提PR而毫无风险。
- **逐子任务控制自动合入**:每个子任务可独立设置是否自动合入到基线分支。
- **每个 Agent 拥有唯一 worktree**:Agent 从基线分支创建自己的 worktree 与独立分支。
- **更适合 PR 协作**:对希望走 PR 或手动审核合入的子任务,关闭其自动合入即可。

![Flow 运行时协作架构图（中文）](./docs/assets/flow-runtime-collab-cn.png)

---

## 🤖 支持的 Agent 平台

| Agent | Kind | 运行形态 | 状态 |
|---|---|---|---|
| **OpenClaw** | `openclaw` | TUI | ⭐ 深度适配 |
| **Hermes** | `hermes` | TUI | ⭐ 深度适配 |
| **Claude Code** | `claude` | TUI | ✅ 完整支持 |
| **Codex** | `codex` | TUI | ✅ 完整支持 |
| **Cursor** | `cursor` | TUI | ✅ 完整支持 |
| **OpenCode** | `opencode` | TUI | Testing |
| **Gemini CLI** | `gemini` | TUI | Testing |
| **Kimi CLI** | `kimi` | TUI | Testing |
| **Qwen Code** | `qwen` | TUI | Testing |
| **Qoder CLI** | `qoder` | TUI | Testing |
| **CodeBuddy Code** | `codebuddy` | TUI | Testing |
| **nanobot** | `nanobot` | TUI | Testing |

---

## 🤔 为什么是 ClawsomeFlow？

多 Agent 框架常见的痛点不是「模型能力不足」，而是「协作控制流不稳定」：流程写在 Prompt 里，最终行为取决于 Agent 当下的理解和模型质量，系统的可预测性、成本与恢复能力都不够强。

ClawsomeFlow 的方法很直接：**把协调从自然语言迁回代码，把并发隔离做成默认能力，把失败处理做成流程内建。**

### 🆚 与其他 Agent 编排平台的对比

| 维度 | 其他多 Agent 编排平台 | ✅ ClawsomeFlow |
|---|---|---|
| **任务编排适配** | 多为框架特定，绑定单一生态 | 任务编排 **深度适配 OpenClaw/Hermes Agents**，同时兼容 Claude / Codex / Cursor 等任意 CLI Agent 同图协同 |
| **并发与隔离** | 并行易竞争，workspace 冲突、上下文串扰 | **多任务并行时 workspace 隔离、可回滚，并彻底解决会话冲突**；**内置跨进程仓库锁，让多分支并行开发与合入绝对可靠** |
| **工程护栏（Harness）** | 普遍缺失，失败靠 Agent 临场发挥 | **Harness engineering**：人工检查点、结果可回滚、投诉闭环机制、定期熵管理 |
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
| **协作流程** | 协作流程不可控，更适合一次性任务 | 调度器驱动的确定性工作流，适合可重复、可收敛的工程协作 |
| **OpenClaw 适配** | 作为可选 CLI Agent 支持 | 深度适配，解决会话与 workspace 并发冲突 |
| **多分支并行可靠性** | **无仓库级合入锁**——基线分支并发合入会竞争、可能损坏 git 元数据，完全不可控 | **内置跨进程仓库锁，保证多分支并行开发的绝对可靠性** |
| **失败与护栏** | 基础生命周期协议 | 人工检查点 / 回滚 / 投诉闭环 / 熵管理 |
| **Skill 配置** | 需要在 Agent 平台额外配置 skills | 无需额外配置 skills，开箱即用 |
| **使用形态** | CLI + MCP + 监控面板 | Web UI + CLI，自然语言全流程治理 |

---

## 🚀 快速开始

> **开始之前——请先确保你的 Agent CLI 可正常使用。**
> ClawsomeFlow 通过调用外部 Agent CLI（`claude`、`codex`、`hermes` 等)来运行，
> 且 Agent **会继承你全局 CLI 的认证**。因此请先安装并登录每个你打算使用的 CLI，
> 并确认它们能独立运行（例如 `claude -p hi`、`hermes` 对话、`codex`）。若某个 CLI
> 未完成认证,使用它的 Agent 会卡在登录界面。Hermes 还可在 **设置 → 模型** 中为
> 每个 Agent 单独配置模型/提供商/密钥。遇到认证报错时,请优先排查 CLI 自身的
> 模型/提供商配置是否正确。
>
> **Qoder / CodeBuddy** 需一次性认证:CodeBuddy 运行 `codebuddy` 交互式登录;
> Qoder 设置 `export QODER_PERSONAL_ACCESS_TOKEN=…`(或 `qodercli` → `/login`)。
> ClawsomeFlow 会自动写入二者的目录信任配置(`trustAll` / `trustDirectories`),
> 使无人值守运行不会卡在"是否信任此文件夹"提示——这部分无需手动处理。

### 安装

Linux/macOS
```bash
curl -fsSL https://clawsomeflow.com/install.sh | bash

```

### 常用命令

大多数时候只需要这三个：

```bash
csflow start      # 启动服务，并打印控制台地址
csflow status     # 是否运行中？版本、模式、路径
csflow upgrade    # 升级到最新版本（Flow / Run / 设置都会保留）
```

日常还会用到的一些：

```bash
# 生命周期
csflow stop
csflow doctor                              # 健康检查（依赖 + 配置 + 网关）
csflow uninstall --yes                     # 停止服务并注销 OpenClaw（保留本地数据）
csflow uninstall --purge-data              # 彻底删除数据：需输入 PURGE 确认（不可恢复）

# Flow / Run
csflow flows list
csflow runs start <flow-id> --input k=v    # 带参数字段触发一次 Run
csflow runs list
csflow runs abort <run-id>

# Agent 治理
csflow agents list
# Agent 的创建与对话请在 Web UI（“我的团队”）中完成，不通过 CLI。
```

每个命令都支持 `--help`。完整 CLI 文档：<https://clawsomeflow.com/docs/>

---

## 👩‍💻 贡献者本地部署与测试

如果你是贡献者，需要在改源码后做本地部署验证，推荐使用隔离入口：

```bash
bash scripts/deploy-contributor.sh
```

`deploy-contributor.sh` 脚本默认行为：

- 使用隔离数据目录和运行时：`~/.clawsomeflow-dev`（不复用 `~/.clawsomeflow`）。
- 后端端口默认为 `17117`，Vite 端口默认为 `5174`。
- 默认将 ClawTeam 运行时隔离到 `~/.clawsomeflow-dev/.clawteam-data`。

日常贡献开发建议优先 `bash scripts/deploy-contributor.sh`，将测试环境与常规服务状态隔离。

自定义 profile / 端口示例：

```bash
CSFLOW_DEV_HOME=~/.clawsomeflow-dev-alice \
CSFLOW_DEV_BACKEND_PORT=18117 \
CSFLOW_DEV_FRONTEND_PORT=5184 \
bash scripts/deploy-contributor.sh
```

### 运行测试

所有测试都在 Docker 容器中运行——独立的文件系统与网络命名空间,保证测试**永远不会**碰到你真实的 `~/.clawsomeflow` / `~/.openclaw` 或正在运行的网关:

```bash
scripts/test-in-docker.sh                                       # 全量后端测试
scripts/test-in-docker.sh -q backend/tests/test_api_guard.py   # 子集(参数透传给 pytest)
```

需要 Docker 和本地 ClawTeam 源码(同级目录 `../ClawTeam`,或设 `CLAWTEAM_SRC=/path/to/ClawTeam`)。**不要**在跑着 csflow/openclaw 服务的机器上直接 `pytest`——那会连到真实网关 `:18789`。发版门禁(`scripts/release.sh`)也以同样方式跑测试。

### 停止贡献者服务

停止 `deploy-contributor.sh` 启动的贡献者环境，请使用专用停止脚本：

```bash
bash scripts/stop-contributor.sh
```
请**不要**用 `csflow stop` 停止贡献者环境——那是用来停止正式用户服务的。
若你使用了自定义 profile，请传入相同的环境变量：

```bash
CSFLOW_DEV_BACKEND_PORT=18117 CSFLOW_DEV_FRONTEND_PORT=5184 \
bash scripts/stop-contributor.sh
```

---

## 🗺️ 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | **Agent Store**——可共享的 Agent、Team 与 Flow 模板市场：一键安装、复用并贡献领域专家。 | 🚧 进行中 |
| **P1** | **支持更多 Agent 平台**——接入更多 CLI Agent 运行时，持续兼容新兴生态，让任意 Agent 同图协作。 | 🚧 进行中 |
| **P2** | **手机端 & Server 模式**——移动端控制台 + 多用户服务端部署，随时随地监控与干预 Run。 | 💡 探索 |
| **P3** | **云端 Agent & SSH Agent**——通过 SSH 驱动远程 / 云端主机上的 Agent，把协作扩展到单机之外。 | 💡 探索 |

---

## 🙏 致谢

- **[ClawTeam]** —— 给了我们灵感的火花。感谢它展示了 Agent 自组织的可能。
- **各个 Agent 平台「团队成员」** —— 它们才是每个 Flow 里真正干活的「队员」：**Claude**、**OpenClaw**、**Codex**、**Gemini** 以及不断壮大的 CLI Agent 阵容。ClawsomeFlow 的精彩，源自它所协调的这些 Agent。

---

## 💬 微信交流群

如果 ClawsomeFlow 帮你协调好了 Agent 团队的工作，**请给我们一个 ⭐ Star** —— 这是支撑我们继续走下去的动力。

对 ClawsomeFlow 的使用有疑问，或者对成立 **OPC（一人公司）** 感兴趣？欢迎来和我们一起交流 —— 扫描下方二维码加入微信讨论社群：

<p align="center">
  <img src="./docs/assets/wechat-group-qr.png?v=2" alt="ClawsomeFlow 微信交流群" width="240" />
</p>

---

## 📄 License

MIT
