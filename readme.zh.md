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

<p><b>面向长周期、多方协作的 Harness——人、机器与任意 Agent 平台，在同一条可控流程里协同。</b></p>

<p>真实业务从来不是「纯 AI」：总有人要对接物理世界、审批不可逆操作、或做关键决策；协作也往往跨机器、跨工具，而不是困在一台电脑或一次对话里。ClawsomeFlow 把 <b>真人、远程 ClawsomeFlow、自定义 Webhook、以及各类 CLI Agent</b> 都当作同一张 DAG 里的一等执行节点：可复用、可观测、可检查点、成本可控。</p>

<p>
  兼容 <b>OpenClaw、Hermes、Claude Code、Codex、Cursor</b> 等 CLI Agent，也兼容你已有的系统——只需一份简洁的 webhook 约定。
</p>

<p>
  <a href="#-快速开始">快速开始</a> ·
  <a href="https://clawsomeflow.com/docs/">Docs</a> ·
  <a href="#-news">News</a> ·
  <a href="#-谁最该试试它">谁最该试试它</a> ·
  <a href="#-核心特性">核心特性</a> ·
  <a href="#-外部执行节点">外部执行节点</a> ·
  <a href="#%EF%B8%8F-工作原理">工作原理</a> ·
  <a href="#-为什么是-clawsomeflow">为什么是 ClawsomeFlow</a> ·
  <a href="#-贡献者本地部署与测试">贡献者开发</a> ·
  <a href="#-路线图">路线图</a> ·
  <a href="#-交流社区">交流社区</a>
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

- **2026-06-02**：ClawsomeFlow 公开发布 🎉

---

## 🎯 谁最该试试它？

- 正在建设 **AI Native 业务**、工作必然跨人、跨工具、跨机器，而不是只停在写代码沙盒里的运营者与建设者；
- 流程里需要 **人工检查点**、不可逆审批，或必须对接真实物理世界的团队；
- 希望 **OpenClaw / Hermes / Claude / Codex / Cursor**（以及自研 Agent 栈）在同一张图里协作的人；
- 受够了用**一次超长对话**扛复杂长周期项目、希望流程可复用、可审计、成本可控的实践派。

---

## ✨ 核心特性

ClawsomeFlow 是一套 **Harness**：让长周期、多执行方的协作保持稳定——能力可以变强，流程却不会坍成一份无法审阅的聊天记录。

| 🤝 人机协同 · 跨机器 · 跨 Agent 平台 | 🔗 跨机器协同 | 🧩 接入你自己的执行方 |
|---|---|---|
| 真人、本地 Agent、远程 ClawsomeFlow、自定义系统，共享**同一张 DAG**与相同的依赖 / 完成语义。 | 把子任务委派到另一台机器上的 Flow，结果回注为上游上下文。 | 极简 webhook 约定——或你自研的 Agent 平台——无需改调度器即可接入。 |

| ♻️ 可复用、可稳定产出 | 🎛️ 过程可控 | 💸 成本可控 |
|---|---|---|
| 一次定义 Flow，参数化复跑；结构稳定、结果可预期，而不是一次性 Prompt。 | 人工检查点、子任务重跑、投诉改进闭环。你掌舵，Harness 记住状态。 | 工作拆到多节点，每个执行方只看短上下文——比单 Agent 扛全项目更省 token。 |

| ⏪ 可回滚与仓库安全 | 👁 全程可观测 | 🌱 可自我提升 |
|---|---|---|
| Worktree 隔离 + **内置跨进程仓库锁**，并行修改不踩坏基线；合并与结果可审可回退。 | 每次派发、交接与失败都是 RunEvent——看板、依赖边上的 inbox 消息、完整回放。 | 不满意就投诉；系统返工并把教训写回，下次更好。 |

| ⏳ 支撑长周期 | 🏢 不止 coding |
|---|---|
| Flow 可跨小时到数周：等人、等远程任务、跨专业交接而不丢线。 | 市场、运营、内容、客服、研发——能回报结果的专业，都能成为节点。 |

## 🛠️ 工作原理

从一句话，到交付成果。目标始终由你掌控；协作、并行，以及出错时的恢复，都交给 ClawsomeFlow。

![ClawsomeFlow 任务编排总体框架图](./docs/assets/flow-orchestration-overview.png)

