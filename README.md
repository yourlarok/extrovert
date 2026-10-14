# extrovert

让"内向"的 AI agent 变外向：一个本地常驻的 MCP 能力代理（capability broker）。agent 只需配置一次 extrovert，之后就能自己检索[官方 MCP Registry](https://registry.modelcontextprotocol.io)、自动安装第三方 MCP 服务器、做冒烟验证，并把新工具**热挂载**进来——全程不需要用户介入，也不需要重启 agent。

Make your "introverted" AI agent extroverted: a local MCP capability broker. Configure extrovert once, and the agent can search the [official MCP Registry](https://registry.modelcontextprotocol.io), install third-party MCP servers, smoke-verify them, and **hot-mount** their tools at runtime — no user intervention, no restarts.

## 架构 / Architecture

```
┌──────────────┐   MCP (stdio, 一次配置)   ┌───────────────────────────────┐
│  AI agent    │ ◄──────────────────────► │           extrovert           │
│ Claude Code  │                          │  capability_search / add /    │
│ Kimi Code …  │   ◄── notifications/     │  verify / list / remove       │
│              │      tools/list_changed  │                               │
└──────────────┘                          │  mounts:                      │
                                          │   fs__*  ──► npx -y <pkg>     │
                                          │   db__*  ──► uvx <pkg>        │
                                          │   echo__* ─► python3 s.py     │
                                          └───────┬───────────┬───────────┘
                                          HTTPS   ▼           ▼ 子进程 (stdio)
                              registry.modelcontextprotocol.io
```

## 工作原理 / How it works

1. **发现 Discover** — `capability_search(query)` 检索官方 MCP Registry，返回结构化候选（名称、描述、版本、npm/pypi 包名）。/ searches the official MCP Registry and returns structured candidates.
2. **安装 Install** — `capability_add(identifier)` 把注册表条目解析成启动命令（npm → `npx -y <pkg>`；pypi → `uvx <pkg>` 或 `python3 -m <pkg>`），以 scrub 过的环境变量启动子进程并完成 MCP 握手。也接受显式本地命令，如 `python3 /path/to/server.py`。/ resolves the entry into a launch command, spawns it with a scrubbed environment, and completes the MCP handshake. Explicit local commands are also accepted.
3. **验证 Verify** — `capability_verify(alias)` 冒烟检查：ping + list_tools，若存在无参数的 echo/ping/hello/version 类安全工具则真实调用一次，证据写入信任库 `~/.extrovert/trust.json`。/ smoke check: ping + list_tools + one safe zero-argument probe call; evidence goes to the trust store.
4. **挂载 Mount** — 下游工具以 `<alias>__<tool>` 立即出现在工具列表中，并通过 `notifications/tools/list_changed` 通知客户端刷新；验证过的服务器会在下次启动时自动重挂载（`~/.extrovert/config.json`）。/ downstream tools appear immediately as `<alias>__<tool>`, the client is told via `notifications/tools/list_changed`, and verified servers auto-remount on next start.

## 安装 / Install

```bash
pip install extrovert-mcp        # 发布后 / once published
# 或从源码 / or from source:
pip install -e .
```

要求 Python ≥ 3.10。注册表里的 npm 服务器需要 `npx`；pypi 服务器优先用 `uvx`（未安装则回退 `python3 -m`，需该包已安装）。

## 客户端配置 / Client configuration

**Claude Code**

```bash
claude mcp add extrovert -- extrovert
```

或写入 `~/.claude.json` / 项目 `.mcp.json`：

```json
{
  "mcpServers": {
    "extrovert": { "command": "extrovert" }
  }
}
```

**Kimi Code**

写入 `~/.kimi-code/mcp.json`（或 `$KIMI_CODE_HOME/mcp.json`）：

```json
{
  "mcpServers": {
    "extrovert": { "command": "extrovert" }
  }
}
```

之后 agent 自己完成其余事情："帮我找一个能操作文件系统的 MCP 服务器并装上" → `capability_search` → `capability_add` → `capability_verify` → 直接用 `fs__*` 工具。

## 工具 / Tools

| 工具 | 作用 |
|---|---|
| `capability_search(query, limit=5)` | 检索官方注册表；网络不可用时返回明确错误 |
| `capability_add(identifier, alias=None)` | 安装并挂载（注册表名如 `com.example/foo`，或本地命令）；热挂载并发通知 |
| `capability_verify(alias)` | ping + list_tools + 无参安全工具探针；结果写入信任库 |
| `capability_list()` | 列出挂载的服务器、工具、健康与验证状态、来源 |
| `capability_remove(alias)` | 终止子进程、注销工具、更新配置并发通知 |

## 数据文件 / State files

均在 `~/.extrovert/`（可用环境变量 `EXTROVERT_HOME` 覆盖）：

- `config.json` — 已挂载服务器列表与 `env_passthrough` 白名单追加项
- `trust.json` — 验证记录（时间戳、通过与否、证据摘要）
- `broker.log` — JSON Lines 审计日志（安装/验证/移除/重挂载事件）
- `logs/<alias>.stderr.log` — 各下游子进程的 stderr

## 安全说明 / Security

威胁模型（请务必读）：

- **`capability_add` 会执行第三方代码。** 安装来源仅限官方注册表条目或显式给出的本地命令，且每次安装都会写审计日志——但注册表条目不等于安全。
- **子进程默认拿不到你的环境变量。** 只透传 `PATH`/`HOME`/`LANG` 等白名单变量（`API KEY`、令牌之类默认不可见）；需要时可在 `config.json` 的 `env_passthrough` 里显式追加。
- **探针验证是活性检查，不是安全审计。** 它只证明服务器能应答，不证明它 benign。
- 挂载的第三方服务器拥有其进程权限内的一切能力；请只安装你信任的发布者的服务器。

Threat model (please read): `capability_add` executes third-party code. Sources are restricted to official-registry entries or explicit local commands, and every install is audit-logged — but registry presence is not a safety guarantee. Subprocesses only see a whitelist of environment variables (API keys and tokens are withheld by default; opt in via `env_passthrough`). Probe verification is a liveness check, not a security audit. Only install servers from publishers you trust.

## 路线图 / Roadmap

- **A2A Agent Card 发现** — 除 MCP Registry 外，支持发现 A2A 协议的 Agent Card 并桥接调用。
- **技能库沉淀** — 把验证过的用法、参数模板沉淀为可复用的技能描述，减少重复探索。
- **探针验证强化** — 更丰富的只读探针、输出模式校验、挂载前的静态启发式检查。

## 开发 / Development

```bash
pip install -e ".[dev]"
python3 -m pytest tests/ -x -q
```

测试覆盖：注册表响应解析（mock 数据）、本地 echo 服务器挂载与代理调用、verify/remove/remount、断线错误路径，以及一个真实拉起 extrovert 进程的端到端集成测试（网络可用时会真实检索官方注册表）。

### 已知限制 / Known limitations

- 仅支持 stdio 传输；registry 中仅提供 remote（streamable-http）端点的服务器暂不支持。
- 代理工具的入参 schema 由下游 JSON Schema 综合而成：嵌套 object/array 的内部结构在代理侧不展开（下游服务器仍做权威校验）。
- 可选参数未传时不会向下游发送显式 `null`。
- 首个挂载超时默认 90 秒（`EXTROVERT_MOUNT_TIMEOUT` 可调），npm 包首次下载可能较慢。

## License

MIT © yourlarok
