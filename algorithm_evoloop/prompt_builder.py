from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class PromptBuilder:
    """按角色拼装提示词：只给出该角色需要的当轮信息，不暴露框架内部字段。"""

    def __init__(self, prompts_dir: Path):
        """记录提示词目录，其中应包含 `_shared_header.md` 与四个角色各自的 Markdown 文件。"""
        self.prompts_dir = prompts_dir.resolve()

    def system_prompt(self, role: str) -> str:
        """返回该角色的系统提示词，即共享说明加角色说明拼接后的文本。"""
        shared = (self.prompts_dir / "_shared_header.md").read_text(encoding="utf-8")
        role_prompt = (self.prompts_dir / f"{role}.md").read_text(encoding="utf-8")
        return shared.rstrip() + "\n\n" + role_prompt.rstrip() + "\n"

    def dynamic_prompt(self, role: str, context: dict[str, Any]) -> str:
        """返回该角色的当轮提示词：裁剪后的上下文以 JSON 形式给出，并要求直接返回结构化对象。"""
        payload = self._compact_context(role, context)
        skill_command = ""
        # 评估者需要显式声明使用哪个技能，技能正文由框架读取后注入系统提示词
        if role == "evaluator":
            skills = payload.get("evaluator_skills", [])
            if skills:
                skill_command = f"使用技能：{skills[0]['name']}\n\n"
        return (
            skill_command
            + "请根据下面的当轮信息完成本角色任务，并直接返回符合 JSON Schema 的对象。\n"
            + "字段值优先使用中文；不要输出 Markdown，不要读写文件，不要补充框架内部字段。\n\n"
            + "```json\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n```\n"
        )

    def _compact_context(self, role: str, context: dict[str, Any]) -> dict[str, Any]:
        """按角色白名单裁剪上下文：去掉空值，擂主只保留指标，规划者的记忆再做压缩。"""
        allowed = {
            "planner": {"iteration_id", "objective", "metric", "champion", "rules", "memory", "human_guidance"},
            "executor": {"iteration_id", "plan", "tools", "fixture", "human_guidance"},
            "evaluator": {
                "metric",
                "champion",
                "execution",
                "evidence",
                "evaluator_tools",
                "evaluator_skills",
                "evaluation_rules",
                "human_guidance",
            },
            "reflector": {"evaluation", "consecutive_failures", "human_review_rules", "human_guidance"},
        }[role]
        compact = {key: context[key] for key in allowed if key in context and context[key] not in (None, "")}
        champion = compact.get("champion")
        if isinstance(champion, dict):
            compact["champion"] = {"score": champion["score"]} if "score" in champion else {}
        if role == "planner" and isinstance(compact.get("memory"), dict):
            compact["memory"] = {
                key: self._compact_memory(items)
                for key, items in compact["memory"].items()
                if isinstance(items, list) and items
            }
        return self._json_safe(compact)

    @staticmethod
    def _compact_memory(items: list[Any]) -> list[Any]:
        """每类长期记忆只保留最近 5 条，并且每条只保留少量可读字段，避免提示词膨胀。"""
        compact: list[Any] = []
        for item in items[-5:]:
            if not isinstance(item, dict):
                compact.append(item)
                continue
            compact.append({key: item[key] for key in ("id", "iteration", "text", "decision", "notes") if key in item})
        return compact

    def _json_safe(self, value: Any) -> Any:
        """递归把上下文转成可序列化的形式：路径只保留文件名，避免泄露本机目录结构。"""
        if isinstance(value, Path):
            return value.name
        if isinstance(value, dict):
            return {key: self._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._json_safe(item) for item in value]
        return value
