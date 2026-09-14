from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# 四个角色交接记录中使用的受控枚举：评估结论、反思建议动作、最终治理决定、记忆条目类型。
VERDICTS = {"improved", "no_improvement", "inconclusive", "insufficient_evidence"}
PROPOSED_ACTIONS = {"keep", "rollback", "branch", "block", "request_human_review"}
FINAL_DECISIONS = {"keep", "rollback", "branch", "block"}
MEMORY_TYPES = {"insight", "failure", "do_not_repeat"}


def role_json_schema(role: str) -> dict[str, Any]:
    """返回指定角色交接记录的 JSON Schema（已强制 additionalProperties=False）。

    role 取值须为 planner / executor / evaluator / reflector 之一，否则抛出 ValueError。
    """
    string_list = {"type": "array", "items": {"type": "string"}}
    artifact_path_list = {
        "type": "array",
        "items": {"type": "string", "pattern": r"^artifacts/[A-Za-z0-9._/-]+$"},
    }
    tool_calls = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "mode": {"type": "string"},
                "status": {"type": "string"},
                "result": {"type": "string"},
            },
            "required": ["name", "mode", "status", "result"],
        },
    }
    schemas: dict[str, dict[str, Any]] = {
        # 规划者记录：必填迭代号、目标、假设、步骤、成功判据与引用到的记忆条目。
        # 这里刻意不含任何度量数值或结论字段，规划阶段只描述“打算怎么做”。
        "planner": {
            "type": "object",
            "properties": {
                "iteration_id": {"type": "string"},
                "objective": {"type": "string"},
                "hypothesis": {"type": "string"},
                "steps": string_list,
                "success_criteria": string_list,
                "memory_used": string_list,
            },
            "required": [
                "iteration_id",
                "objective",
                "hypothesis",
                "steps",
                "success_criteria",
                "memory_used",
            ],
        },
        # 执行者记录：必填改动摘要、至少一次工具调用、原始度量、日志与产物路径。
        # 产物路径受 artifact_path_list 约束，只允许 artifacts/ 前缀；不含 verdict 等
        # 结论字段，因为“改动是否算改进”只由评估者判定，执行者不得自评。
        "executor": {
            "type": "object",
            "properties": {
                "change_summary": {"type": "string"},
                "tool_calls": tool_calls,
                "raw_metrics": {"type": "object"},
                "logs": string_list,
                "artifacts": artifact_path_list,
            },
            "required": ["change_summary", "tool_calls", "raw_metrics", "logs", "artifacts"],
        },
        # 评估者记录：必填度量对比（候选/冠军/优化方向）、证据是否完整、结论 verdict、
        # 工具与 skill 调用、代码评审结论、证据路径、缺失证据与人工复核标记。
        # verdict 只能取 VERDICTS 内的值，结论必须由证据支撑，故 evidence_paths 必填。
        "evaluator": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "candidate": {"type": "number"},
                        "champion": {"type": "number"},
                        "direction": {"enum": ["maximize", "minimize"]},
                    },
                    "required": ["name", "candidate", "champion", "direction"],
                },
                "evidence_complete": {"type": "boolean"},
                "verdict": {"enum": sorted(VERDICTS)},
                "tool_calls": tool_calls,
                "skill_calls": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "skill_loaded": {"type": "boolean"},
                            "result": {"type": "string"},
                        },
                        "required": ["name", "skill_loaded", "result"],
                    },
                },
                "code_review": {
                    "type": "object",
                    "properties": {
                        "passed": {"type": "boolean"},
                        "result": {"type": "string"},
                    },
                    "required": ["passed", "result"],
                },
                "evidence_paths": string_list,
                "missing_evidence": string_list,
                "human_review": {
                    "type": "object",
                    "properties": {
                        "required": {"type": "boolean"},
                        "trigger": {"type": ["string", "null"]},
                    },
                    "required": ["required", "trigger"],
                },
            },
            "required": [
                "metric",
                "evidence_complete",
                "verdict",
                "tool_calls",
                "skill_calls",
                "code_review",
                "evidence_paths",
                "missing_evidence",
                "human_review",
            ],
        },
        # 反思者记录：必填治理建议 proposed_action、理由、记忆更新列表与人工复核标记。
        # proposed_action 限定在 PROPOSED_ACTIONS 内；这里只“建议”而非最终决定，
        # 最终决定由引擎（必要时叠加人工复核）在 FINAL_DECISIONS 范围内落定。
        "reflector": {
            "type": "object",
            "properties": {
                "proposed_action": {"enum": sorted(PROPOSED_ACTIONS)},
                "reason": {"type": "string"},
                "memory_updates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"enum": sorted(MEMORY_TYPES)},
                            "text": {"type": "string"},
                        },
                        "required": ["type", "text"],
                    },
                },
                "human_review": {
                    "type": "object",
                    "properties": {
                        "required": {"type": "boolean"},
                        "trigger": {"type": ["string", "null"]},
                    },
                    "required": ["required", "trigger"],
                },
            },
            "required": ["proposed_action", "reason", "memory_updates", "human_review"],
        },
    }
    if role not in schemas:
        raise ValueError(f"Unknown role: {role}")
    schemas[role]["additionalProperties"] = False
    return schemas[role]


