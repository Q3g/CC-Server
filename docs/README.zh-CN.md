# CC Server — 基于 Claude Code 会话循环的请求 Server

> English: [../README.md](../README.md)

把一个正在运行的 Claude Code 会话变成一个**常驻服务**:你从命令行（或任何能发 HTTP 的地方）提交请求,Claude Code 在空闲时自动取走、处理、回传结果,然后继续待命等下一个。

## 使用流程

顺序很关键 —— 必须 **Server 在运行**,**且有一个 Claude Code 会话活着**,请求才会被取走。用两个终端:

**终端 A —— 先启动 Server,再把 Claude Code 跑起来:**

```bash
cd /path/to/cc_server
S=.claude/skills/cc-server/scripts/cc_server.py

# 1. 启动后台 Server（默认端口 8787）
$S start

# 2. 在本项目里跑 Claude Code。会话一旦空闲下来，它的 Stop hook
#    就会开始长轮询 Server —— 会话进入待命状态。
claude
```

**终端 B —— 提交请求、取回结果:**

```bash
cd /path/to/cc_server
S=.claude/skills/cc-server/scripts/cc_server.py

# 3. 提交一个请求 —— 会打印出一个 id
$S send "你有哪些 skills？"
#    → ✅ Request submitted (id=c85286b7e009)

# 4. 等终端 A 的会话处理完后，取回结果
$S result c85286b7e009 --wait 600
```

`$S send "..." --wait 600` 也可以一步完成"提交并阻塞等回复"。用完后,在任一终端 `$S stop` 关掉 Server,会话循环随之释放。

## 缘起

Claude Code 自身的 Stop hook 有一个特性:hook 以 `exit 2` 退出时,会把 stderr 的内容反馈给模型并**让会话继续**,而不是真正停止。本项目把这个特性做成一个本地 HTTP 队列服务:

- Stop hook 不再是"做完就停",而是"做完就去轮询有没有新活儿"。
- 只要 Server 在运行,会话就停不下来,始终待命。
- Server 一停,Stop hook 直接放行,会话正常结束。

也就是说:**启动 Server = 开启会话循环,停止 Server = 结束会话循环。**

## 工作原理

```
   命令行 ── send ──▶  HTTP Server（内存请求队列）
                            ▲   │
                     poll   │   │  返回一个请求
        ┌───────────────────┘   ▼
   Claude Code Stop hook ──▶ Claude Code 处理任务 ──▶ reply ──▶ Server 存结果
        （长轮询，exit 2                                            │
         把请求喂回模型）                                           ▼
                                       命令行  send --wait / result  取回结果
```

一次完整往返:

1. **提交** — `cc_server.py send "..."` 把请求 POST 到 Server,入队,拿到一个 `id`。
2. **取走** — Claude Code 会话停下时,Stop hook 跑 `cc_server.py poll`,长轮询 Server。
3. **喂回** — `poll` 拿到请求后打印到 stderr,hook 以 `exit 2` 退出,请求内容就成了模型的新输入。
4. **处理** — Claude Code 理解并完成请求里的任务。
5. **回传** — Claude Code 跑 `cc_server.py reply <id> ...`,结果存回 Server。
6. **取回** — 提交方用 `send --wait` 阻塞等待,或事后用 `result <id>` 查询。
7. 会话再次停下 → 回到第 2 步,继续轮询下一个请求。

## 目录结构

```
cc_server/
├── README.md                            # 英文文档
├── docs/
│   ├── README.zh-CN.md                  # 中文 README（本文件）
│   └── SKILL.zh-CN.md                   # 中文 skill 说明
└── .claude/
    ├── settings.json                    # 注册 Stop hook
    ├── hooks/
    │   └── cc-server-poll.sh            # Stop hook：长轮询 Server
    └── skills/
        └── cc-server/
            ├── SKILL.md                 # 给 Claude Code 看的用法说明
            └── scripts/
                └── cc_server.py         # HTTP Server + CLI（纯标准库，无依赖）
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `start [--port N]` | 启动后台 Server 守护进程 |
| `stop` | 停止 Server（守护进程收到 SIGTERM 优雅退出） |
| `serve [--port N]` | 前台运行 Server（`start` 内部调用,调试用） |
| `status [--quiet]` | 查看状态;`--quiet` 不输出,仅用退出码表示是否运行 |
| `send <prompt>` | 提交请求。`--stdin` 从标准输入读,`--wait N` 阻塞等回复最多 N 秒 |
| `poll [--wait N]` | 长轮询一个请求(Stop hook 调用,默认等 840 秒) |
| `reply <id>` | 回传某个请求的处理结果。`--stdin` 从标准输入读 |
| `result <id> [--wait N]` | 查询某个请求的结果,`--wait` 可阻塞等待 |

示例:

```bash
# 长内容用 stdin 提交
cat task.md | $S send --stdin --wait 900

# Claude Code 侧回传长结果
$S reply 5114cf20e779 --stdin <<'EOF'
这是处理结果……
EOF