1. **描述目标** — 把 Flow 排成图：本地 Agent、真人、webhook、远程 ClawsomeFlow 按需入座。
2. **各方执行自己的部分** — Harness 派发就绪节点（可并行），隔离 Agent 工作区，并在等人/等远程时不断线。
3. **观察、干预、恢复** — 实时看板（依赖边上可看 inbox 交接）、检查点、子任务重跑、重试/跳过/中止。
4. **汇总交付** — leader 收敛为可审阅结果；历史可审计、可改进。

---

## 🧪 开发者模式

开发者模式为**软件开发协作项目**提供更灵活的协作方式。

- **每个子任务都拿到上游上下文**：对每个直接依赖，子任务都会被注入上游的 **Agent id、worktree 路径、分支与基线分支**，从而可以灵活地在其成果之上继续——查看、合入该分支、或为其提 PR，完全由你的任务说明驱动。
- **自然语言直接指挥跨分支协作**：在下游任务里只需写一句「把上游 Agent X 的 worktree 分支合入 Y 分支」或「为 X 提交 PR」。
- **内置锁 = 并行合入绝对可靠**：多个分支可以并行开发与合入，永不竞争、永不损坏仓库。无论调度器还是 Agent，每次合入都在同一把锁上串行，你甚至可以用自然语言指挥跨分支合入 / 提 PR 而毫无风险。
- **逐子任务控制自动合入**：每个子任务可独立设置是否自动合入到基线分支。
- **每个 Agent 拥有唯一 worktree**：Agent 从基线分支创建自己的 worktree 与独立分支。
- **更适合 PR 协作**：对希望走 PR 或手动审核合入的子任务，关闭其自动合入即可。

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
| **OpenCode** | `opencode` | TUI | 🧪 测试中 |
| **Gemini CLI** | `gemini` | TUI | 🧪 测试中 |
| **Kimi CLI** | `kimi` | TUI | 🧪 测试中 |
| **Qwen Code** | `qwen` | TUI | 🧪 测试中 |
| **Qoder CLI** | `qoder` | TUI | 🧪 测试中 |
| **CodeBuddy Code** | `codebuddy` | TUI | 🧪 测试中 |
| **Pi** | `pi` | TUI | 🧪 测试中 |
| **nanobot** | `nanobot` | TUI | 🧪 测试中 |
| **外部执行人** | `external` | 真人 / Webhook / 远端 ClawsomeFlow | ✅ 完整支持 |

---

## 🌐 外部执行节点

真实项目总会走出「纯 AI」：有人要检查硬件、审批付款或在模糊处拍板；另一套系统要跑黑盒步骤；另一台机器上已有合适的 Flow。**外部执行节点**让这些执行方成为 DAG 的一等公民——不启本地进程、不建 worktree，却与 Agent 节点共享相同的依赖解阻与上游摘要语义。

三种 Owner 类型（Flow 编辑 → Owner 来源 → **外部执行**）：

| Owner 类型 | 谁来执行 | 结果如何回来 |
|---|---|---|
| **人工** | Run 页上的真人（可选聊天通知） | 在待办卡片上提交结果 |
| **远程ClawsomeFlow** | 另一台 ClawsomeFlow 上的某条 Flow | 对端无人值守执行，leader 报告回调 |
| **通用接口（webhook）** | 你自有的任意 HTTP 服务 | POST 任务包 → 你的系统回调回执 |

在 Flow 编辑页可直接看到并复制 **Flow ID**——配置「远程ClawsomeFlow」节点时对端需要它。

### 远程ClawsomeFlow — 两边各配什么

**受理方（peer，被委派的机器）：**

```bash
csflow external pair-token peer-a          # 入站凭证（只打印一次）
csflow external expose on                  # 仅放开 /api/external 的非本机访问
# 把密钥、本机可达地址、目标 Flow ID 发给发起方
```

**发起方（origin，委派出去的机器）：**

```bash
csflow external add-remote peer-a <peer 给的密钥>
csflow external callback-url http://<origin-host>:17017   # peer 必须能回调到的绝对地址
```

在编辑器中选择 **远程ClawsomeFlow**，填写对端地址、Flow ID、凭证名称（`peer-a`）。节点运行时 origin 调用 `/api/external/delegate`；peer 结束后回调 origin 的一次性回执 URL。

### 通用接口（webhook）— 两边各配什么

**ClawsomeFlow（origin）：** 编辑器选 **通用接口（webhook）**，填你的端点 URL。按需：

