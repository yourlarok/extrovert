---
name: extrovert
description: 当你正在规划或执行一个任务，而其中某个环节属于通用外部能力（生成图片/视频/音频、OCR 与文档解析、网页爬取与浏览器操作、翻译、调用 SaaS/官方 API、3D 渲染、专业格式转换等）时——先别从零实现，走 extrovert 流程（capability_consider）寻找现成能力。本技能在任务规划阶段即生效，无需用户明确要求。
---

# extrovert：规划期的外部能力自检

你（agent）通过 MCP 连接了一个名为 extrovert 的能力中介，提供六个工具：`capability_consider`、`capability_search`、`capability_add`、`capability_verify`、`capability_list`、`capability_remove`。本技能决定你在**规划阶段**如何使用它们。

## 何时生效

本技能不等待用户口令。它在以下时刻自发生效：

- **规划期**：你刚拆解完一个任务，正在排定实现步骤——对每个子任务问一句"这是通用能力吗？"
- **执行期**：做到一半发现当前步骤需要你没有的能力（读某个私有服务、调某个专业模型、处理某种专业格式）

"通用能力"的判定参考：生成图片/视频/音频、OCR 与文档解析、网页爬取与浏览器操作、翻译、SaaS/官方 API 调用、3D 渲染、专业格式转换、TTS/配音、地图/天气/汇率等公共数据。这些领域里，社区大概率已有现成的 MCP 服务器；你从零写脚本几乎总是更差的选择。

## 规划期自检清单（必做）

拆解任务后、动手之前，对每个子任务执行一遍：

1. 问："这个子任务是通用能力吗？"
2. 是 → 调 `capability_consider(子任务描述)`（描述用简短英文，注册表以英文为主；结果有 10 分钟缓存，放心调用）
3. 否（确属项目特有逻辑）→ 正常自行实现

## 标准流程

1. `capability_consider` 返回建议。有合适候选 → 用一句中文告诉用户你准备装哪个、为什么（例："视频配音这一步，注册表里有现成的 TTS 服务器 `com.example/tts`，我准备装它而不是自己拼 ffmpeg"）。
2. `capability_add(identifier)`。调用前必须告知用户将要运行的命令（结果里的 `command` 字段）。若返回的 `warnings` 提到缺少必需凭据（API key 等），**暂停并询问用户**，不要继续。
3. `capability_verify(alias)`。`passed: true` 才可使用。
4. 用 `<alias>__<tool>` 形式的新工具完成该子任务。
5. 没有候选（或注册表不可达）→ 自行实现，并在最终总结里写明"X 子任务已确认无现成能力，故自行实现"。

## 硬性约束

- **verify 失败必须如实报告**：先 `capability_remove(alias)` 清理，再明确告诉用户验证没通过、`evidence` 是什么。绝不假装成功，绝不绕过验证直接调用。
- **装上就要用**：既然走了能力安装流程，就用装上的专业工具，不要退回自己写脚本糊弄。
- **每次安装都被审计**：`capability_add`/`capability_remove` 记录在用户本机 `~/.extrovert/broker.log`。
- **挂载即生效**：`capability_add` 成功后新工具立即可用；`verified: true` 的能力在下次会话自动重挂载。`capability_list()` 可随时看现状。
- **缓存边界**：consider 结果缓存 10 分钟；刚装完一个能力后不要为同一子任务重复 consider，直接干活。

## 反模式（禁止）

- 用户说"帮我把视频做成解说短片"，你闷头开始写 ffmpeg 脚本——配音/剪辑/字幕是典型的通用能力环节，应先在规划期 `capability_consider`。
- 把 `capability_consider`/`capability_search` 当摆设，规划时从不调用。
- 验证失败后不报告、不清理，直接假装能力可用。
- 因为没有现成能力而自行实现，却不在总结里说明（用户需要知道"确认过没有，不是自己偷懒"）。
