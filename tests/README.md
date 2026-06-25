# ClawsomeFlow 测试说明

**测试一律在 Docker 容器内运行。** 容器有独立的文件系统与网络命名空间,
不挂载宿主目录、连不到宿主的网关——因此任何测试都**不可能**碰到你真实的
`~/.clawsomeflow` / `~/.openclaw` / `~/.clawteam` 或正在运行的 `csflow` 服务。

> ⚠️ 不要在跑着 `csflow`/`openclaw` 服务的机器上直接 `pytest`:它会连到真实
> 网关 `:18789`、并可能改动真实数据。请始终用下面的 Docker 入口。

## 运行测试

```bash
scripts/test-in-docker.sh                                       # 全量后端套件(L1,默认入口)
scripts/test-in-docker.sh -q backend/tests/test_api_guard.py   # 子集(参数原样透传给 pytest)
SKIP_BUILD=1 scripts/test-in-docker.sh                         # 复用已构建镜像,跳过重建
```

依赖与机制:

- 需要 **Docker**;以及本地 **ClawTeam 源码**(默认取仓库同级目录 `../ClawTeam`,
  或设 `CLAWTEAM_SRC=/path/to/ClawTeam`——`clawteam` 未发布到 PyPI)。
- 镜像内含:真实 **openclaw**(npm)+ 真实 **clawteam**(本地源码)+ `git` + `tmux`,
  以及 `claude`/`codex`/`hermes` 等**免认证假 CLI**——凡需登录/认证才能拿到正确
  反馈的地方(LLM 回合等)由假 CLI 返回固定回复闭环。
- 每次运行会把**当前工作树只读复制**进镜像(重型依赖层走缓存),改完代码直接重跑
  即可,无需手动同步;构建是只读复制,**绝不回写源码**。
- 与本地分支/发版互不影响:容器跑的是某一刻的源码快照,跑测试期间切分支、改源码、
  发版都不影响运行中的容器,容器也绝不改你的工作树(只需避免在开头几秒的 rsync 暂存
  期切分支)。

## 目录约定

- `backend/tests/`:**L1** 快速单元/集成测试(进程内 TestClient),容器默认入口跑的就是这批(~1300+ 用例)。
- `tests/runtime/`:**L2** 部署后服务运行态验证(service、start/upgrade/restart/recovery)。
- `tests/e2e/`:**L2** 跨组件关键链路 smoke(Flow/Run/events/WS/OpenClaw 回调)。
- `tests/perf/`:**L3** 性能基线与回归。
- `tests/common/`:共享 fixture 与工具。

## 隔离原理(为何不再需要逐条「宿主级隔离红线」)

容器边界本身就是隔离层:不挂载宿主目录、独立网络命名空间。过去那一长串「独立
`CSFLOW_HOME`/`CLAWTEAM_DATA_DIR`/`OPENCLAW_HOME`、独立 service 名、端口回收避让生产、
运行前后比对生产 `MainPID`」红线,在 Docker 路径下**天然满足**,无需逐条人工保证。

仍保留的两道标准防护:

- `backend/tests/conftest.py` 为**每条用例**分配独立临时 `CSFLOW_HOME`(标准用例隔离,
  保证用例间互不干扰、可并行)。
- 代码侧硬闸 `app.cli._user_service._guard_service_namespace`:当 `CSFLOW_HOME` 不指向
  生产路径时,拒绝对默认 `csflow` 服务执行 stop/restart——即便有人绕过 Docker 直接跑,
  也挡住误杀生产服务。

## 发版门禁

维护者的发版流程在「全量测试」环节调用 `scripts/test-in-docker.sh`(同一个 Docker 入口),
因此发版前跑的也是隔离套件,不会触碰真实部署。

## L2/L3(runtime / e2e / perf)与 CI

- **L2/L3** 用例需要拉起**真实部署服务**,默认被 `CSFLOW_*_TEST_ACTIVE` 门控**跳过**;
  容器默认入口(`backend/tests`)不包含它们。需要时显式传路径并设置对应激活标志,
  在容器内拉起隔离的服务实例运行。
- `tests/conftest.py` 在 `runtime`/`e2e`/`perf` marker 下会强制校验隔离命名空间;
  `tests/e2e/conftest.py` 通过 `E2EResourceTracker` 在每条用例后 best-effort 回收 Flow/Run/Agent。
- **CI**:GitHub Actions 在干净的临时 runner 上执行(runner 本身即隔离环境,不存在被测试
  污染的真实服务)。
