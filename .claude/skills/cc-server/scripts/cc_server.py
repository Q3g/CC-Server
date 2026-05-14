#!/usr/bin/env python3
"""
CC Server — HTTP request queue for Claude Code session loops.

A small HTTP server holds a queue of requests. A CLI submits requests; Claude
Code's Stop hook long-polls the server and processes whatever arrives, then
posts the result back. While the server runs, the polling session stays alive
— stop the server to end the loop.

Subcommands:
  start            Start the server as a background daemon
  serve            Run the server in the foreground (used by `start`)
  stop             Stop the running server
  status           Show server status (--quiet: exit 0 if running, 1 if not)
  send <prompt>    Submit a request (--stdin to read prompt, --wait N for reply)
  poll             Long-poll for one request (used by the Stop hook)
  reply <id>       Post a result back for a request (--stdin to read result)
  result <id>      Fetch the result for a request (--wait N to block)
"""

import argparse
import json
import os
import queue
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Config ──────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".config" / "cc-server"
PID_FILE = CONFIG_DIR / "server.pid"
PORT_FILE = CONFIG_DIR / "server.port"
LOG_FILE = CONFIG_DIR / "server.log"
HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("CC_SERVER_PORT", "8787"))
SCRIPT_PATH = os.path.abspath(__file__)

# ── Server state ────────────────────────────────────────────────────────

