from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm_client import LLMClient, LLMRequestError
from .prompt_builder import PromptBuilder
from .schemas import metric_is_better, read_json, role_json_schema, validate_role_record, write_json


class RoleExecutionError(RuntimeError):
    """角色执行失败：role 标明出错角色，raw 保留可落盘的原始上下文（尝试记录、响应文本等）。"""

    def __init__(self, role: str, message: str, raw: dict[str, Any] | None = None):
        """保存角色名与原始上下文，raw 缺省时置为空字典，便于上层统一写日志。"""
        self.role = role
        self.raw = raw or {}
        super().__init__(message)


class RoleRunner(ABC):
    """角色执行底座的抽象接口：负责产出角色记录文件，并把它读回为结构化字典。"""

    @abstractmethod
    def execute(
        self,
        role: str,
        prompt: str,
        workspace: Path,
        iter_dir: Path,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """执行一个角色并把记录写入 iter_dir，返回原始运行信息（引擎类型、可用技能等）；子类必须实现。"""
        raise NotImplementedError

    def write_log(self, role: str, raw: dict[str, Any], iter_dir: Path) -> None:
        """可选的原始日志落盘钩子，默认不做任何事，返回 None。"""
        return None

    @abstractmethod
    def collect(self, role: str, raw: dict[str, Any], iter_dir: Path, workspace: Path) -> dict[str, Any]:
        """读取并校验该角色写出的记录，返回可供流程后续使用的字典；子类必须实现。"""
        raise NotImplementedError


class SampleRunner(RoleRunner):
    """确定性测试底座：不调用大模型，按上下文推导固定记录；公开 CLI 有意不暴露它。"""

    def execute(
        self,
        role: str,
        prompt: str,
        workspace: Path,
        iter_dir: Path,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """依据 context 生成角色记录、校验后写入 iter_dir；context 为 None 时抛 RoleExecutionError。"""
        if context is None:
            raise RoleExecutionError(role, "sample runner requires role context")
        record = self._record(role, context)
        validate_role_record(role, record)
        write_json(iter_dir / self._record_name(role), record)
        return {"engine": "sample", "available_skills": ["code-review-skill"]}

    def collect(self, role: str, raw: dict[str, Any], iter_dir: Path, workspace: Path) -> dict[str, Any]:
        """从 iter_dir 读回该角色的记录并再次校验后原样返回，不做任何回填。"""
        record = read_json(iter_dir / self._record_name(role))
        validate_role_record(role, record)
        return record

    def _record(self, role: str, context: dict[str, Any]) -> dict[str, Any]:
        """按角色构造确定性记录：规划者产出假设与步骤，执行者回放 fixture 指标，评估者比对指标与证据，
        反思者依据评估结论给出 keep / rollback / request_human_review。"""
        if role == "planner":
            memory_ids = [
                item["id"]
                for items in context.get("memory", {}).values()
                for item in items
                if isinstance(item, dict) and item.get("id")
            ]
            evidence_step = "补齐并核对完整证据" if memory_ids else "收集指标、日志和证据"
            return {
                "iteration_id": context["iteration_id"],
                "objective": context["objective"],
                "hypothesis": "采用一项小而可验证的改动可以推进目标。",
                "steps": ["选择一个小改动", "调用 mock_experiment_runner", evidence_step],
                "success_criteria": ["分数优于当前 champion", "证据完整", "代码审核通过"],
                "memory_used": memory_ids,
            }
        if role == "executor":
            fixture = context["fixture"]
            tool = context["tools"][0]
            return {
                "change_summary": fixture["change_summary"],
                "tool_calls": [
                    {
                        "name": tool["name"],
                        "mode": "mock",
                        "status": "success",
                        "result": fixture["tool_result"],
                    }
                ],
                "raw_metrics": {"score": fixture["score"]},
                "logs": [fixture["tool_result"]],
                "artifacts": ["artifacts/evidence.json"] if fixture["evidence_available"] else [],
            }
        if role == "evaluator":
            execution = context["execution"]
            metric = context["metric"]
            candidate = float(execution["raw_metrics"]["score"])
            champion = float(context["champion"]["score"])
            evidence = context["evidence"]
            available = set(evidence["available_paths"])
            missing = [path for path in evidence["required_paths"] if path not in available]
            evidence_complete = not missing
            # 判定优先级：证据缺失优先于指标比较，缺证据一律不判改善
            if missing:
                verdict = "insufficient_evidence"
            elif metric_is_better(candidate, champion, metric["direction"], float(metric.get("epsilon", 0.0))):
                verdict = "improved"
            else:
                verdict = "no_improvement"
            return {
                "metric": {
                    "name": metric["name"],
                    "candidate": candidate,
                    "champion": champion,
                    "direction": metric["direction"],
                },
                "evidence_complete": evidence_complete,
                "verdict": verdict,
                "tool_calls": [
                    {
                        "name": context["evaluator_tools"][0]["name"],
                        "mode": context["evaluator_tools"][0]["mode"],
                        "status": "success",
                        "result": "已比较原始指标并检查必要证据。",
                    }
                ],
                "skill_calls": [
                    {
                        "name": context["evaluator_skills"][0]["name"],
                        "skill_loaded": True,
                        "result": "代码审核通过",
                    }
                ],
                "code_review": {"passed": True, "result": "代码审核通过"},
                "evidence_paths": evidence["available_paths"],
                "missing_evidence": missing,
                "human_review": {
                    "required": verdict == "insufficient_evidence",
                    "trigger": "insufficient_evidence" if verdict == "insufficient_evidence" else None,
                },
            }
        evaluation = context["evaluation"]
        if evaluation["verdict"] == "improved":
            return {
                "proposed_action": "keep",
                "reason": "指标改善、证据完整且代码审核通过。",
                "memory_updates": [{"type": "insight", "text": "保留有完整证据的小改动。"}],
                "human_review": {"required": False, "trigger": None},
            }
        if evaluation["verdict"] == "insufficient_evidence":
            return {
                "proposed_action": "request_human_review",
                "reason": "候选证据不足，不能自动保留。",
                "memory_updates": [
                    {"type": "failure", "text": "本轮候选因证据不足无法确认。"},
                    {"type": "do_not_repeat", "text": "不要再次提交证据不完整的候选。"},
                ],
                "human_review": {"required": True, "trigger": "insufficient_evidence"},
            }
        return {
            "proposed_action": "rollback",
            "reason": "候选没有改善当前 champion。",
            "memory_updates": [{"type": "failure", "text": "本轮候选没有带来指标改善。"}],
            "human_review": {"required": False, "trigger": None},
        }

    @staticmethod
    def _record_name(role: str) -> str:
        """把角色名映射到记录文件名（plan/execution/evaluation/reflection.json）；未知角色抛 KeyError。"""
        return {
            "planner": "plan.json",
            "executor": "execution.json",
            "evaluator": "evaluation.json",
            "reflector": "reflection.json",
        }[role]


@dataclass
class LLMRunner(RoleRunner):
    """正常运行底座：把四个角色统一交给可配置的对话补全接口驱动，并支持结构不合规时的修复重试。"""

    client: LLMClient
    prompt_builder: PromptBuilder
    skill_root: Path
    repair_attempts: int = 1
    extra_skill_names: list[str] = field(default_factory=list)

    def execute(
        self,
        role: str,
        prompt: str,
        workspace: Path,
        iter_dir: Path,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用模型产出角色记录并写盘，返回含 attempts/response_text/available_skills 的原始信息；
        接口失败或重试后仍不合规时抛 RoleExecutionError 并带上该原始信息。"""
        # 只有评估者需要注入技能正文，其余角色不加载技能
        skills = discover_skills(self.skill_root) if role == "evaluator" else []
        system_prompt = self._system_prompt(role, skills)
        schema = role_json_schema(role)
        record_path = iter_dir / SampleRunner._record_name(role)
        started_at = time.monotonic()
        attempts: list[dict[str, Any]] = []
        user_prompt = prompt
        record: dict[str, Any] | None = None
        reply = ""
        problem = ""
        for _ in range(self.repair_attempts + 1):
            try:
                result = self.client.complete(system_prompt, user_prompt, schema, f"{role}_record")
            except LLMRequestError as exc:
                attempts.extend(exc.attempts)
                raise RoleExecutionError(
                    role, f"大模型接口调用失败：{exc}", self._raw(role, attempts, skills, reply, started_at)
                ) from exc
            attempts.extend(result["attempts"])
            reply = result["text"]
            candidate = result["record"]
            try:
                validate_role_record(role, candidate)
            except (TypeError, ValueError) as exc:
                # 结构不合规：把错误与上一次输出拼成修复提示词，再试一轮
                problem = str(exc)
                user_prompt = self._repair_prompt(prompt, candidate, problem)
                continue
            record = candidate
            break
        raw = self._raw(role, attempts, skills, reply, started_at)
        if record is None:
            raise RoleExecutionError(role, f"大模型返回的结构化记录不合规：{problem}", raw)
        write_json(record_path, record)
        return raw

    def write_log(self, role: str, raw: dict[str, Any], iter_dir: Path) -> None:
        """把原始运行信息写入 iter_dir/.llm_logs/<role>.json，便于事后排查调用过程。"""
        write_json(iter_dir / ".llm_logs" / f"{role}.json", raw)

    def collect(self, role: str, raw: dict[str, Any], iter_dir: Path, workspace: Path) -> dict[str, Any]:
        """读回角色记录并校验后回写；评估者的 skill_calls 会按实际可用技能回填 skill_loaded。
        记录不是字典时抛 RoleExecutionError。"""
        record_path = iter_dir / SampleRunner._record_name(role)
        record = read_json(record_path)
        if not isinstance(record, dict):
            raise RoleExecutionError(role, f"invalid role record: {record_path.name}", raw)
        if role == "evaluator":
            # 模型自称调用过的技能未必真的加载，这里用本轮实际可用技能名单纠正
            available = set(raw.get("available_skills", []))
            for skill_call in record.get("skill_calls", []):
                skill_call["skill_loaded"] = skill_call.get("name") in available
        validate_role_record(role, record)
        write_json(record_path, record)
        return record

    def _system_prompt(self, role: str, skills: list[dict[str, str]]) -> str:
        """生成角色系统提示词；若有技能，则把每个技能正文以「## 技能：名称」块追加到提示词末尾。"""
        system_prompt = self.prompt_builder.system_prompt(role)
        if not skills:
            return system_prompt
        blocks = [f"## 技能：{skill['name']}\n{skill['body']}" for skill in skills]
        return system_prompt + "\n\n# 可用技能说明\n\n" + "\n\n".join(blocks)

    @staticmethod
    def _repair_prompt(prompt: str, candidate: dict[str, Any], problem: str) -> str:
        """拼出修复提示词：原提示词 + 校验错误 + 截断到 4000 字符的上一次输出，并要求只输出纯 JSON 对象。"""
        previous = json.dumps(candidate, ensure_ascii=False)[:4000]
        return (
            f"{prompt}\n\n# 上一次输出不合规\n"
            f"错误：{problem}\n上一次输出：{previous}\n"
            "请只输出符合结构约定的 JSON 对象，不要包含解释文字或代码围栏。"
        )

    def _raw(
        self,
        role: str,
        attempts: list[dict[str, Any]],
        skills: list[dict[str, str]],
        reply: str,
        started_at: float,
    ) -> dict[str, Any]:
        """汇总本轮原始信息：脱敏后的配置、逐次尝试记录、模型回复文本、耗时秒数，以及可用技能名单
        （发现的技能名加上 extra_skill_names）。"""
        return {
            "engine": "llm",
            "role": role,
            "config": self.client.config.redacted(),
            "attempts": attempts,
            "response_text": reply,
            "duration_seconds": round(time.monotonic() - started_at, 3),
            "available_skills": [skill["name"] for skill in skills] + list(self.extra_skill_names),
        }


def discover_skills(skill_root: Path) -> list[dict[str, str]]:
    """读取 `<skill_root>/*/SKILL.md`，把已声明技能注入提示词；返回 [{"name", "body"}]，按路径排序。"""
    root = Path(skill_root).expanduser()
    skills: list[dict[str, str]] = []
    for skill_file in sorted(root.glob("*/SKILL.md")):
        text = skill_file.read_text(encoding="utf-8")
        name = skill_file.parent.name
        body = text
        if text.startswith("---"):
            # 有 YAML 前置区块时用其中的 name 覆盖目录名，正文只取前置区块之后的部分
            parts = text.split("---", 2)
            if len(parts) == 3:
                for line in parts[1].splitlines():
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip()
                body = parts[2]
        skills.append({"name": name, "body": body.strip()})
    return skills


DemoRunner = SampleRunner
