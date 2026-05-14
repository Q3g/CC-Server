# CC Server Skill — 基于 CC 会话循环的请求 Server

> English: [../.claude/skills/cc-server/SKILL.md](../.claude/skills/cc-server/SKILL.md)
>
> 本文是 `cc-server` skill 的中文说明。Claude Code 实际加载的是英文版
> `SKILL.md`（带 YAML frontmatter）；本文件仅供阅读参考。

## 机制

```
   命令行 ── send ──▶ HTTP Server（请求队列）
                         ▲   │
                  poll   │   │  返回请求
        ┌────────────────┘   ▼
   Claude Code Stop hook ──▶ Claude Code 处理 ──▶ reply ──▶ Server 存结果
        （long-poll 轮询）                                      │
                                                                ▼
                                              命令行 send --wait / result 取回结果
```

- **Server 运行时**：Stop hook 每次都会 long-poll，会话停不下来，始终待命。
- **Server 停止后**：Stop hook 检测到没在运行，直接放行，会话正常结束。
- 也就是说：**`start` = 开启会话循环，`stop` = 结束会话循环。**

脚本：`.claude/skills/cc-server/scripts/cc_server.py`（纯标准库，无依赖）。

## 启动 / 停止 Server

```bash
# 启动后台 Server（默认端口 8787，可用环境变量 CC_SERVER_PORT 覆盖）
.claude/skills/cc-server/scripts/cc_server.py start

# 查看状态
.claude/skills/cc-server/scripts/cc_server.py status

# 停止（同时结束 Stop hook 的轮询循环）
.claude/skills/cc-server/scripts/cc_server.py stop
```

## 提交请求（命令行侧）

```bash
# 直接传参
.claude/skills/cc-server/scripts/cc_server.py send "帮我查一下 X"

# 长内容用 stdin
cat task.md | .claude/skills/cc-server/scripts/cc_server.py send --stdin

# 提交并阻塞等待 Claude Code 的回复（最多 600s）
.claude/skills/cc-server/scripts/cc_server.py send "..." --wait 600

# 之后再单独取回结果
.claude/skills/cc-server/scripts/cc_server.py result <id> --wait 600
```

## 处理请求（Claude Code 侧）

Stop hook 触发 `poll` 后，Claude Code 会在 stderr 看到类似内容：

```
[CC Server] New request received (id=abc123def456, ts=...):

帮我查一下 X

After handling the request above, run this to send the result back:
  .../cc_server.py reply abc123def456 --stdin <<'EOF'
  <your reply>
  EOF
```

Claude Code 应该：

1. 理解并完成请求里的任务；
2. 用 `reply` 把结果回传：

```bash
.claude/skills/cc-server/scripts/cc_server.py reply <id> "结果文本"
# 或长结果用 stdin
.claude/skills/cc-server/scripts/cc_server.py reply <id> --stdin <<'EOF'
...
EOF
```

回传后会话会再次 stop，Stop hook 继续 long-poll 下一个请求。

## Stop Hook 集成

`.claude/hooks/cc-server-poll.sh` 已经实现，并在 `.claude/settings.json` 的
Stop hook 中注册（timeout 设为 900000ms，poll 用 840s，留出余量）。逻辑：

1. Server 没运行 → `exit 0`，会话正常结束；
2. long-poll 拿到请求 → 输出给 Claude Code，`exit 2` 继续会话；
3. 超时无请求 → `exit 2`，保持轮询循环。

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/submit` | `{prompt}` → `{id}` 入队 |
| GET  | `/poll?wait=N` | 长轮询，`{request}` 或 `{request:null}` |
| POST | `/reply` | `{id, result}` 存结果并唤醒等待方 |
| GET  | `/result?id=X&wait=N` | 取结果，`{result}` 或 `{result:null}` |
| GET  | `/status` | 健康检查、队列长度 |
| POST | `/shutdown` | 优雅关闭 |

## 文件位置

- PID / 端口 / 日志：`~/.config/cc-server/{server.pid,server.port,server.log}`
- 请求队列与结果仅存在 Server 内存中，重启即清空。

## 故障排查

| 问题 | 解决方式 |
|------|---------|
| `start` 失败 | 查看 `~/.config/cc-server/server.log`，可能端口被占用，换 `CC_SERVER_PORT` |
| 会话停不下来 | 这是预期行为（Server 在运行）；`stop` 即可结束循环 |
| `send --wait` 超时 | 请求已入队但还没被处理完，用 `result <id> --wait N` 再等 |
| 进程在但连不上 | `stop` 后重新 `start` |
