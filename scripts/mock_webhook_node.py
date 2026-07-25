#!/usr/bin/env python3
"""Mock "generic webhook" execution node for testing external Flow nodes.

Stdlib only (no venv needed) — run it anywhere python3 exists:

    python3 scripts/mock_webhook_node.py --port 18899

Then in a Flow give an ``external`` agent channel=webhook and
``endpoint_url = http://<this-host>:18899/hook``.

It does no real work: it validates the dispatch package against the
``schemaVersion: 2`` contract and then plays the executor side of it. That
protocol is **outbound-only from ClawsomeFlow** — this node never calls back, it
either answers the dispatch request or waits to be polled — so it works fine on
a machine ClawsomeFlow cannot reach at all.

Per-dispatch behaviour is driven by query params on the endpoint URL, so one
running instance can simulate every case just by varying the URL in the Flow:

    /hook                        accept, then report success ~3s later (polled)
    /hook?sync=1                 answer the dispatch request itself
    /hook?delay=60               slow executor (still succeeds)
    /hook?status=failed          report failure (drives the failure pause)
    /hook?summary=custom+text    custom summary text
    /hook?mode=manual            stay "running" until you click on the web page
    /hook?nopoll=1               accept WITHOUT a poll url — exercises the
                                 fallback where we poll this same endpoint
    /hook?ack=500                reject the dispatch itself (retry next tick)

Inspect what happened at ``http://<this-host>:18899/`` (HTML, auto-refresh) or
``GET /log`` (JSON). Everything is also appended to --log-file as JSONL.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEFAULT_PORT = 18899
DEFAULT_DELAY_SEC = 3.0
HISTORY_MAX = 200

#: Keys the dispatch package must carry for this node to play its part.
REQUIRED_FIELDS = ("runId", "taskId", "taskToken")

_lock = threading.Lock()
_history: deque[dict[str, Any]] = deque(maxlen=HISTORY_MAX)
_by_task: dict[tuple[str, str], dict[str, Any]] = {}  # (runId, taskId) → record
_log_file: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record(entry: dict[str, Any]) -> None:
    pkg = entry.get("package") or {}
    key = (str(pkg.get("runId") or ""), str(pkg.get("taskId") or ""))
    with _lock:
        _history.appendleft(entry)
        # Latest attempt wins: a re-dispatch mints a fresh taskToken and the
        # previous attempt's token stops being accepted by ClawsomeFlow anyway.
        _by_task[key] = entry
    _append_log(entry)


def _append_log(entry: dict[str, Any]) -> None:
    if not _log_file:
        return
    try:
        with open(_log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[warn] cannot write log file: {exc}", flush=True)


def _first(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key) or []
    return values[0] if values else None


def _finish(record: dict[str, Any], *, status: str, summary: str) -> None:
    """Flip a record terminal; the next poll (or the manual page) reports it."""
    with _lock:
        record["result"] = {"status": status, "summary": summary}
        record["state"] = f"finished_{status}"
        record["finishedAt"] = _now()
    _append_log({"event": "finished", "id": record["id"], "status": status})
    print(
        f"[finish] task {(record.get('package') or {}).get('taskId')} "
        f"status={status}",
        flush=True,
    )


def _finish_later(record: dict[str, Any], *, delay: float, status: str, summary: str) -> None:
    def _worker() -> None:
        if delay > 0:
            time.sleep(delay)
        _finish(record, status=status, summary=summary)

    threading.Thread(target=_worker, name="mock-finish", daemon=True).start()


def _describe_dispatch(pkg: dict[str, Any]) -> str:
    ups = pkg.get("upstreamOutputs") or []
    return (
        f"run={pkg.get('runId')} task={pkg.get('taskId')} "
        f"agent={pkg.get('agentId')} channel={pkg.get('channel')} "
        f"v={pkg.get('schemaVersion')} "
        f"subject={str(pkg.get('subject') or '')[:60]!r} upstream={len(ups)}"
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "MockWebhookNode/2"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter access log
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    # ── helpers ──────────────────────────────────────────────────────
    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def _self_base(self) -> str:
        host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
        return f"http://{host}"

    def _bearer(self) -> str:
        value = self.headers.get("Authorization") or ""
        return value[7:].strip() if value.lower().startswith("bearer ") else ""

    # ── routes ───────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        params = parse_qs(parts.query)
        if path in ("/", "/ui"):
            self._send_html(_render_page())
        elif path == "/log":
            with _lock:
                self._send_json(200, {"count": len(_history), "items": list(_history)})
        elif path in ("/health", "/healthz"):
            self._send_json(200, {"status": "ok", "records": len(_history)})
        elif path.startswith("/status/"):
            self._report_status(record_id=path.rsplit("/", 1)[-1])
        else:
            # Poll fallback: ClawsomeFlow GETs the dispatch endpoint itself with
            # ?runId=&taskId= when the executor supplied no poll url.
            run_id = _first(params, "runId") or ""
            task_id = _first(params, "taskId") or ""
            if run_id and task_id:
                with _lock:
                    record = _by_task.get((run_id, task_id))
                self._report_status(record=record)
                return
            self._send_json(
                404,
                {"error": "not found", "hint": "POST the dispatch package to /hook"},
            )

    def _report_status(
        self,
        *,
        record_id: str | None = None,
        record: dict[str, Any] | None = None,
    ) -> None:
        """Answer one poll: ``running`` until finished, then the result."""
        if record is None and record_id:
            with _lock:
                record = next((r for r in _history if r["id"] == record_id), None)
        if record is None:
            self._send_json(404, {"error": "unknown task"})
            return
        presented = self._bearer()
        expected = str((record.get("package") or {}).get("taskToken") or "")
        # Proves the token round-trip; a real integration would 401 instead.
        token_ok = bool(presented) and presented == expected
        with _lock:
            record["polls"] = int(record.get("polls") or 0) + 1
            record["lastPollAt"] = _now()
            record["lastPollTokenOk"] = token_ok
            result = record.get("result")
        if not result:
            self._send_json(200, {"status": "running", "tokenOk": token_ok})
            return
        self._send_json(200, {**result, "tokenOk": token_ok})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        params = parse_qs(parts.query)
        if path.startswith("/complete"):
            self._handle_manual_complete(path, params)
            return
        self._handle_dispatch(params)

    def _handle_dispatch(self, params: dict[str, list[str]]) -> None:
        try:
            pkg = self._read_json()
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON body: {exc}"})
            return
        if not isinstance(pkg, dict):
            self._send_json(400, {"error": "expected a JSON object body"})
            return

        missing = [k for k in REQUIRED_FIELDS if not str(pkg.get(k) or "").strip()]
        record: dict[str, Any] = {
            "id": uuid.uuid4().hex[:8],
            "at": _now(),
            "query": {k: v[0] for k, v in params.items()},
            "package": pkg,
            "missing": missing,
            "state": "received",
            "polls": 0,
        }
        print(f"[dispatch] {_describe_dispatch(pkg)}", flush=True)
        if missing:
            record["state"] = "rejected_incomplete"
            _record(record)
            self._send_json(
                400,
                {"error": "dispatch package missing required fields", "missing": missing},
            )
            return

        ack = int(_first(params, "ack") or 200)
        if not (200 <= ack < 300):
            record["state"] = "ack_rejected"
            _record(record)
            self._send_json(ack, {"error": f"simulated ack failure ({ack})"})
            return

        mode = (_first(params, "mode") or "auto").lower()
        status = (_first(params, "status") or "success").lower()
        if status not in ("success", "failed"):
            status = "success"
        delay = float(_first(params, "delay") or DEFAULT_DELAY_SEC)
        summary = _first(params, "summary") or _default_summary(pkg, status)
        sync = (_first(params, "sync") or "").lower() in ("1", "true", "yes")
        nopoll = (_first(params, "nopoll") or "").lower() in ("1", "true", "yes")

        if sync:
            record["state"] = f"answered_{status}"
            record["result"] = {"status": status, "summary": summary}
            _record(record)
            self._send_json(200, {"status": status, "summary": summary})
            return

        poll_url = "" if nopoll else f"{self._self_base()}/status/{record['id']}"
        if mode == "manual":
            record["state"] = "awaiting_manual"
        else:
            record["state"] = "accepted"
            record["plan"] = {"status": status, "delaySec": delay, "summary": summary}
            _finish_later(record, delay=delay, status=status, summary=summary)
        _record(record)
        accepted: dict[str, Any] = {
            "status": "accepted",
            "recordId": record["id"],
            "poll": {"intervalSeconds": 5},
        }
        if poll_url:
            accepted["poll"]["url"] = poll_url
        self._send_json(202, accepted)

    def _handle_manual_complete(self, path: str, params: dict[str, list[str]]) -> None:
        record_id = path.rsplit("/", 1)[-1]
        try:
            body = self._read_json() or {}
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        with _lock:
            record = next((r for r in _history if r["id"] == record_id), None)
        if record is None:
            self._send_json(404, {"error": f"no dispatch record {record_id!r}"})
            return
        status = str(
            body.get("status") or _first(params, "status") or "success"
        ).lower()
        if status not in ("success", "failed"):
            status = "success"
        summary = str(
            body.get("summary")
            or _first(params, "summary")
            or _default_summary(record["package"], status)
        )
        _finish(record, status=status, summary=summary)
        self._send_json(200, {"ok": True, "result": record.get("result")})


def _default_summary(pkg: dict[str, Any], status: str) -> str:
    task = pkg.get("taskId") or "?"
    if status == "failed":
        return (
            f"[mock webhook node] simulated failure for task {task}: "
            "the test executor was told to report FAILED."
        )
    return (
        f"[mock webhook node] task {task} handled by the test webhook node at "
        f"{_now()}. No real work was performed; this is a protocol smoke test. "
        "Deliverable: none (mock)."
    )


_PAGE_CSS = """
body{font:14px/1.5 system-ui,sans-serif;margin:24px;color:#1f2933;background:#f7f8fa}
h1{font-size:18px} h2{font-size:15px;margin:24px 0 8px}
code,pre{font-family:ui-monospace,Menlo,monospace}
pre{background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:10px;
overflow:auto;max-height:280px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:12px;
margin-bottom:12px}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
background:#eef2f7;margin-right:6px}
.ok{background:#e6f6ec;color:#166534}.bad{background:#fdecec;color:#991b1b}
.wait{background:#fff7e6;color:#92400e}
button{font:13px system-ui;padding:4px 10px;border-radius:6px;
border:1px solid #cbd5e1;background:#fff;cursor:pointer}
"""

_PAGE_JS = """
async function done(id, status){
  await fetch('/complete/'+id, {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({status: status})});
  location.reload();
}
setTimeout(()=>location.reload(), 5000);
"""


def _state_class(state: str) -> str:
    if state.endswith("success"):
        return "ok"
    if (
        state.endswith("failed")
        or state in ("rejected_incomplete", "ack_rejected")
    ):
        return "bad"
    return "wait"


def _render_page() -> str:
    with _lock:
        items = list(_history)
    rows = []
    for it in items:
        pkg = it.get("package") or {}
        buttons = ""
        if it.get("state") == "awaiting_manual":
            buttons = (
                f"<p><button onclick=\"done('{it['id']}','success')\">报告成功"
                f"</button> <button onclick=\"done('{it['id']}','failed')\">"
                "报告失败</button></p>"
            )
        result = it.get("result")
        result_html = ""
        if result:
            result_html = (
                "<div><span class='tag'>result</span>"
                f"{html.escape(str(result.get('status')))}"
                f"<pre>{html.escape(str(result.get('summary') or ''))}</pre></div>"
            )
        rows.append(
            "<div class='card'>"
            f"<span class='tag {_state_class(str(it.get('state')))}'>"
            f"{html.escape(str(it.get('state')))}</span>"
            f"<span class='tag'>{html.escape(str(it.get('at')))}</span>"
            f"<span class='tag'>task {html.escape(str(pkg.get('taskId')))}</span>"
            f"<span class='tag'>run {html.escape(str(pkg.get('runId')))}</span>"
            f"<span class='tag'>polls {html.escape(str(it.get('polls') or 0))}"
            f"{'' if it.get('lastPollTokenOk') is not False else ' · bad token'}"
            "</span>"
            f"<div><b>{html.escape(str(pkg.get('subject') or ''))}</b></div>"
            f"{buttons}{result_html}"
            "<details><summary>dispatch package</summary>"
            f"<pre>{html.escape(json.dumps(pkg, ensure_ascii=False, indent=2))}</pre>"
            "</details></div>"
        )
    body = "".join(rows) or "<p>还没有收到任何派发。</p>"
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>Mock Webhook Node</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        "<h1>Mock Webhook 执行节点（测试用）</h1>"
        "<p>把 <code>/hook</code> 填进 Flow 的 webhook 外部节点。可选参数："
        "<code>?sync=1</code>、<code>?status=failed</code>、<code>?delay=30</code>、"
        "<code>?mode=manual</code>、<code>?nopoll=1</code>、"
        "<code>?ack=500</code>、<code>?summary=...</code></p>"
        "<p>本节点从不回调 ClawsomeFlow：要么在派发响应里给结果，"
        "要么等 ClawsomeFlow 来轮询。</p>"
        f"<h2>最近 {len(items)} 次交互</h2>{body}"
        f"<script>{_PAGE_JS}</script></body></html>"
    )


def main() -> None:
    global _log_file
    ap = argparse.ArgumentParser(description="Mock webhook execution node")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument(
        "--log-file",
        default=None,
        help="append every dispatch/result as JSONL to this file",
    )
    args = ap.parse_args()
    _log_file = args.log_file

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"mock webhook node listening on http://{args.host}:{args.port}\n"
        f"  endpoint for Flow : http://<this-host>:{args.port}/hook\n"
        f"  inspect           : http://<this-host>:{args.port}/\n"
        "  this is a server: it holds the terminal until Ctrl-C. To background it:\n"
        f"    nohup python3 {sys.argv[0]} --port {args.port} > /tmp/mock-webhook.log 2>&1 &",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("bye", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
