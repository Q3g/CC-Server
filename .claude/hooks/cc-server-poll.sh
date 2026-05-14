#!/usr/bin/env bash
# Stop hook — long-poll the CC Server and feed queued requests back to Claude Code.
#
# While the CC Server is running, this keeps the session alive in a polling
# loop: each time Claude Code stops, it long-polls for a request, processes
# whatever arrives, then stops again and re-polls. Stop the loop by stopping
# the server: cc_server.py stop
cat >/dev/null  # drain the hook JSON payload on stdin

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HOOK_DIR/../skills/cc-server/scripts/cc_server.py"

# Server not running → nothing to do, let the session stop normally.
"$SCRIPT" status --quiet 2>/dev/null || exit 0

# Long-poll for a queued request (kept under the hook timeout in settings.json).
# CC_SERVER_POLL_WAIT overrides the wait window — handy for testing.
OUT=$("$SCRIPT" poll --wait "${CC_SERVER_POLL_WAIT:-840}" 2>/dev/null)

if [ -n "$OUT" ]; then
  echo "$OUT" >&2
  exit 2  # feed the request to Claude Code and keep the session alive
fi

# Timeout with no request — keep the polling loop alive.
echo "[CC Server] 无新请求，继续轮询中…" >&2
exit 2
