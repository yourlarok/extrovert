"""``extrovert hint``: one-line planning reminder for prompt-submit hooks.

Agent clients that support hooks (e.g. Claude Code's UserPromptSubmit) can
inject this line into the conversation context so the planning-phase
capability check fires even without the skill pack installed.
"""

from __future__ import annotations

HINT_TEXT = (
    "规划任务时：若某环节是通用外部能力（图/视频/音频/文档/爬取/SaaS），"
    "先调 capability_consider，不要从零手搓。"
)


def main() -> int:
    print(HINT_TEXT)
    return 0
