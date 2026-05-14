---
name: cc-server
description: |
  A request server built on the Claude Code session loop. Starts a local HTTP
  server that holds a request queue; you submit requests from the command line,
  and Claude Code long-polls in its Stop hook, processing each request and
  posting the result back.
  Trigger phrases: start cc server, cc session loop, send a request to cc,
  cc server status, 启动 cc server, 给 cc 发请求.
allowed-tools: Bash, Read
---

# CC Server — a request server built on the Claude Code session loop

> 中文说明：[../../../docs/SKILL.zh-CN.md](../../../docs/SKILL.zh-CN.md)

## Mechanism

```
   CLI ──── send ────▶ HTTP Server (request queue)
                            ▲   │
                     poll   │   │  returns a request
        ┌───────────────────┘   ▼
   Claude Code Stop hook ──▶ Claude Code processes ──▶ reply ──▶ Server stores result
        (long-poll)                                                  │
                                                                      ▼
                                       CLI  send --wait / result  retrieves result
```

- **While the server runs**: the Stop hook long-polls every time, so the session can't stop — it stays on standby.
- **Once the server stops**: the Stop hook sees it's down and lets go, so the session ends normally.
- In other words: **`start` opens the session loop, `stop` closes it.**

Script: `.claude/skills/cc-server/scripts/cc_server.py` (pure standard library, no dependencies).

## Start / stop the server

```bash
# Start the background server (default port 8787, override with CC_SERVER_PORT)
.claude/skills/cc-server/scripts/cc_server.py start

# Check status
.claude/skills/cc-server/scripts/cc_server.py status

# Stop (this also ends the Stop hook polling loop)
.claude/skills/cc-server/scripts/cc_server.py stop
```

## Submit a request (submitter side)

```bash
# Pass the prompt directly
.claude/skills/cc-server/scripts/cc_server.py send "Look up X for me"

# Long content via stdin
cat task.md | .claude/skills/cc-server/scripts/cc_server.py send --stdin

# Submit and block for Claude Code's reply (up to 600s)
.claude/skills/cc-server/scripts/cc_server.py send "..." --wait 600

# Retrieve the result separately later
.claude/skills/cc-server/scripts/cc_server.py result <id> --wait 600
```

## Process a request (Claude Code side)

After the Stop hook runs `poll`, Claude Code sees something like this on stderr:

```
[CC Server] New request received (id=abc123def456, ts=...):

Look up X for me

After handling the request above, run this to send the result back:
  .../cc_server.py reply abc123def456 --stdin <<'EOF'
  <your reply>
  EOF
```

Claude Code should:

1. Understand and complete the task in the request;
2. Send the result back with `reply`:

```bash
.claude/skills/cc-server/scripts/cc_server.py reply <id> "result text"
# or long results via stdin
.claude/skills/cc-server/scripts/cc_server.py reply <id> --stdin <<'EOF'
...
EOF
```

After replying, the session stops again and the Stop hook long-polls for the next request.

## Stop hook integration

`.claude/hooks/cc-server-poll.sh` is already implemented and registered in the
Stop hook of `.claude/settings.json` (timeout 900000 ms, `poll --wait 840`,
leaving headroom). Its logic:

1. Server not running → `exit 0`, the session ends normally;
2. Long-poll returns a request → print it for Claude Code, `exit 2` to continue the session;
3. Timed out with no request → `exit 2`, keep the polling loop alive.

## HTTP API

| Method | Path | Description |
|--------|------|-------------|
| POST | `/submit` | `{prompt}` → `{id}`, enqueue |
| GET  | `/poll?wait=N` | long-poll → `{request}` or `{request:null}` |
| POST | `/reply` | `{id, result}` stores result and wakes any waiter |
| GET  | `/result?id=X&wait=N` | get result → `{result}` or `{result:null}` |
| GET  | `/status` | health check, queue length |
| POST | `/shutdown` | graceful shutdown |

## File locations

- PID / port / log: `~/.config/cc-server/{server.pid,server.port,server.log}`
- The request queue and results live only in the server's memory — cleared on restart.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `start` fails | Check `~/.config/cc-server/server.log`; the port may be taken — change `CC_SERVER_PORT` |
| Session won't stop | Expected (the server is running); `stop` ends the loop |
| `send --wait` times out | The request is queued but not done yet; keep waiting with `result <id> --wait N` |
| Process is up but unreachable | `stop`, then `start` again |
