# CC Server — a request server built on the Claude Code session loop

> 中文文档：[docs/README.zh-CN.md](docs/README.zh-CN.md)

Turn a running Claude Code session into a **standing service**: submit requests
from the command line (or anything that can send HTTP), and Claude Code picks
them up while idle, processes them, posts the result back, then waits for the
next one.

## Usage flow

The order matters — the server must be running **and** a Claude Code session
must be alive for requests to get picked up. Use two terminals:

**Terminal A — start the server, then run Claude Code:**

```bash
cd /path/to/cc_server
S=.claude/skills/cc-server/scripts/cc_server.py

# 1. Start the background server (default port 8787)
$S start

# 2. Run Claude Code in this project. Once the session goes idle, its Stop
#    hook starts long-polling the server — the session stays on standby.
claude
```

**Terminal B — submit requests and collect results:**

```bash
cd /path/to/cc_server
S=.claude/skills/cc-server/scripts/cc_server.py

# 3. Submit a request — this prints an id
$S send "What skills do you have?"
#    → ✅ Request submitted (id=c85286b7e009)

# 4. Fetch the result once the session in Terminal A has handled it
$S result c85286b7e009 --wait 600
```

`$S send "..." --wait 600` can also submit and block for the reply in one step.
When you're done, `$S stop` (from either terminal) shuts the server down and
releases the session loop.

## Why

Claude Code's Stop hook has a useful property: when the hook exits with code
`2`, its stderr is fed back to the model and **the session continues** instead
of stopping. CC Server turns that property into a local HTTP queue service:

- The Stop hook no longer means "done, stop" — it means "done, go check for more work."
- While the server runs, the session can't stop; it stays on standby.
- The moment the server stops, the Stop hook lets go and the session ends normally.

In other words: **starting the server opens the session loop, stopping the server closes it.**

## How it works

```
   CLI ──── send ────▶  HTTP Server (in-memory request queue)
                              ▲   │
                       poll   │   │  returns one request
        ┌─────────────────────┘   ▼
   Claude Code Stop hook ──▶ Claude Code does the work ──▶ reply ──▶ Server stores result
        (long-poll, exit 2                                              │
         feeds request to model)                                        ▼
                                          CLI  send --wait / result  retrieves result
```

A full round trip:

1. **Submit** — `cc_server.py send "..."` POSTs the request to the server, it's queued, you get an `id`.
2. **Pick up** — when the Claude Code session stops, the Stop hook runs `cc_server.py poll` and long-polls the server.
3. **Feed back** — once `poll` gets a request it prints it to stderr; the hook exits `2`, so the request becomes the model's new input.
4. **Process** — Claude Code understands and completes the task in the request.
5. **Reply** — Claude Code runs `cc_server.py reply <id> ...`, storing the result back on the server.
6. **Retrieve** — the submitter blocks on `send --wait`, or queries later with `result <id>`.
7. The session stops again → back to step 2, polling for the next request.

## Layout

```
cc_server/
├── README.md                            # English (this file)
├── docs/
│   ├── README.zh-CN.md                  # Chinese README
│   └── SKILL.zh-CN.md                   # Chinese skill doc
└── .claude/
    ├── settings.json                    # registers the Stop hook
    ├── hooks/
    │   └── cc-server-poll.sh            # Stop hook: long-polls the server
    └── skills/
        └── cc-server/
            ├── SKILL.md                 # usage doc for Claude Code
            └── scripts/
                └── cc_server.py         # HTTP server + CLI (pure stdlib, no deps)
```

## CLI commands

| Command | Description |
|---------|-------------|
| `start [--port N]` | Start the background server daemon |
| `stop` | Stop the server (daemon exits gracefully on SIGTERM) |
| `serve [--port N]` | Run the server in the foreground (called by `start`; for debugging) |
| `status [--quiet]` | Show status; `--quiet` prints nothing and just sets the exit code |
| `send <prompt>` | Submit a request. `--stdin` reads from stdin, `--wait N` blocks up to N seconds for the reply |
| `poll [--wait N]` | Long-poll for one request (called by the Stop hook, default 840 s) |
| `reply <id>` | Post the result for a request. `--stdin` reads from stdin |
| `result <id> [--wait N]` | Query a request's result; `--wait` blocks until it's ready |

Examples:

```bash
# Submit long content via stdin
cat task.md | $S send --stdin --wait 900

# Claude Code posts a long result back
$S reply 5114cf20e779 --stdin <<'EOF'
Here is the processed result...
EOF

# Query a request's result later
$S result 5114cf20e779 --wait 600
```

## HTTP API

The server listens on `127.0.0.1:8787` (loopback only), plain JSON:

| Method | Path | Request / Response |
|--------|------|--------------------|
| `POST` | `/submit` | `{prompt}` → `{id}`, request queued (fire-and-forget) |
| `POST` | `/submit?wait=N` | `{prompt}` → `{id,result}` — **synchronous**: submit and block up to N s for the answer, no separate `/result` needed |
| `GET`  | `/poll?wait=N` | long-poll → `{request:{id,prompt,ts}}` or `{request:null}` |
| `POST` | `/reply` | `{id,result}` → `{ok:true}`, stores result and wakes any waiter |
| `GET`  | `/result?id=X&wait=N` | → `{result}` or `{result:null}` |
| `GET`  | `/status` | → `{ok,pid,queued,pending_results}` |
| `POST` | `/shutdown` | → `{ok:true}`, graceful shutdown |

So the CLI is optional — any program or script that can speak HTTP can hand
work to the session. For a one-shot request/response, a single
`POST /submit?wait=N` is all you need (this is what `send --wait N` uses under
the hood).

## Stop hook integration

Registered in `.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/cc-server-poll.sh",
            "timeout": 900000
          }
        ]
      }
    ]
  }
}
```

- Uses `$CLAUDE_PROJECT_DIR` (the project root injected by Claude Code) — no machine-specific absolute paths.
- `timeout` is 900000 ms (15 min); the hook runs `poll --wait 840` (14 min), leaving headroom.

What `cc-server-poll.sh` does:

1. Server not running → `exit 0`, the session ends normally.
2. Long-poll returns a request → print it to stderr, `exit 2`, feed the request to the model and continue the session.
3. Timed out with no request → `exit 2`, keep the polling loop alive.

> For debugging, the `CC_SERVER_POLL_WAIT` environment variable overrides the
> 840-second wait window, e.g. `CC_SERVER_POLL_WAIT=3` makes the hook wait only 3 seconds.

## Design notes

### Why long-polling lives on the server side — and must check for disconnects in slices

`/poll` has the server hold the connection and block until a request arrives
(server push, low latency). But blocking the whole window with a single
`queue.Queue.get(timeout=wait)` hides a serious flaw:

> When a long-poll client disconnects (a killed hook, an interrupted process),
> `queue.get()` has no idea the socket is dead and the handler thread **keeps
> blocking**. When the next request is enqueued, this **zombie thread steals
> it**, then writes it into an already-closed connection (`BrokenPipeError`) —
> and the request is lost.

So `/poll` is implemented as:

- One `get(timeout=1.0)` per second, and between rounds it checks whether the
  client is still there using `select` + `MSG_PEEK` (`_client_alive()`); if it's
  gone, return immediately and **never consume the queue**.
- If the client dies in the tiny window after `get` succeeds but before `_send`,
  the request is **re-queued** rather than lost.

Any blocking long-poll service should watch for this race.

### Other

- **Zero dependencies** — `cc_server.py` uses only the Python standard library (`http.server`, `queue`, `threading`, `select`, ...); no `pip install`.
- **Concurrency** — `ThreadingHTTPServer`, one thread per connection, so `submit` / `poll` / `reply` don't block each other.
- **State in memory** — the request queue and results live only in the server process's memory and **are cleared on restart**; not persisting is intentional (a request is "work for the session right now" — stale ones have no meaning).
- **Daemon** — `start` uses `subprocess.Popen(..., start_new_session=True)` to spawn `serve` as an independent background process; pid / port / log go to `~/.config/cc-server/`. `stop` sends `SIGTERM`; `serve`'s `SIGTERM` handler calls `httpd.shutdown()` from a separate thread (to avoid deadlocking with `serve_forever`).

## Files and configuration

| Path | Purpose |
|------|---------|
| `~/.config/cc-server/server.pid` | daemon PID |
| `~/.config/cc-server/server.port` | actual listening port |
| `~/.config/cc-server/server.log` | server log (check here first if `start` fails) |

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `CC_SERVER_PORT` | `8787` | server listening port |
| `CC_SERVER_POLL_WAIT` | `840` | wait seconds for `poll` in the hook script (for debugging) |

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `start` fails | Check `~/.config/cc-server/server.log`; usually the port is taken — change `CC_SERVER_PORT` |
| Session won't stop | Expected behavior (the server is running); run `cc_server.py stop` to end the loop |
| `send --wait` times out | The request is queued but not done yet; keep waiting with `result <id> --wait N` |
| Process is up but unreachable | `stop`, then `start` again |
| `status` shows "process exists but can't connect" | The daemon is wedged; `stop` to clean up, then restart |

## Use cases

- **Dispatch work while away** — `start` before you leave, then push tasks into the session from your phone / scripts / cron jobs, and collect the results when you're back.
- **Drive Claude Code across processes** — any program can hand a task to a real Claude Code session over HTTP.
- **Serial task queue** — multiple requests line up, and the session works through them one by one.