```bash
csflow external callback-url http://<origin-host>:17017
csflow external expose on    # 仅当外部系统需从外网访问 /api/external 时
```

**你的系统：** 接收 `POST` JSON（`schemaVersion: 1`，`event: external_task_dispatch`），内含任务说明、`upstreamOutputs` 与自描述的 `callback`。完成后：

```http
POST {callbackUrl}
Authorization: Bearer {callbackToken}
Content-Type: application/json

{"status": "success", "summary": "交付说明（可带链接/引用）"}
```

整个对接就是两次 JSON。未知字段应忽略；后续可加字段而不必立刻升 `schemaVersion`。

协作面只暴露 `/api/external/*`；主 API 仍仅本机；每次回执都是一次性、单任务票据。

## 🤔 为什么是 ClawsomeFlow？

多智能体落地的难点，很少是「模型不够聪明」，而是**协作没有 Harness**：流程写在 Prompt 里、上下文膨胀、人与其它机器插不进来，长项目变得无法审阅。

ClawsomeFlow 的判断很直接：把协调放进**持久的 Harness**——开放的执行方（人 / 远程 / webhook / 任意 Agent CLI）、短上下文节点、检查点、仓库锁、可观测与可复用——让能力变强时流程不散架。

| | 典型「单 Agent + 一次对话」 | ClawsomeFlow |
|---|---|---|
| **谁能执行** | 基本是眼前的模型 | 人、远程实例、webhook、多 Agent 平台 |
| **长项目** | 上下文腐烂，难安全暂停/续跑 | 为长周期 + 等人 + 重跑而设计的 Flow |
| **成本** | 一个上下文扛一切 | 拆节点 → 更短上下文，更省 token |
| **可控性** | 指望 Prompt 扛住 | 检查点、子任务重跑、投诉闭环、可回滚 |
| **并发** | 容易互相踩仓库 | Worktree 隔离 + 内置仓库锁 |
| **范围** | 偏 coding 演示 | 跨专业、端到端的业务流 |

**目标归你。多方执行的掌控，交给 ClawsomeFlow。**

## 🚀 快速开始

