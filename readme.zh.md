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

<p>真实业务的复杂性往往不能只靠一个agent解决，总有人要对接物理世界、审批不可逆操作、或做关键决策，真实的协作也往往跨机器、跨工具，而不是困在一台电脑、一份skill或一次对话里。ClawsomeFlow彻底打破本地agent协作的局限，把协作扩展到远程，扩展到泛agent工具， <b>各类CLI Agent、真人、远程ClawsomeFlow、自定义agent工具，甚至任意一个可以称之为“执行单元”的概念</b> 都能放进一张协作网中，结合Harness工程，实现复杂、长周期项目的有效协同。</p>

<p>ClawsomeFlow并不打算替代你原有的agent或流程，只会让它更具扩展性！</p>

<p>
  <a href="#-快速开始">快速开始</a> ·
  <a href="https://clawsomeflow.com/docs/">Docs</a> ·
  <a href="#-news">News</a> ·
  <a href="#-谁最该试试它">谁最该试试它</a> ·
  <a href="#-为什么是-clawsomeflow">为什么是ClawsomeFlow</a> ·
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

- **2026-07**：ClawsomeFlow 0.2.0发布，彻底打破本地agent协作的局限，把各路执行方放进一张协作网中
- **2026-06**：ClawsomeFlow 0.1.0公开发布 🎉

---

## 🎯 谁最该试试它？

- 正在建设 **AI Native 业务**，工作需要跨人、跨工具、跨机器，而不是只停在写代码沙盒里的运营者与建设者；
- 受够了用**一次超长对话**扛复杂长周期项目，希望流程可复用、可审计、可局部重跑、成本可控、可稳定产出的实践派。

---


## 🤔 为什么是 ClawsomeFlow？

多智能体落地的难点，很少是「模型不够聪明」，而是**协作没有 Harness**：流程写在 Prompt 里、上下文膨胀、人与其它机器插不进来，长项目变得无法审阅。

ClawsomeFlow 的判断很直接：把协调放进**持久的 Harness**——开放的执行方（人 / 远程执行单元 / webhook / Agent CLI）、短上下文节点、检查点、仓库锁、可观测与可复用——让能力变强时流程不散架。

| | 典型「单 Agent + 一次对话」 | ClawsomeFlow |
|---|---|---|
| **谁能执行** | 基本是眼前的模型 | 人、远程实例、webhook、多 Agent 平台 |
| **长项目** | 上下文腐烂，难安全暂停/续跑 | 为长周期 + 等人 + 重跑而设计的 Flow |
| **成本** | 一个上下文扛一切 | 拆节点 → 更短上下文，更省 token |
| **可控性** | 指望 Prompt 扛住 | 检查点、子任务可审查可局部重跑、投诉闭环、可回滚 |
| **并发** | 容易有冲突 | Worktree 隔离 + 内置仓库锁 |
| **范围** | 偏 coding 演示 | 跨专业、端到端的业务流 |

---


## ✨ 核心特性

**🤝 融合多路执行方** —— 把协作扩展到远程、扩展到泛 agent 工具。真人、各类 CLI Agent、远程 ClawsomeFlow、自定义 agent 工具，共享**同一张 DAG** 与相同的依赖/完成语义；子任务可委派到另一台机器，可接入你自研的执行方，只需极简 webhook 约定，无需改调度器。

**🧩 Harness 工程** —— 让长周期、多执行方的协作保持稳定，能力变强而流程不散架：拆节点 → 更短上下文、更省 token；人工检查点、子任务局部重跑、投诉改进闭环；Worktree 隔离 + **内置跨进程仓库锁**，并行修改不踩坏基线；每次派发 / 交接 / 失败都是可回放的 RunEvent；Flow 一次定义、参数化复跑，可跨小时到数周稳定产出。

---

## 🌐 外部执行节点

**外部执行节点**彻底打破了本地agent协作的局限，把各路执行方放进一张协作网中

| 执行节点类型 | 谁来执行 | 
|---|---|
| **人工** | 真人 | 
| **远程ClawsomeFlow** | 另一台机器上的ClawsomeFlow | 
| **通用接口** | 泛agent执行单元 | 


---

## 🚀 快速开始

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
> PS：**如果你的流程中需要加入Agent CLI执行节点——请先确保本地Agent CLI 可正常使用。**


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

## 🤖 本地支持的 Agent 平台

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

---

## 🗺️ 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P1** | **支持更多Agent协作方式**——把协作扩展到单机之外，持续兼容新兴生态，让任意形式的Agent同图协作。 | 🚧 进行中 |
| **P2** | **手机端**——移动端控制台，随时随地监控与干预 Run。 | 💡 探索 |

---


## 💬 交流社区

如果 ClawsomeFlow 帮你协调好了团队的工作，**请给我们一个 ⭐ Star** —— 这是支撑我们继续走下去的动力。

对 ClawsomeFlow 的使用有疑问，或者对成立 **OPC（一人公司）** 感兴趣？欢迎来和我们一起交流 —— 加入 Discord 社区，或扫描下方二维码加入微信讨论社群：

<p align="center">
  <a href="https://discord.gg/hcpMwXnrkM"><img alt="Discord" src="https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white"></a>
</p>

<p align="center">
  <img src="./docs/assets/wechat-group-qr.png?v=5" alt="ClawsomeFlow 微信交流群" width="240" />
</p>

---

## 📄 License

MIT
