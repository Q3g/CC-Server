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
        q = parse_qs(u.query)

        if u.path == "/submit":
            data = self._read_json()
            prompt = (data.get("prompt") or "").strip()
            if not prompt:
                self._send(400, {"error": "empty prompt"})
                return
            # ?wait=N makes /submit synchronous: enqueue, then block for the
            # result and return it in the same response — no separate /result.
            wait = min(int((q.get("wait") or ["0"])[0]), 3600)
            rid = uuid.uuid4().hex[:12]
            item = {
                "id": rid,
                "prompt": prompt,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            ev = threading.Event()
            with STATE_LOCK:
                RESULT_EVENTS[rid] = ev
            REQUESTS.put(item)
            if wait <= 0:
                self._send(200, {"id": rid})
                return
            if ev.wait(timeout=wait):
                with STATE_LOCK:
                    res = RESULTS.pop(rid, None)
                    RESULT_EVENTS.pop(rid, None)
                self._send(200, {"id": rid, "result": res})
            else:
                self._send(200, {"id": rid, "result": None})

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


def _post(path: str, payload: dict, timeout: int = 15) -> dict:
    host, port = server_addr()
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
        print(f"CC Server already running (pid {PID_FILE.read_text().strip()})")
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
            print(f"✅ CC Server started: http://{HOST}:{port} (pid {proc.pid})")
            print(f"   log:  {LOG_FILE}")
            print("   stop: cc_server.py stop")
            return
    print(f"❌ CC Server failed to start, check the log: {LOG_FILE}", file=sys.stderr)
    sys.exit(1)


def cmd_stop():
    if not is_running():
        print("CC Server is not running")
        _cleanup_files()
        return
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"Failed to stop: {e}", file=sys.stderr)
        sys.exit(1)
    for _ in range(30):
        time.sleep(0.1)
        if not is_running():
            print("✅ CC Server stopped")
            return
    print("⚠️  CC Server did not stop within the expected time", file=sys.stderr)
    sys.exit(1)


def cmd_status(quiet: bool):
    running = is_running()
    if quiet:
        sys.exit(0 if running else 1)
    if not running:
        print("CC Server: not running")
        return
    host, port = server_addr()
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/status", timeout=3) as r:
            info = json.loads(r.read())
        print(f"CC Server: running (pid {info['pid']}, http://{host}:{port})")
        print(f"  queued requests:        {info['queued']}")
        print(f"  requests awaiting reply: {info['pending_results']}")
    except Exception as e:
        print(f"CC Server: process exists but is unreachable ({e})")


def cmd_send(prompt: str, wait: int):
    if not prompt.strip():
        print("Request body is empty", file=sys.stderr)
        sys.exit(1)
    if not is_running():
        print("CC Server is not running, start it first: cc_server.py start", file=sys.stderr)
        sys.exit(1)
    # wait > 0 uses the synchronous /submit?wait=N — one round trip, no /result.
    if wait <= 0:
        resp = _post("/submit", {"prompt": prompt})
        rid = resp.get("id")
        if not rid:
            print(f"Submit failed: {resp}", file=sys.stderr)
            sys.exit(1)
        print(f"✅ Request submitted (id={rid})")
        print(f"   query the reply: cc_server.py result {rid} --wait 600")
        return
    print(f"Waiting for Claude Code to reply (up to {wait}s)...")
    resp = _post(f"/submit?wait={wait}", {"prompt": prompt}, timeout=wait + 10)
    rid = resp.get("id")
    if not rid:
        print(f"Submit failed: {resp}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ Request submitted (id={rid})")
    result = resp.get("result")
    if result is None:
        print(f"⏱  Timed out, no reply yet. Query later: cc_server.py result {rid} --wait 600")
        sys.exit(2)
    print("\n--- Claude Code reply ---")
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
    print(f"[CC Server] New request received (id={req['id']}, ts={req['ts']}):")
    print()
    print(req["prompt"])
    print()
    print("After handling the request above, run this to send the result back:")
    print(f"  {SCRIPT_PATH} reply {req['id']} --stdin <<'EOF'")
    print("  <your reply>")
    print("  EOF")


def cmd_reply(rid: str, result: str):
    if not is_running():
        print("CC Server is not running", file=sys.stderr)
        sys.exit(1)
    resp = _post("/reply", {"id": rid, "result": result})
    if resp.get("ok"):
        print(f"✅ Result sent back (id={rid})")
    else:
        print(f"Failed to send result back: {resp}", file=sys.stderr)
        sys.exit(1)


def cmd_result(rid: str, wait: int):
    if not is_running():
        print("CC Server is not running", file=sys.stderr)
        sys.exit(1)
    host, port = server_addr()
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/result?id={rid}&wait={wait}", timeout=wait + 10
        ) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"Unknown request id: {rid}", file=sys.stderr)
        else:
            print(f"Query failed: HTTP {e.code}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Query failed: {e}", file=sys.stderr)
        sys.exit(1)
    result = data.get("result")
    if result is None:
        print("No reply yet")
        sys.exit(2)
    print(result)


# ── Main ────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="CC Server — HTTP request queue for Claude Code session loops")
    sub = parser.add_subparsers(dest="command")

    start_p = sub.add_parser("start", help="start the background server")
    start_p.add_argument("--port", type=int, default=DEFAULT_PORT)

    serve_p = sub.add_parser("serve", help="run the server in the foreground (called by start)")
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)

    sub.add_parser("stop", help="stop the server")

    status_p = sub.add_parser("status", help="show server status")
    status_p.add_argument("--quiet", "-q", action="store_true", help="print nothing, only set the exit code")

    send_p = sub.add_parser("send", help="submit a request to the server")
    send_p.add_argument("prompt", nargs="?", default=None, help="request body")
    send_p.add_argument("--stdin", action="store_true", help="read the request body from stdin")
    send_p.add_argument("--wait", "-w", type=int, default=0, help="max seconds to block for the reply (0 = don't wait)")

    poll_p = sub.add_parser("poll", help="long-poll for one request (called by the Stop hook)")
    poll_p.add_argument("--wait", "-w", type=int, default=840, help="max long-poll seconds")

    reply_p = sub.add_parser("reply", help="post the result for a request")
    reply_p.add_argument("id", help="request id")
    reply_p.add_argument("result", nargs="?", default=None, help="result body")
    reply_p.add_argument("--stdin", action="store_true", help="read the result body from stdin")

    result_p = sub.add_parser("result", help="query the result for a request")
    result_p.add_argument("id", help="request id")
    result_p.add_argument("--wait", "-w", type=int, default=0, help="max seconds to block")

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
            print("Provide a request body: send <prompt> or send --stdin", file=sys.stderr)
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
            print("Provide a result body: reply <id> <result> or reply <id> --stdin", file=sys.stderr)
            sys.exit(1)
        cmd_reply(args.id, result)
    elif args.command == "result":
        cmd_result(args.id, args.wait)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
