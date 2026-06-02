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

- 运行态与性能测试必须使用独立 `CSFLOW_HOME` 与独立 systemd user service 名。
- 运行态与性能测试必须使用独立 `CLAWTEAM_DATA_DIR`（禁止触碰 `~/.clawteam`）。
- 运行态与性能测试必须使用独立 `OPENCLAW_HOME`，且 `csflow init` 使用 `--skip-openclaw`。
- 运行态与性能测试必须设置 `CSFLOW_DISABLE_BOARD=1`，避免拉起测试侧 `clawteam board` 子进程。
- 运行态与性能测试必须使用测试专属虚拟环境（`~/.clawsomeflow-test/<id>/.venv`），禁止复用生产 Python 用户环境。
- 运行态与性能测试的 venv 必须具备 `clawteam runtime` 能力（可通过 `CSFLOW_TEST_CLAWTEAM_SOURCE` 指定源码路径/包规格）。
- 生产服务名 `csflow` 不得被测试流程重启、停用或覆写。
- 测试结束必须清理测试 unit、测试目录和临时端口占用。