> **开始之前——请先确保你的 Agent CLI 可正常使用。**
> ClawsomeFlow 通过调用外部 Agent CLI（`claude`、`codex`、`hermes` 等）来运行，
> 且 Agent **会继承你全局 CLI 的认证**。因此请先安装并登录每个你打算使用的 CLI，
> 并确认它们能独立运行（例如 `claude -p hi`、`hermes` 对话、`codex`）。若某个 CLI
> 未完成认证，使用它的 Agent 会卡在登录界面。Hermes 还可在 **设置 → 模型** 中为
> 每个 Agent 单独配置模型 / 提供商 / 密钥。遇到认证报错时，请优先排查 CLI 自身的
> 模型 / 提供商配置是否正确。
>
> **Qoder / CodeBuddy** 需一次性认证：CodeBuddy 运行 `codebuddy` 交互式登录；
> Qoder 设置 `export QODER_PERSONAL_ACCESS_TOKEN=…`（或 `qodercli` → `/login`）。
> ClawsomeFlow 会自动写入二者的目录信任配置（`trustAll` / `trustDirectories`），
> 使无人值守运行不会卡在「是否信任此文件夹」提示——这部分无需手动处理。
>
> **Pi**（`npm i -g @earendil-works/pi-coding-agent`）需先配置一个模型提供商：
> 运行 `pi` 后用 `/login`，或设置 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 等环境变量。
> Pi 默认自动执行工具（无放权弹窗），spawn 时 ClawsomeFlow 追加 `-a`（信任项目本地文件），
> 因此携带 `.pi/` 扩展的仓库也不会卡在信任提示。


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
```

每个命令都支持 `--help`。完整 CLI 文档：<https://clawsomeflow.com/docs/>

---

## 🔌 MCP：远程驱动 Flow

ClawsomeFlow配置为 **MCP server** 运行。把你的某个 Agent 接上它，就能通过该 Agent 自带的渠道（飞书、Telegram 等）用自然语言询问有哪些 Flow、执行某个 Flow，并拿回 **leader 的最终工作汇报**。典型闭环：*你在 Telegram 发一个文件 + 一句需求 → Agent 选中合适的 Flow 并执行 → 读取 leader 汇报 → 通过 Telegram 回复你*。

### 与 Agent 对话（示例）

- “当前有哪些可用的 ClawsomeFlow 执行流？”
- “执行XXXX任务”
- “给我看看运行结果。”
- “停掉XXX任务。”

Agent 会从你的需求里自行组织参数，因此你很少需要显式点名字段——描述任务即可，它会把你的话映射到 Flow 的参数上。

### 注册到 Agent

```bash
csflow mcp install --platform hermes                   # Hermes：默认 profile
csflow mcp install --platform hermes --agent <id>      # Hermes：指定某个 agent profile
csflow mcp install --platform codex                    # 全局平台（见下表）
csflow mcp uninstall --platform codex                  # 移除
```

| 平台 | 范围 | 写入位置 |
|---|---|---|
| `hermes` | 按 agent（`--agent <id>`；省略 → **默认 profile**） | `~/.hermes/…/config.yaml` 的 `mcp_servers` |
| `claude` | 全局 | `~/.claude.json` 的 `mcpServers` |
| `cursor` | 全局 | `~/.cursor/mcp.json` 的 `mcpServers` |
| `gemini` | 全局 | `~/.gemini/settings.json` 的 `mcpServers` |
| `codex` | 全局 | `~/.codex/config.toml` 的 `[mcp_servers.*]` |
| `opencode` | 全局 | `~/.config/opencode/opencode.json` 的 `mcp` |
| `openclaw`、`kimi`、`qwen`、`nanobot` | 手动 | 仅 print-config（粘贴到平台自己的 MCP 配置） |


### 手动配置

也可为任意平台打印可直接粘贴的片段，而不写文件：

```bash
csflow mcp print-config --platform claude     # JSON：{ "mcpServers": { "clawsomeflow": … } }
csflow mcp print-config --platform codex      # TOML：[mcp_servers.clawsomeflow]
csflow mcp print-config --platform hermes     # YAML：mcp_servers: …
```

服务器条目始终是同一条命令——若你的平台不在上表中，手动注册它即可：

```json
{
  "mcpServers": {
    "clawsomeflow": { "command": "csflow", "args": ["mcp", "serve"] }
  }
}
```

注意，ClawsomeFlow服务必须处于运行状态（`csflow start`）才能正常使用MCP。

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

所有测试都在 Docker 容器中运行——独立的文件系统与网络命名空间，保证测试**永远不会**碰到你真实的 `~/.clawsomeflow` / `~/.openclaw` 或正在运行的网关：

```bash
scripts/test-in-docker.sh                                       # 全量后端测试
scripts/test-in-docker.sh -q backend/tests/test_api_guard.py   # 子集（参数透传给 pytest）
```

需要 Docker 和本地 ClawTeam 源码（同级目录 `../ClawTeam`，或设 `CLAWTEAM_SRC=/path/to/ClawTeam`）。**不要**在跑着 csflow/openclaw 服务的机器上直接 `pytest`——那会连到真实网关 `:18789`。

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
| **P2** | **手机端**——移动端控制台，随时随地监控与干预 Run。 | 💡 探索 |
| **P3** | **云端 Agent & SSH Agent**——通过 SSH 驱动远程 / 云端主机上的 Agent，把协作扩展到单机之外。 | 💡 探索 |

---

## 🙏 致谢

- **[ClawTeam]** —— 给了我们灵感的火花。感谢它展示了 Agent 自组织的可能。
- **各个 Agent 平台「团队成员」** —— 它们才是每个 Flow 里真正干活的「队员」：**Claude**、**OpenClaw**、**Codex**、**Gemini** 以及不断壮大的 CLI Agent 阵容。ClawsomeFlow 的精彩，源自它所协调的这些 Agent。

---

## 💬 交流社区

如果 ClawsomeFlow 帮你协调好了 Agent 团队的工作，**请给我们一个 ⭐ Star** —— 这是支撑我们继续走下去的动力。

对 ClawsomeFlow 的使用有疑问，或者对成立 **OPC（一人公司）** 感兴趣？欢迎来和我们一起交流 —— 加入 Discord 社区，或扫描下方二维码加入微信讨论社群：

<p align="center">
  <a href="https://discord.gg/hcpMwXnrkM"><img alt="Discord" src="https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white"></a>
</p>

<p align="center">
  <img src="./docs/assets/wechat-group-qr.png?v=4" alt="ClawsomeFlow 微信交流群" width="240" />
</p>

---

## 📄 License

MIT
