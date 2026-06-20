#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  add-heartbeat-task.sh \
    --workspace <agent_workspace> \
    --task-id <task_id> \
    --title <task_title> \
    [--output <path>]...

Description:
  Add one checklist item into <workspace>/HEARTBEAT.md.
  If HEARTBEAT.md has no `tasks:` section, it creates one.
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 127
  fi
}

WORKSPACE=""
TASK_ID=""
TITLE=""
DEPRECATED_SCHEDULE=""
declare -a OUTPUTS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)
      WORKSPACE="$2"
      shift 2
      ;;
    --task-id)
      TASK_ID="$2"
      shift 2
      ;;
    --title)
      TITLE="$2"
      shift 2
      ;;
    --schedule)
      # Deprecated: HEARTBEAT checklist is not cron configuration.
      DEPRECATED_SCHEDULE="$2"
      shift 2
      ;;
    --output)
      OUTPUTS+=("$2")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$WORKSPACE" || -z "$TASK_ID" || -z "$TITLE" ]]; then
  echo "required arguments missing" >&2
  usage
  exit 2
fi

if [[ -n "$DEPRECATED_SCHEDULE" ]]; then
  echo "warning: --schedule is deprecated and ignored; HEARTBEAT.md is not cron config" >&2
fi

require_cmd python3

python3 - "$WORKSPACE" "$TASK_ID" "$TITLE" "${OUTPUTS[@]}" <<'PY'
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

workspace = Path(sys.argv[1]).expanduser().resolve()
task_id = sys.argv[2].strip()
title = sys.argv[3].strip()
outputs = [x for x in sys.argv[4:] if x.strip()]

if not workspace.exists() or not workspace.is_dir():
    raise SystemExit(f"workspace does not exist: {workspace}")

heartbeat_md = workspace / "HEARTBEAT.md"
if heartbeat_md.exists():
    text = heartbeat_md.read_text(encoding="utf-8")
else:
    text = "# HEARTBEAT.md\n\n"

if re.search(rf"(?m)^\s*-\s*id:\s*{re.escape(task_id)}\s*$", text):
    raise SystemExit(f"task id already exists: {task_id}")

block_lines = [
    f"  - id: {task_id}",
    f"    title: {title}",
]
if outputs:
    block_lines.append("    outputs:")
    for out in outputs:
        block_lines.append(f"      - {out}")
block = "\n".join(block_lines) + "\n"

if re.search(r"(?m)^tasks:\s*$", text):
    merged = text.rstrip() + "\n" + block
else:
    merged = text.rstrip() + "\n\ntasks:\n" + block

heartbeat_md.write_text(merged, encoding="utf-8")

print(json.dumps({
    "workspace": str(workspace),
    "heartbeat_file": str(heartbeat_md),
    "task_id": task_id,
    "status": "added",
}, ensure_ascii=False))
PY
