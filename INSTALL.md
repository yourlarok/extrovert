# 安装 extrovert / Install extrovert

把下面这段提示词原样粘贴到任何支持 MCP 的 agent 对话框里（Claude Code、Kimi Code 等），agent 会自己完成安装与注册：

```text
请帮我安装 extrovert（GitHub: yourlarok/extrovert），一个 MCP 能力中介。步骤：
1) 运行 `pip install git+https://github.com/yourlarok/extrovert.git`（如果你更熟悉 uv，也可以用 `uv tool install git+https://github.com/yourlarok/extrovert.git`）；
2) 运行 `extrovert doctor` 自检，把报告原样给我看；若有 FAIL 项，先帮我修复再继续；
3) 运行 `extrovert install` 把它注册到你的 MCP 配置（先看一眼它打印的计划，确认无误再执行；如果它打印的是需要你手动粘贴的配置片段，就告诉我应该贴到哪个文件，不要擅自改动看不懂的配置）；
4) 告诉我需要重启会话生效。完成后回复"extrovert 已就绪"。
```

Paste the prompt above into any MCP-capable agent. It will install the package, run `extrovert doctor`, register extrovert into its own MCP config via `extrovert install`, and ask you to restart the session.

重启会话后，对 agent 说"装个 XX 能力"或"找个 XX 工具"即可触发能力发现与安装。可选：把仓库里的 `skills/extrovert/` 复制或软链到 `~/.agents/skills/extrovert/`，让 agent 获得更稳定的触发行为。

## 手动安装 / Manual install

```bash
pip install extrovert-mcp    # 或 pip install git+https://github.com/yourlarok/extrovert.git
extrovert doctor             # 自检
extrovert install            # 注册到检测到的客户端；--dry-run 只看计划
```

之后把 `{"mcpServers": {"extrovert": {"command": "extrovert"}}}` 加进任何 MCP 客户端也可以——见 README 的客户端配置小节。