REQUESTS: "queue.Queue[dict]" = queue.Queue()
RESULTS: dict[str, str] = {}
RESULT_EVENTS: dict[str, threading.Event] = {}
STATE_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D102 — log to the daemon log file
        sys.stderr.write(
            "%s [%s] %s\n" % (self.address_string(), time.strftime("%H:%M:%S"), fmt % args)
        )

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_alive(self) -> bool:
        """True while the client connection is still open (no data expected on a
        waiting long-poll, so a readable socket means it was closed)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return True
            return self.connection.recv(1, socket.MSG_PEEK) != b""
        except OSError:
            return False

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/status":
            self._send(200, {
                "ok": True,
                "pid": os.getpid(),
                "queued": REQUESTS.qsize(),
                "pending_results": len(RESULT_EVENTS),
            })

        elif u.path == "/poll":
            wait = max(1, min(int((q.get("wait") or ["1"])[0]), 3600))
            deadline = time.time() + wait
            # Poll in short slices so a disconnected client is noticed quickly
            # and its thread exits — instead of blocking in get() and stealing
            # the next request to deliver it into a dead socket.
            while time.time() < deadline:
                try:
                    item = REQUESTS.get(timeout=1.0)
                except queue.Empty:
                    if not self._client_alive():
                        return  # client gone — don't consume future requests
                    continue
                try:
                    self._send(200, {"request": item})
                except OSError:
                    REQUESTS.put(item)  # client vanished mid-send — re-queue it
                return
            self._send(200, {"request": None})

        elif u.path == "/result":
            rid = (q.get("id") or [""])[0]
            wait = min(int((q.get("wait") or ["0"])[0]), 3600)
            ev = RESULT_EVENTS.get(rid)
            if ev is None:
                self._send(404, {"error": "unknown id"})
                return
            got = ev.wait(timeout=wait if wait > 0 else 0)
            if got:
                with STATE_LOCK:
                    res = RESULTS.pop(rid, None)
                    RESULT_EVENTS.pop(rid, None)
                self._send(200, {"result": res})
            else:
                self._send(200, {"result": None})

        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)

        if u.path == "/submit":
            data = self._read_json()
            prompt = (data.get("prompt") or "").strip()
            if not prompt:
                self._send(400, {"error": "empty prompt"})
                return
            rid = uuid.uuid4().hex[:12]
            item = {
                "id": rid,
                "prompt": prompt,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            with STATE_LOCK:
                RESULT_EVENTS[rid] = threading.Event()
            REQUESTS.put(item)
            self._send(200, {"id": rid})

        elif u.path == "/reply":
            data = self._read_json()
            rid = data.get("id", "")
            result = data.get("result", "")
            with STATE_LOCK:
                ev = RESULT_EVENTS.get(rid)
                if ev is None:
                    self._send(404, {"error": "unknown id"})
                    return
                RESULTS[rid] = result
                ev.set()
            self._send(200, {"ok": True})

        elif u.path == "/shutdown":
            self._send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        else:
            self._send(404, {"error": "not found"})


# ── Daemon helpers ──────────────────────────────────────────────────────


def server_addr() -> tuple[str, int]:
    if PORT_FILE.exists():
        try:
            return HOST, int(PORT_FILE.read_text().strip())
        except (ValueError, OSError):
            pass
    return HOST, DEFAULT_PORT


def is_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _ping(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/status", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _post(path: str, payload: dict) -> dict:
    host, port = server_addr()
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def _cleanup_files():
    for f in (PID_FILE, PORT_FILE):
        try:
            f.unlink()
        except OSError:
            pass


# ── Subcommands ─────────────────────────────────────────────────────────


def cmd_serve(port: int):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    httpd.daemon_threads = True
    PID_FILE.write_text(str(os.getpid()))
    PORT_FILE.write_text(str(port))

    # Run shutdown() off the main thread so it doesn't deadlock serve_forever().
    signal.signal(
        signal.SIGTERM,
        lambda *a: threading.Thread(target=httpd.shutdown, daemon=True).start(),
    )

    print(f"CC Server listening on http://{HOST}:{port} (pid {os.getpid()})", flush=True)
    try:
        httpd.serve_forever()
    finally:
        _cleanup_files()
        print("CC Server stopped", flush=True)


def cmd_start(port: int):
    if is_running():
        print(f"CC Server 已在运行 (pid {PID_FILE.read_text().strip()})")
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_files()  # clear stale pid/port from a crashed run
    log = open(LOG_FILE, "a")
    proc = subprocess.Popen(
        [sys.executable, SCRIPT_PATH, "serve", "--port", str(port)],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    for _ in range(50):
        time.sleep(0.1)
        if _ping(port):
            print(f"✅ CC Server 已启动: http://{HOST}:{port} (pid {proc.pid})")
            print(f"   日志: {LOG_FILE}")
            print("   停止: cc_server.py stop")
            return
    print(f"❌ CC Server 启动失败，查看日志: {LOG_FILE}", file=sys.stderr)
    sys.exit(1)


def cmd_stop():
    if not is_running():
        print("CC Server 未运行")
        _cleanup_files()
        return
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"停止失败: {e}", file=sys.stderr)
        sys.exit(1)
    for _ in range(30):
        time.sleep(0.1)
        if not is_running():
            print("✅ CC Server 已停止")
            return
    print("⚠️  CC Server 未在预期时间内停止", file=sys.stderr)
    sys.exit(1)


def cmd_status(quiet: bool):
    running = is_running()
    if quiet:
        sys.exit(0 if running else 1)
    if not running:
        print("CC Server: 未运行")
        return
    host, port = server_addr()
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/status", timeout=3) as r:
            info = json.loads(r.read())
        print(f"CC Server: 运行中 (pid {info['pid']}, http://{host}:{port})")
        print(f"  队列中待处理请求: {info['queued']}")
        print(f"  等待回复的请求:   {info['pending_results']}")
    except Exception as e:
        print(f"CC Server: 进程存在但无法连接 ({e})")


def cmd_send(prompt: str, wait: int):
    if not prompt.strip():
        print("请求内容为空", file=sys.stderr)
        sys.exit(1)
    if not is_running():
        print("CC Server 未运行，请先: cc_server.py start", file=sys.stderr)
        sys.exit(1)
    resp = _post("/submit", {"prompt": prompt})
    rid = resp.get("id")
    if not rid:
        print(f"提交失败: {resp}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ 已提交请求 (id={rid})")
    if wait <= 0:
        print(f"   查询回复: cc_server.py result {rid} --wait 600")
        return
    print(f"等待 Claude Code 处理回复 (最多 {wait}s)...")
    host, port = server_addr()
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/result?id={rid}&wait={wait}", timeout=wait + 10
        ) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"等待回复失败: {e}", file=sys.stderr)
        sys.exit(1)
    result = data.get("result")
    if result is None:
        print(f"⏱  超时，暂无回复。稍后查询: cc_server.py result {rid} --wait 600")
        sys.exit(2)
    print("\n--- Claude Code 回复 ---")
    print(result)


def cmd_poll(wait: int):
    """Long-poll for one request. Silent on no-request/errors — used by the Stop hook."""
    if not is_running():
        return
    host, port = server_addr()
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/poll?wait={wait}", timeout=wait + 10
        ) as r:
            data = json.loads(r.read())
    except Exception:
        return
    req = data.get("request")
    if not req:
        return
    print(f"[CC Server] 收到新请求 (id={req['id']}, ts={req['ts']}):")
    print()
    print(req["prompt"])
    print()
    print("处理完上述请求后，运行以下命令把结果回传给提交方：")
    print(f"  {SCRIPT_PATH} reply {req['id']} --stdin <<'EOF'")
    print("  <你的回复内容>")
    print("  EOF")


def cmd_reply(rid: str, result: str):
    if not is_running():
        print("CC Server 未运行", file=sys.stderr)
        sys.exit(1)
    resp = _post("/reply", {"id": rid, "result": result})
    if resp.get("ok"):
        print(f"✅ 已回传结果 (id={rid})")
    else:
        print(f"回传失败: {resp}", file=sys.stderr)
        sys.exit(1)


def cmd_result(rid: str, wait: int):
    if not is_running():
        print("CC Server 未运行", file=sys.stderr)
        sys.exit(1)
    host, port = server_addr()
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/result?id={rid}&wait={wait}", timeout=wait + 10
        ) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"未知请求 id: {rid}", file=sys.stderr)
        else:
            print(f"查询失败: HTTP {e.code}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"查询失败: {e}", file=sys.stderr)
        sys.exit(1)
    result = data.get("result")
    if result is None:
        print("暂无回复")
        sys.exit(2)
    print(result)


# ── Main ────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="CC Server — HTTP request queue for Claude Code session loops")
    sub = parser.add_subparsers(dest="command")

    start_p = sub.add_parser("start", help="启动后台 Server")
    start_p.add_argument("--port", type=int, default=DEFAULT_PORT)

    serve_p = sub.add_parser("serve", help="前台运行 Server（由 start 调用）")
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)

    sub.add_parser("stop", help="停止 Server")

    status_p = sub.add_parser("status", help="查看 Server 状态")
    status_p.add_argument("--quiet", "-q", action="store_true", help="不输出，仅用退出码表示是否运行")

    send_p = sub.add_parser("send", help="向 Server 提交一个请求")
    send_p.add_argument("prompt", nargs="?", default=None, help="请求内容")
    send_p.add_argument("--stdin", action="store_true", help="从 stdin 读取请求内容")
    send_p.add_argument("--wait", "-w", type=int, default=0, help="阻塞等待回复的最长秒数（0=不等待）")

    poll_p = sub.add_parser("poll", help="长轮询一个请求（由 Stop hook 调用）")
    poll_p.add_argument("--wait", "-w", type=int, default=840, help="长轮询最长秒数")

    reply_p = sub.add_parser("reply", help="回传某个请求的处理结果")
    reply_p.add_argument("id", help="请求 id")
    reply_p.add_argument("result", nargs="?", default=None, help="结果内容")
    reply_p.add_argument("--stdin", action="store_true", help="从 stdin 读取结果内容")

    result_p = sub.add_parser("result", help="查询某个请求的处理结果")
    result_p.add_argument("id", help="请求 id")
    result_p.add_argument("--wait", "-w", type=int, default=0, help="阻塞等待的最长秒数")

    args = parser.parse_args()

    if args.command == "start":
        cmd_start(args.port)
    elif args.command == "serve":
        cmd_serve(args.port)
    elif args.command == "stop":
        cmd_stop()
    elif args.command == "status":
        cmd_status(args.quiet)
    elif args.command == "send":
        if args.stdin:
            prompt = sys.stdin.read()
        elif args.prompt:
            prompt = args.prompt
        else:
            print("请提供请求内容: send <prompt> 或 send --stdin", file=sys.stderr)
            sys.exit(1)
        cmd_send(prompt, args.wait)
    elif args.command == "poll":
        cmd_poll(args.wait)
    elif args.command == "reply":
        if args.stdin:
            result = sys.stdin.read()
        elif args.result is not None:
            result = args.result
        else:
            print("请提供结果内容: reply <id> <result> 或 reply <id> --stdin", file=sys.stderr)
            sys.exit(1)
        cmd_reply(args.id, result)
    elif args.command == "result":
        cmd_result(args.id, args.wait)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