def validate_role_record(role: str, record: dict[str, Any]) -> None:
    """校验角色记录：必须是 dict、必填键齐全、且不含 Schema 之外的多余键。

    随后按角色做细化检查（执行者产物前缀、评估者 verdict 与子对象、反思者动作枚举等）。
    校验通过返回 None，任何不合规都抛出 ValueError 并在消息中指出角色与具体键名。
    """
    if not isinstance(record, dict):
        raise ValueError(f"{role} record must be an object")
    required = role_json_schema(role)["required"]
    for key in required:
        if key not in record:
            raise ValueError(f"{role} record missing required key: {key}")
    allowed = set(role_json_schema(role)["properties"])
    # 多余键一律视为越权输出（例如执行者试图写入评估结论），排序后一次性报出便于定位。
    unexpected = sorted(set(record) - allowed)
    if unexpected:
        raise ValueError(f"{role} record contains unexpected keys: {', '.join(unexpected)}")

    if role == "executor":
        if not isinstance(record["raw_metrics"], dict):
            raise ValueError("executor raw_metrics must be an object")
        for artifact in record["artifacts"]:
            if not isinstance(artifact, str) or not artifact.startswith("artifacts/"):
                raise ValueError(f"executor artifact must be under artifacts/: {artifact}")
    elif role == "evaluator":
        if record["verdict"] not in VERDICTS:
            raise ValueError(f"invalid evaluator verdict: {record['verdict']}")
        metric = record["metric"]
        for key in ("name", "candidate", "champion", "direction"):
            if key not in metric:
                raise ValueError(f"evaluator metric missing required key: {key}")
        for key in ("passed", "result"):
            if key not in record["code_review"]:
                raise ValueError(f"evaluator code_review missing required key: {key}")
        for key in ("required", "trigger"):
            if key not in record["human_review"]:
                raise ValueError(f"evaluator human_review missing required key: {key}")
        for call in record["skill_calls"]:
            for key in ("name", "skill_loaded", "result"):
                if key not in call:
                    raise ValueError(f"evaluator skill_call missing required key: {key}")
    elif role == "reflector":
        if record["proposed_action"] not in PROPOSED_ACTIONS:
            raise ValueError(f"invalid reflector proposed_action: {record['proposed_action']}")
        for key in ("required", "trigger"):
            if key not in record["human_review"]:
                raise ValueError(f"reflector human_review missing required key: {key}")


def utc_now() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串，秒级精度（微秒被抹零），带时区偏移。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iteration_id(iteration: int) -> str:
    """把整数轮次格式化成统一的迭代标识，如 1 -> "iter_001"，用于目录名与记录字段。"""
    return f"iter_{iteration:03d}"


def ensure_dir(path: Path) -> Path:
    """递归创建目录（已存在则忽略），并把该目录路径原样返回，便于链式调用。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default: Any | None = None) -> Any:
    """读取 UTF-8 编码的 JSON 文件并返回解析结果；文件不存在时返回 default。

    注意：文件存在但内容非法 JSON 时不会兜底，会由 json 模块抛出异常。
    """
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, data: Any) -> None:
    """把数据以缩进 2、键排序、保留非 ASCII 的形式写入 JSON 文件，并补一个换行。

    会先自动创建父目录；同名文件将被覆盖。
    """
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    """向 JSONL 文件追加一行紧凑 JSON（键排序、保留非 ASCII），父目录自动创建。"""
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")


def write_text(path: Path, content: str) -> None:
    """以 UTF-8 覆盖写入文本文件，父目录自动创建。"""
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def append_text(path: Path, content: str) -> None:
    """以 UTF-8 在文本文件末尾追加内容（不额外补换行），父目录自动创建。"""
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as file:
        file.write(content)


def relpath(path: Path, base: Path) -> str:
    """把 path 解析为相对 base 的 POSIX 风格相对路径；path 不在 base 之下时抛 ValueError。"""
    return path.resolve().relative_to(base.resolve()).as_posix()


def metric_is_better(candidate: float, champion: float, direction: str, epsilon: float = 0.0) -> bool:
    """判断候选度量是否优于冠军：direction 为 minimize 时越小越好，否则越大越好。

    epsilon 为最小提升幅度，只有超出该幅度才算真正改进，用于过滤噪声级波动。
    """
    if direction == "minimize":
        return candidate < champion - epsilon
    return candidate > champion + epsilon


def target_reached(score: float, target: float, direction: str) -> bool:
    """判断分数是否达标：minimize 方向要求 score <= target，否则要求 score >= target。"""
    if direction == "minimize":
        return score <= target
    return score >= target
