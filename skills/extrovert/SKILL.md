---
name: extrovert
description: 当任务需要当前不具备的能力时，自动发现、安装、验证并接入外部 MCP 工具/能力。触发词：装个XX能力、找XX工具、有没有XX的工具、用专业的XX模型、交朋友、缺工具、capability。
---

# extrovert：按需接入外部能力

你（agent）通过 MCP 连接了一个名为 extrovert 的能力中介。它提供五个工具：`capability_search`、`capability_add`、`capability_verify`、`capability_list`、`capability_remove`。本技能教你何时以及如何使用它们。

## 何时触发

满足以下任一条件时，进入本流程：

- 用户明确要求外部能力或工具（"装个视频能力""找个画图工具""有没有操作 XX 的工具"）
- 当前任务卡在缺少工具上（例如需要读某个私有服务、调用某个专业模型，而你现有的工具做不到）
- 用户说出触发词（交朋友、缺工具、capability 等）

不要为已有工具能完成的任务触发本流程。

## 标准流程（按顺序执行）

1. **检索**：调用 `capability_search(query)`，query 用简短英文关键词（注册表以英文为主）。同一会话对同一 query 只检索一次；结果不理想时换关键词，不要重复同一检索。
2. **汇报候选**：用一句中文向用户汇报你选了哪个候选、为什么（例："注册表里找到 `@modelcontextprotocol/server-filesystem`，提供文件读写能力，准备安装"）。
3. **透明安装**：调用 `capability_add(identifier)`。调用前必须告知用户将要运行的命令（`capability_add` 返回结果里的 `command` 字段；注册表服务器通常是 `npx -y <pkg>` 或 `uvx <pkg>`）。若返回的 `warnings` 提到缺少必需的环境变量（API key 等），**暂停并询问用户**，不要继续。
4. **验证**：调用 `capability_verify(alias)`。`passed: true` 才可继续使用。
5. **调用**：使用 `<alias>__<tool>` 形式的新工具完成任务。

## 硬性约束

- **verify 失败必须如实报告**：先 `capability_remove(alias)` 清理，然后明确告诉用户验证没通过、证据是什么（`evidence` 字段）。绝不假装成功，绝不绕过验证直接调用。
- **不要手捏替代实现**：既然走了能力安装流程，就用装上的专业工具完成任务，而不是退回自己写脚本糊弄。
- **安装动作会被审计**：每次 `capability_add`/`capability_remove` 都记录在用户本机的 `~/.extrovert/broker.log`。只装用户在对话中认可的能力。
- **挂载即生效**：`capability_add` 成功后新工具立即可用（客户端会收到 tools/list_changed）。装完不用再 search 同类能力，直接 `capability_list()` 确认现状。
- **重启自动恢复**：验证通过的能力会在下次会话自动重挂载；`capability_list()` 里 `verified: true` 而当前未挂载的条目说明重挂载失败，如实告知用户。
