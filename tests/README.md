# ClawsomeFlow 仓库级测试模块

本目录用于补齐 **部署后运行态** 与 **性能基线** 测试，不替代 `backend/tests` 的快速单元/集成测试。

## 目录约定

- `tests/runtime/`：真实部署后的服务运行态验证（service、start/upgrade/restart/recovery）
- `tests/e2e/`：跨组件关键链路 smoke（Flow/Run/events/WS/OpenClaw internal 回调）
- `tests/perf/`：性能基线与回归测试
- `tests/common/`：共享 fixture 与工具函数

## 执行入口

- 快速门禁：`./scripts/test-fast.sh`（本地首次可用 `--install` 自动执行前端 `npm ci`）
- 运行态门禁：`./scripts/test-runtime.sh`
- 性能门禁：`./scripts/test-perf.sh`

## 环境隔离红线

- 发版前 L2/L3 门禁仍以生产 `17017` 服务为健康基线（`CSFLOW_PROD_HEALTH_URL`），但**所有会写入数据的用例必须打在隔离测试端口**（默认 `27117` / `28117`），禁止直接对生产端口做 Flow/Run/Agent 变更。
- 运行态与性能测试必须使用独立 `CSFLOW_HOME` 与独立 systemd user service 名。
- 运行态与性能测试必须使用独立 `CLAWTEAM_DATA_DIR`（禁止触碰 `~/.clawteam`）。
- 运行态与性能测试必须使用独立 `OPENCLAW_HOME`，且 `csflow init` 使用 `--skip-openclaw`。
- 运行态与性能测试必须设置 `CSFLOW_DISABLE_BOARD=1`，避免拉起测试侧 `clawteam board` 子进程。
- 运行态与性能测试必须使用测试专属虚拟环境（`~/.clawsomeflow-test/<id>/.venv`），禁止复用生产 Python 用户环境。
- 运行态与性能测试的 venv 必须具备 `clawteam runtime` 能力（可通过 `CSFLOW_TEST_CLAWTEAM_SOURCE` 指定源码路径/包规格）。
- 生产服务名 `csflow` 不得被测试流程重启、停用或覆写。
- 测试结束必须清理测试 unit、测试目录和临时端口占用。
- `tests/conftest.py` 会在 `runtime` / `e2e` / `perf` marker 下自动校验隔离路径；`tests/e2e/conftest.py` 通过 `E2EResourceTracker` 在每条用例结束后 best-effort 回收 Flow/Run/Agent。
- `scripts/_test_isolation_guards.sh` 在门禁脚本收尾阶段会：清扫隔离命名空间内残留的 `e2e-*` 资源，并扫描生产 `17017` 是否被测试数据污染（发现即失败）。
- **禁止误杀生产服务**：`app.cli._user_service` 现在有两道硬闸：
  1. `CSFLOW_HOME` 位于 `~/.clawsomeflow-test/` 时，禁止对默认生产 unit 名 `csflow` 执行 stop/restart；
  2. 端口回收逻辑会跳过 `csflow serve` 托管链路（含其 uvicorn 子进程），只清理手动 `uvicorn app.main:app` 残留（如 `deploy.sh source`）。
- `test-runtime.sh` 会在测试前后比对生产 `csflow` 的 systemd `MainPID`，若被重启则直接失败。
