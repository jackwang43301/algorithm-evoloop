from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ..schemas import append_jsonl, ensure_dir, utc_now


NON_INTERACTIVE_REVIEW_MESSAGE = (
    "Human review required, but stdin is not interactive. "
    "Rerun with --human-decision keep|rollback|branch|block."
)


class HumanReview:
    """人工审查入口：把审查请求与角色干预落盘，并在交互终端收集治理决定。

    交互式终端等待输入 keep / rollback / branch / block；非交互环境且无预设决定时返回 block，
    避免静默回退掩盖问题。
    """

    def __init__(
        self,
        workspace: Path,
        preset_decision: str | None = None,
        pause_before: set[str] | None = None,
    ):
        """保存工作区路径、命令行预设决定与需要暂停的角色集合，并计算两个 JSONL 落盘路径。

        preset_decision 来自 --human-decision，优先级最高；pause_before 为 None 时视为空集合。
        """
        self.workspace = workspace
        self.preset_decision = preset_decision
        self.pause_before = pause_before or set()
        self.requests_path = workspace / "human" / "review_requests.jsonl"
        self.interventions_path = workspace / "human" / "role_interventions.jsonl"

    def is_interactive(self) -> bool:
        """判断标准输入是否连接到终端，用于决定能否向人工提问。"""
        return sys.stdin.isatty()

    def request(self, context: dict[str, Any]) -> dict[str, Any]:
        """发起一次人工审查：先把请求追加到 review_requests.jsonl，再确定治理动作并返回结果字典。

        返回值在请求字段之外附加 actor、decision、recommended_action、notes；
        有预设决定时直接采用，交互终端则提示输入，非交互环境返回 block。
        """
        ensure_dir(self.requests_path.parent)
        request = {
            "id": f"human-{context['run_id']}-iter-{int(context['iteration']):03d}-review",
            "run_id": context["run_id"],
            "iteration": context["iteration"],
            "trigger": context["trigger"],
            "verdict": context["evaluation"]["verdict"],
            "reason": context["reflection"]["reason"],
            "created_at": utc_now(),
        }
        append_jsonl(self.requests_path, request)

        if self.preset_decision:
            action = self.preset_decision
            actor = "preset_human_review"
            notes = "使用命令行预设治理动作"
        elif self.is_interactive():
            action = self._prompt_action()
            actor = "human_prompt"
            notes = "人工在交互终端完成审查"
        else:
            action = "block"
            actor = "human_review_unavailable"
            notes = NON_INTERACTIVE_REVIEW_MESSAGE

        return {
            **request,
            "actor": actor,
            "decision": action,
            "recommended_action": action,
            "notes": notes,
        }

    def before_role(self, role: str, run_id: str, iteration: int) -> dict[str, Any] | None:
        """在指定角色执行前设置人工检查点，返回落盘的干预记录，未触发时返回 None。

        仅当该角色在 pause_before 且处于交互终端才提示；可选 continue、guidance、stop，
        输入其他内容会重新提示，guidance 分支还会收集多行指导文本。
        """
        if role not in self.pause_before or not self.is_interactive():
            return None
        print(f"\nHuman checkpoint before {role} (iteration {iteration})")
        print("Choose: continue, guidance, stop")
        while True:
            # 直接回车视为 continue；非法输入不退出循环，持续追问直到得到合法选项
            choice = input("> ").strip().lower() or "continue"
            if choice == "continue":
                guidance = ""
                break
            if choice == "stop":
                guidance = ""
                break
            if choice == "guidance":
                guidance = self._prompt_guidance()
                break
            print("请输入 continue、guidance 或 stop。")

        intervention = {
            "id": f"human-{run_id}-iter-{iteration:03d}-{role}",
            "run_id": run_id,
            "iteration": iteration,
            "role": role,
            "decision": choice,
            "notes": guidance,
            "created_at": utc_now(),
        }
        append_jsonl(self.interventions_path, intervention)
        return intervention

    @staticmethod
    def _prompt_action() -> str:
        """提示人工输入治理动作并归一化为小写；不在四个合法值内时保守地返回 block。"""
        print("Human review required. Choose: keep, rollback, branch, block")
        action = input("> ").strip().lower()
        return action if action in {"keep", "rollback", "branch", "block"} else "block"

    @staticmethod
    def _prompt_guidance() -> str:
        """逐行读取人工给该角色的指导文本，单独一行 END 结束，返回拼接并去掉首尾空白的字符串。"""
        print("输入给该角色的指导；单独输入 END 结束：")
        lines: list[str] = []
        while True:
            line = input()
            if line.strip() == "END":
                return "\n".join(lines).strip()
            lines.append(line)
