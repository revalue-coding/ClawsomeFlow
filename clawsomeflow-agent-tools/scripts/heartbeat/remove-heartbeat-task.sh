#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  remove-heartbeat-task.sh \
    --workspace <agent_workspace> \
    --task-id <task_id>

Description:
  Remove one task item from <workspace>/HEARTBEAT.md by task id.
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

if [[ -z "$WORKSPACE" || -z "$TASK_ID" ]]; then
  echo "required arguments missing" >&2
  usage
  exit 2
fi

require_cmd python3

python3 - "$WORKSPACE" "$TASK_ID" <<'PY'
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

workspace = Path(sys.argv[1]).expanduser().resolve()
task_id = sys.argv[2].strip()

if not workspace.exists() or not workspace.is_dir():
    raise SystemExit(f"workspace does not exist: {workspace}")

heartbeat_md = workspace / "HEARTBEAT.md"
if not heartbeat_md.exists():
    raise SystemExit(f"HEARTBEAT.md not found: {heartbeat_md}")

lines = heartbeat_md.read_text(encoding="utf-8").splitlines(keepends=True)
pat = re.compile(rf"^\s*-\s*id:\s*{re.escape(task_id)}\s*$")
task_header_pat = re.compile(r"^\s*-\s*id:\s*.+$")

start = -1
for i, line in enumerate(lines):
    if pat.match(line.rstrip("\n")):
        start = i
        break

if start < 0:
    raise SystemExit(f"task id not found: {task_id}")

end = start + 1
while end < len(lines):
    candidate = lines[end].rstrip("\n")
    if task_header_pat.match(candidate):
        break
    if candidate.startswith("#"):
        break
    end += 1

new_lines = lines[:start] + lines[end:]
heartbeat_md.write_text("".join(new_lines).rstrip() + "\n", encoding="utf-8")

print(json.dumps({
    "workspace": str(workspace),
    "heartbeat_file": str(heartbeat_md),
    "task_id": task_id,
    "status": "removed",
}, ensure_ascii=False))
PY