# 事后查询某个请求的结果
$S result 5114cf20e779 --wait 600
```

## HTTP 接口

Server 监听 `127.0.0.1:8787`(仅本机),纯 JSON:

| 方法 | 路径 | 请求 / 响应 |
|------|------|-------------|
| `POST` | `/submit` | `{prompt}` → `{id}`,请求入队（提交后即返回） |
| `POST` | `/submit?wait=N` | `{prompt}` → `{id,result}` —— **同步**:提交并阻塞最多 N 秒直接拿到答案,不用再走 `/result` |
| `GET`  | `/poll?wait=N` | 长轮询 → `{request:{id,prompt,ts}}` 或 `{request:null}` |
| `POST` | `/reply` | `{id,result}` → `{ok:true}`,存结果并唤醒等待方 |
| `GET`  | `/result?id=X&wait=N` | → `{result}` 或 `{result:null}` |
| `GET`  | `/status` | → `{ok,pid,queued,pending_results}` |
| `POST` | `/shutdown` | → `{ok:true}`,优雅关闭 |

所以不依赖 CLI 也行,任何能发 HTTP 的程序/脚本都能往会话里塞任务。要"一问一答"式的同步调用,一个 `POST /submit?wait=N` 就够了(`send --wait N` 内部就是走的它)。

## Stop Hook 集成

`.claude/settings.json` 里已注册:

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

- 用 `$CLAUDE_PROJECT_DIR`(Claude Code 注入的项目根路径),不含任何机器相关的绝对路径。
- `timeout` 设 900000ms(15 分钟),hook 内 `poll --wait 840`(14 分钟),留出余量。

`cc-server-poll.sh` 的逻辑:

1. Server 没运行 → `exit 0`,会话正常结束。
2. 长轮询拿到请求 → 打印到 stderr,`exit 2`,把请求喂回模型并继续会话。
3. 超时无请求 → `exit 2`,保持轮询循环。

> 调试时可用环境变量 `CC_SERVER_POLL_WAIT` 覆盖 840 秒的等待窗口,例如 `CC_SERVER_POLL_WAIT=3` 让 hook 只等 3 秒。

## 设计要点

### 长轮询为什么放在服务端、又必须分片检测断连

`/poll` 由服务端持有连接、阻塞等待请求到来(服务端推,延迟低)。但如果直接用 `queue.Queue.get(timeout=wait)` 一次性阻塞整个窗口,会有一个隐蔽的严重缺陷:

> 长轮询客户端(被 kill 掉的 hook、被中断的进程)断开后,`queue.get()` 感知不到 socket 已死,处理线程会**继续阻塞**。等下一个请求入队时,这个**僵尸线程会把它抢走**,然后写进一个已经关闭的连接(`BrokenPipeError`)——请求就这么丢了。

因此 `/poll` 的实现是:

- 每秒一轮 `get(timeout=1.0)`,每轮之间用 `select` + `MSG_PEEK` 检测客户端是否还在(`_client_alive()`),断了就立刻退出,**绝不消费队列**。
- 万一在 `get` 成功之后、`_send` 之前客户端刚好死掉,把请求**重新入队**,不丢。

任何阻塞式长轮询服务都该注意这个竞态。

### 其它

- **零依赖** — `cc_server.py` 只用 Python 标准库(`http.server`、`queue`、`threading`、`select` 等),不需要 `pip install`。
- **并发** — `ThreadingHTTPServer`,每个连接一个线程,`submit` / `poll` / `reply` 互不阻塞。
- **状态在内存** — 请求队列和结果只存在 Server 进程内存里,**重启即清空**;不做持久化是有意为之(请求是"此刻交给会话的活儿",过期无意义)。
- **守护进程** — `start` 用 `subprocess.Popen(..., start_new_session=True)` 把 `serve` 拉成独立后台进程,pid / 端口 / 日志写到 `~/.config/cc-server/`。`stop` 发 `SIGTERM`,`serve` 的 `SIGTERM` 处理器在独立线程里调 `httpd.shutdown()`(避免和 `serve_forever` 死锁)。

## 文件与配置

| 路径 | 用途 |
|------|------|
| `~/.config/cc-server/server.pid` | 守护进程 PID |
| `~/.config/cc-server/server.port` | 实际监听端口 |
| `~/.config/cc-server/server.log` | 服务端日志(`start` 失败时先看这里) |

环境变量:

| 变量 | 默认 | 说明 |
|------|------|------|
| `CC_SERVER_PORT` | `8787` | Server 监听端口 |
| `CC_SERVER_POLL_WAIT` | `840` | hook 脚本里 `poll` 的等待秒数(调试用) |

## 故障排查

| 问题 | 解决方式 |
|------|---------|
| `start` 失败 | 看 `~/.config/cc-server/server.log`;多半是端口被占,换 `CC_SERVER_PORT` |
| 会话停不下来 | 这是预期行为(Server 在运行);`cc_server.py stop` 即可结束循环 |
| `send --wait` 超时 | 请求已入队但还没处理完,用 `result <id> --wait N` 继续等 |
| 进程在但连不上 | `stop` 后重新 `start` |
| `status` 显示"进程存在但无法连接" | 守护进程僵死,`stop` 清理后重启 |

## 适用场景

- **挂机派活** — 离开电脑前 `start`,然后从手机/脚本/定时任务往会话里塞任务,回来收结果。
- **跨进程驱动 Claude Code** — 任何程序都能通过 HTTP 把任务交给一个真实的 Claude Code 会话处理。
- **串行任务队列** — 多个请求排队,会话一个接一个处理完。
