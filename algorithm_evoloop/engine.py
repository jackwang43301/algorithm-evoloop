"""自迭代闭环的调度引擎：串联规划者、执行者、评估者、反思者四个角色，并落盘审计与版本记录。"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .human.review import HumanReview
from .memory.store import MemoryStore
from .prompt_builder import PromptBuilder
from .role_runner import RoleExecutionError, RoleRunner, SampleRunner
from .schemas import (
    append_jsonl,
    ensure_dir,
    iteration_id,
    read_json,
    target_reached,
    utc_now,
    write_json,
    write_text,
)
from .tools.versioning import FileSnapshotAdapter, GitAdapter, VersionAdapter
from .workspace_guard import WorkspaceGuard, WorkspaceGuardViolation


class StoppedByHuman(RuntimeError):
    """人工在某个角色执行前选择 stop 时抛出，用于让本轮迭代提前退出并把运行状态标为 stopped_by_human。"""

    pass


class Engine:
    """自迭代闭环的总调度器：按轮驱动规划者、执行者、评估者、反思者，并负责治理决策、版本落盘与状态输出。"""

    def __init__(
        self,
        task_path: Path,
        workspace: Path,
        framework_root: Path,
        version_adapter: str = "git",
        runner: RoleRunner | None = None,
        prompts_dir: Path | None = None,
        show_role_io: bool = False,
        human_decision: str | None = None,
        pause_before: set[str] | None = None,
    ):
        """加载并校验任务配置，解析各类目录为绝对路径，装配提示词构建器、角色执行器、记忆库、人工评审与工作区守卫。
        version_adapter 取 "file-snapshot" 或其他值（默认走 Git）决定版本适配器实现；
        任务文件缺字段时由 _load_task 抛 ValueError。"""
        self.task_path = task_path.resolve()
        self.workspace = workspace.resolve()
        self.framework_root = framework_root.resolve()
        self.task = self._load_task(self.task_path)
        self.prompts_dir = (prompts_dir or self.framework_root / "prompts").resolve()
        self.prompt_builder = PromptBuilder(self.prompts_dir)
        self.runner = runner or SampleRunner()
        self.show_role_io = show_role_io
        self.memory = MemoryStore(self.workspace)
        self.human_review = HumanReview(
            self.workspace,
            preset_decision=human_decision,
            pause_before=pause_before,
        )
        self.guard = WorkspaceGuard(self.workspace)
        self.version_adapter_name = version_adapter
        self.version_adapter = self._build_version_adapter()

    def initialize(self) -> dict[str, str]:
        """创建审计目录与记忆库，让版本适配器建立基线，并写出 status=initialized 的 STATUS.md。
        返回版本适配器的信息字典，其中 baseline_commit 作为首轮擂主（champion）的起始提交。"""
        self._initialize_audit_dirs()
        self.memory.initialize()
        version_info = self.version_adapter.initialize(self.task)
        metric = self.task["metric"]
        self._write_status(
            {
                "run_id": "not-started",
                "status": "initialized",
                "current_champion": {
                    "score": float(metric["initial"]),
                    "commit": version_info["baseline_commit"],
                    "iteration": 0,
                },
                "last_decision": None,
                "next_action": "启动第一轮迭代",
            }
        )
        return version_info

    def run(self, max_iterations: int | None = None) -> dict[str, Any]:
        """驱动完整闭环：每轮依次跑规划者、执行者、评估者、反思者，再做治理决策与版本记录。
        返回运行摘要（run_id、current_champion、iterations、status、next_action 等）；
        status 可能为 stopped_by_human、blocked、target_reached 或 max_iterations_reached。"""
        version_info = self.initialize()
        run_id = self._new_run_id()
        run_dir = ensure_dir(self.workspace / "runs" / run_id)
        metric = self.task["metric"]
        limit = max_iterations or int(self.task.get("max_iterations", 1))
        champion = {
            "score": float(metric["initial"]),
            "commit": version_info["baseline_commit"],
            "iteration": 0,
        }
        summary: dict[str, Any] = {
            "run_id": run_id,
            "task_name": self.task["name"],
            "status": "running",
            "current_champion": champion,
            "iterations": [],
            "created_at": utc_now(),
            "last_decision": None,
            "next_action": "运行第一轮迭代",
        }
        self._write_run_summary(run_dir, summary)

        for iteration in range(1, limit + 1):
            iter_id = iteration_id(iteration)
            iter_dir = ensure_dir(run_dir / iter_id)
            ensure_dir(iter_dir / "artifacts")
            memory = self.memory.summary()

            try:
                plan = self._run_role(
                    "planner",
                    run_id,
                    iteration,
                    iter_dir,
                    {
                        "iteration_id": iter_id,
                        "objective": self.task["objective"],
                        "metric": metric,
                        "champion": {"score": champion["score"]},
                        "rules": self.task["rules"],
                        "memory": memory,
                    },
                )
                write_json(iter_dir / "plan.json", plan)

                fixture = self._fixture(iteration)
                execution = self._run_role(
                    "executor",
                    run_id,
                    iteration,
                    iter_dir,
                    {
                        "iteration_id": iter_id,
                        "plan": plan,
                        "tools": self.task["executor_tools"],
                        "fixture": fixture,
                    },
                )
                self._validate_execution_references(execution)
                self._materialize_execution(iter_dir, execution)
                write_json(iter_dir / "execution.json", execution)

                candidate_version = self.version_adapter.commit_candidate(
                    run_id, iteration, execution
                )
                evidence = {
                    "available_paths": ["metrics.json", "logs.txt", *execution["artifacts"]],
                    "required_paths": self.task["required_evidence"],
                }
                evaluation = self._run_role(
                    "evaluator",
                    run_id,
                    iteration,
                    iter_dir,
                    {
                        "metric": metric,
                        "champion": {"score": champion["score"]},
                        "execution": execution,
                        "evidence": evidence,
                        "evaluator_tools": self.task["evaluator_tools"],
                        "evaluator_skills": self.task["evaluator_skills"],
                        "evaluation_rules": self.task["evaluation_rules"],
                    },
                )
                self._validate_evaluation_references(evaluation, iter_dir)
                write_json(iter_dir / "evaluation.json", evaluation)

                consecutive_failures = self._consecutive_failures(summary, evaluation)
                reflection = self._run_role(
                    "reflector",
                    run_id,
                    iteration,
                    iter_dir,
                    {
                        "evaluation": evaluation,
                        "consecutive_failures": consecutive_failures,
                        "human_review_rules": self.task["human_review"],
                    },
                )
                write_json(iter_dir / "reflection.json", reflection)
            except StoppedByHuman:
                summary["status"] = "stopped_by_human"
                summary["next_action"] = "人工在角色执行前停止"
                self._write_run_summary(run_dir, summary)
                self._write_status(summary)
                break
            except (RoleExecutionError, WorkspaceGuardViolation, ValueError) as exc:
                self._handle_role_failure(run_id, iteration, iter_dir, run_dir, summary, champion, exc)
                break

            applied_memory = self.memory.apply_updates(
                reflection["memory_updates"], run_id, iteration
            )
            governance = self._apply_decision(
                run_id,
                iteration,
                evaluation,
                reflection,
                candidate_version,
                champion,
            )
            final_decision = governance["final_decision"]
            human_decision = governance["human_decision"]
            # 只有 keep 才让候选晋升为新擂主，branch/block/rollback 都保留原擂主
            if final_decision == "keep":
                champion = {
                    "score": float(evaluation["metric"]["candidate"]),
                    "commit": candidate_version["commit_sha"],
                    "iteration": iteration,
                }

            version_record = self._write_version_record(
                run_id,
                iteration,
                candidate_version,
                evaluation,
                reflection,
                human_decision,
                applied_memory,
                final_decision,
            )
            iter_summary = {
                "iteration": iteration,
                "iteration_id": iter_id,
                "score": evaluation["metric"]["candidate"],
                "verdict": evaluation["verdict"],
                "proposed_action": reflection["proposed_action"],
                "final_decision": final_decision,
                "candidate_commit": candidate_version["commit_sha"],
                "version_record": version_record,
                "memory_updates": applied_memory,
                "trace": {
                    "memory_used": plan["memory_used"],
                    "executor_tools": [item["name"] for item in execution["tool_calls"]],
                    "evaluator_tools": [item["name"] for item in evaluation["tool_calls"]],
                    "evaluator_skills": [item["name"] for item in evaluation["skill_calls"]],
                    "evaluation_verdict": evaluation["verdict"],
                    "human_decision_id": human_decision.get("id") if human_decision else None,
                    "memory_update_ids": [item["id"] for item in applied_memory],
                },
            }
            summary["iterations"].append(iter_summary)
            summary["current_champion"] = champion
            summary["last_decision"] = final_decision

            if final_decision == "block":
                summary["status"] = "blocked"
                # 人工评审通道不可用时，把其说明原样透出，指引使用者补齐评审条件
                if human_decision and human_decision.get("actor") == "human_review_unavailable":
                    summary["next_action"] = human_decision["notes"]
                else:
                    summary["next_action"] = "等待人工处理"
                self._write_run_summary(run_dir, summary)
                self._write_status(summary)
                break
            if final_decision == "keep" and target_reached(
                champion["score"], float(metric["target"]), metric["direction"]
            ):
                summary["status"] = "target_reached"
                summary["next_action"] = "目标已达到"
                self._write_run_summary(run_dir, summary)
                self._write_status(summary)
                break

            summary["next_action"] = "进入下一轮迭代"
            self._write_run_summary(run_dir, summary)
            self._write_status(summary)
        else:
            # for-else：循环跑满 limit 轮且没有任何 break，说明既未达标也未被阻塞
            summary["status"] = "max_iterations_reached"
            summary["next_action"] = "检查记录后决定是否继续"
            self._write_run_summary(run_dir, summary)
            self._write_status(summary)

        return summary

    def _run_role(
        self,
        role: str,
        run_id: str,
        iteration: int,
        iter_dir: Path,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """执行单个角色：先给人工干预机会（stop 则抛 StoppedByHuman，notes 会作为 human_guidance 注入上下文），
        再用 PromptBuilder 拼动态提示词，在 WorkspaceGuard 保护下调用 runner，写日志并返回结构化的角色输出记录。"""
        intervention = self.human_review.before_role(role, run_id, iteration)
        if intervention:
            self.memory.record_human_decision(intervention)
            if intervention["decision"] == "stop":
                raise StoppedByHuman(role)
            if intervention.get("notes"):
                # 人工留下的备注作为额外指引合并进上下文，不覆盖原有字段
                context = {**context, "human_guidance": intervention["notes"]}

        prompt = self.prompt_builder.dynamic_prompt(role, context)
        if self.show_role_io:
            self._print_role_input(role, prompt)
        started_at = time.monotonic()
        with self.guard.protect(role, iter_dir):
            raw = self.runner.execute(role, prompt, self.workspace, iter_dir, context)
        self.runner.write_log(role, raw, iter_dir)
        record = self.runner.collect(role, raw, iter_dir, self.workspace)
        if self.show_role_io:
            self._print_role_output(role, record, time.monotonic() - started_at)
        return record

    def _validate_evaluation_references(
        self, evaluation: dict[str, Any], iter_dir: Path
    ) -> None:
        """校验评估者输出的可信度：指标名必须与任务一致，证据路径必须真实存在且不越出本轮目录，
        工具与技能调用必须都在任务声明的白名单内；任一条不满足即抛 ValueError 让本轮被阻塞。"""
        if evaluation["metric"]["name"] != self.task["metric"]["name"]:
            raise ValueError("Evaluator returned an unknown metric name")
        for item in evaluation["evidence_paths"]:
            path = (iter_dir / item).resolve()
            try:
                # relative_to 失败说明路径通过 .. 或绝对路径逃出了迭代目录，属于越权引用
                path.relative_to(iter_dir.resolve())
            except ValueError as exc:
                raise ValueError(f"Evaluator evidence path escapes iteration directory: {item}") from exc
            if not path.is_file():
                raise ValueError(f"Evaluator referenced missing evidence: {item}")

        configured_tools = {item["name"] for item in self.task["evaluator_tools"]}
        called_tools = {item["name"] for item in evaluation["tool_calls"]}
        if not called_tools.issubset(configured_tools):
            raise ValueError("Evaluator called an undeclared tool")
        configured_skills = {item["name"] for item in self.task["evaluator_skills"]}
        called_skills = {item["name"] for item in evaluation["skill_calls"]}
        if not called_skills.issubset(configured_skills):
            raise ValueError("Evaluator called an undeclared skill")

    def _validate_execution_references(self, execution: dict[str, Any]) -> None:
        """校验执行者只使用了任务 executor_tools 中声明的工具；出现未声明工具时抛 ValueError，避免越权改动进入评估。"""
        configured_tools = {item["name"] for item in self.task["executor_tools"]}
        called_tools = {item["name"] for item in execution["tool_calls"]}
        if not called_tools.issubset(configured_tools):
            raise ValueError("Executor called an undeclared tool")

    def _apply_decision(
        self,
        run_id: str,
        iteration: int,
        evaluation: dict[str, Any],
        reflection: dict[str, Any],
        candidate_version: dict[str, str],
        champion: dict[str, Any],
    ) -> dict[str, Any]:
        """把反思者的建议动作落成最终治理决策：必要时先走人工评审改写动作，再调用版本适配器 keep/branch/rollback。
        返回 {"final_decision": keep|branch|block|rollback, "human_decision": 人工决策或 None}；
        动作不在这四种之内时抛 ValueError。"""
        action = reflection["proposed_action"]
        evaluation_review = evaluation["human_review"]
        reflection_review = reflection["human_review"]
        governance_trigger = self._keep_governance_trigger(action, evaluation)
        # 评估者、反思者显式要求，或 keep 未满足治理红线，都会强制进入人工评审
        review_required = (
            evaluation_review["required"]
            or reflection_review["required"]
            or action == "request_human_review"
            or governance_trigger is not None
        )
        human_decision: dict[str, Any] | None = None
        if review_required:
            # 触发原因按优先级取第一个命中的，便于人工看到最根本的原因
            if evaluation_review["required"]:
                trigger = evaluation_review["trigger"]
            elif reflection_review["required"]:
                trigger = reflection_review["trigger"]
            elif governance_trigger is not None:
                trigger = governance_trigger
            else:
                trigger = "request_human_review"
            decision = self.human_review.request(
                {
                    "run_id": run_id,
                    "iteration": iteration,
                    "trigger": trigger,
                    "evaluation": evaluation,
                    "reflection": reflection,
                }
            )
            human_decision = self.memory.record_human_decision(decision)
            # 人工的建议动作直接覆盖反思者的提议，人工判断优先级最高
            action = human_decision["recommended_action"]
        if action == "keep":
            self.version_adapter.keep(candidate_version["commit_sha"])
            return {"final_decision": "keep", "human_decision": human_decision}
        if action == "branch":
            self.version_adapter.branch(
                f"explore/{run_id}-iter-{iteration:03d}",
                candidate_version["commit_sha"],
                champion["commit"],
            )
            return {"final_decision": "branch", "human_decision": human_decision}
        if action == "block":
            return {"final_decision": "block", "human_decision": human_decision}
        if action != "rollback":
            raise ValueError(f"Unsupported final governance action: {action}")
        self.version_adapter.rollback(champion["commit"])
        return {"final_decision": "rollback", "human_decision": human_decision}

    def _keep_governance_trigger(
        self, action: str, evaluation: dict[str, Any]
    ) -> str | None:
        """判断 keep 是否踩了治理红线：必需技能未真正加载返回 "required_skill_not_loaded"，
        代码评审未通过返回 "code_review_failed"，其余情况返回 None 表示可直接采纳。"""
        if action != "keep":
            return None
        loaded_skills = {
            item["name"]: item["skill_loaded"] for item in evaluation["skill_calls"]
        }
        required_skills = {item["name"] for item in self.task["evaluator_skills"]}
        # 未出现在 skill_calls 中的必需技能视为未加载，等同于加载失败
        if any(not loaded_skills.get(name, False) for name in required_skills):
            return "required_skill_not_loaded"
        if not evaluation["code_review"]["passed"]:
            return "code_review_failed"
        return None

    def _materialize_execution(self, iter_dir: Path, execution: dict[str, Any]) -> None:
        """把执行者输出落盘为本轮证据：写 metrics.json 与 logs.txt，并逐个生成 artifacts 文件。
        产物路径若逃出迭代目录或不在 artifacts/ 下，抛 ValueError 阻止污染工作区。"""
        write_json(iter_dir / "metrics.json", execution["raw_metrics"])
        write_text(iter_dir / "logs.txt", "\n".join(execution["logs"]) + "\n")
        for item in execution["artifacts"]:
            path = (iter_dir / item).resolve()
            try:
                relative = path.relative_to(iter_dir.resolve())
            except ValueError as exc:
                raise ValueError(f"Executor artifact escapes iteration directory: {item}") from exc
            # 产物必须落在 artifacts/ 子目录，避免覆盖 plan.json、evaluation.json 等审计文件
            if not relative.parts or relative.parts[0] != "artifacts":
                raise ValueError(f"Executor artifact must be under artifacts/: {item}")
            write_json(
                path,
                {
                    "tool_calls": execution["tool_calls"],
                    "raw_metrics": execution["raw_metrics"],
                    "change_summary": execution["change_summary"],
                },
            )

    def _fixture(self, iteration: int) -> dict[str, Any]:
        """按轮次取任务 demo_fixtures 中对应的样例数据（1 基下标）供执行者使用；轮次越界时抛 ValueError。"""
        fixtures = self.task.get("demo_fixtures", [])
        if iteration < 1 or iteration > len(fixtures):
            raise ValueError(f"missing demo fixture for iteration {iteration}")
        return fixtures[iteration - 1]

    @staticmethod
    def _consecutive_failures(summary: dict[str, Any], evaluation: dict[str, Any]) -> int:
        """统计截至本轮的连续未改进次数：本轮 verdict 为 improved 返回 0，否则从本轮起向历史回溯累加。
        该计数会传给反思者，用于按人工评审规则判断是否升级到人工介入。"""
        if evaluation["verdict"] == "improved":
            return 0
        count = 1
        for item in reversed(summary["iterations"]):
            if item["verdict"] == "improved":
                break
            count += 1
        return count

    def _write_version_record(
        self,
        run_id: str,
        iteration: int,
        candidate_version: dict[str, str],
        evaluation: dict[str, Any],
        reflection: dict[str, Any],
        human_decision: dict[str, Any] | None,
        applied_memory: list[dict[str, Any]],
        final_decision: str,
    ) -> str:
        """把本轮的补丁、提交、评估结论、反思动作、人工决策与记忆更新 id 追加写入 versions/version_records.jsonl。
        返回该记录文件相对工作区的 posix 路径，便于写进运行摘要形成可追溯链路。"""
        path = self.workspace / "versions" / "version_records.jsonl"
        append_jsonl(
            path,
            {
                "run_id": run_id,
                "iteration": iteration,
                "run_record": f"runs/{run_id}/{iteration_id(iteration)}",
                "patch_id": candidate_version["patch_id"],
                "commit_sha": candidate_version["commit_sha"],
                "parent_commit": candidate_version["parent_commit"],
                "evaluation_verdict": evaluation["verdict"],
                "proposed_action": reflection["proposed_action"],
                "human_decision_id": human_decision.get("id") if human_decision else None,
                "final_decision": final_decision,
                "memory_update_ids": [item["id"] for item in applied_memory],
                "reason": reflection["reason"],
                "created_at": utc_now(),
            },
        )
        return path.relative_to(self.workspace).as_posix()

    def _handle_role_failure(
        self,
        run_id: str,
        iteration: int,
        iter_dir: Path,
        run_dir: Path,
        summary: dict[str, Any],
        champion: dict[str, Any],
        exc: Exception,
    ) -> None:
        """角色执行异常、工作区越权或结构校验失败时的收尾：写 blocker.json（含错误类型、消息、角色原始输出或越权明细），
        向记忆库追加一条 failure 记录，并把运行摘要与 STATUS.md 置为 blocked，擂主保持不变。"""
        blocker = {
            "error_type": type(exc).__name__,
            "message": str(exc),
            "created_at": utc_now(),
        }
        # 不同异常携带不同上下文：角色执行失败补原始输出，守卫违规补越权文件清单
        if isinstance(exc, RoleExecutionError):
            blocker["role"] = exc.role
            blocker["raw"] = exc.raw
        if isinstance(exc, WorkspaceGuardViolation):
            blocker["violations"] = exc.violations
        write_json(iter_dir / "blocker.json", blocker)
        self.memory.apply_updates(
            [{"type": "failure", "text": f"第 {iteration} 轮被阻塞：{type(exc).__name__}"}],
            run_id,
            iteration,
        )
        summary["status"] = "blocked"
        summary["current_champion"] = champion
        summary["last_decision"] = "block"
        summary["next_action"] = "检查 blocker.json 并人工处理"
        self._write_run_summary(run_dir, summary)
        self._write_status(summary)

    def _initialize_audit_dirs(self) -> None:
        """准备工作区审计骨架：建 runs/memory/versions/human 四个目录，并把版本记录与人工记录的 jsonl 补齐为空文件。"""
        for name in ("runs", "memory", "versions", "human"):
            ensure_dir(self.workspace / name)
        for path in (
            self.workspace / "versions" / "version_records.jsonl",
            self.workspace / "human" / "review_requests.jsonl",
            self.workspace / "human" / "role_interventions.jsonl",
        ):
            if not path.exists():
                write_text(path, "")

    def _write_run_summary(self, run_dir: Path, summary: dict[str, Any]) -> None:
        """把运行摘要覆盖写入 runs/<run_id>/run_summary.json，每轮结束或中断时都会刷新，保证进度可断点续查。"""
        write_json(run_dir / "run_summary.json", summary)

    def _write_status(self, summary: dict[str, Any]) -> None:
        """依据摘要渲染工作区根目录的 STATUS.md，暴露 run_id、状态、擂主分数与提交、上一次决策和下一步动作。"""
        champion = summary["current_champion"]
        write_text(
            self.workspace / "STATUS.md",
            (
                "# algorithm-evoloop status\n\n"
                f"- run_id: `{summary['run_id']}`\n"
                f"- status: `{summary['status']}`\n"
                f"- current_champion_score: `{champion['score']}`\n"
                f"- current_champion_commit: `{champion['commit']}`\n"
                f"- last_decision: `{summary.get('last_decision')}`\n"
                f"- next_action: {summary['next_action']}\n"
            ),
        )

    def _build_version_adapter(self) -> VersionAdapter:
        """按 version_adapter 名称选择版本后端："file-snapshot" 走文件快照，其他值默认回退到 Git 适配器。"""
        if self.version_adapter_name == "file-snapshot":
            return FileSnapshotAdapter(self.workspace, self.framework_root)
        return GitAdapter(self.workspace, self.framework_root)

    @staticmethod
    def _new_run_id() -> str:
        """生成形如 run_<UTC时间戳>_<8位随机串> 的运行 id，保证同一工作区内多次运行目录互不冲突。"""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"run_{stamp}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _load_task(path: Path) -> dict[str, Any]:
        """读取并严格校验任务定义：顶层必填字段、metric 的 name/direction/initial/target、
        以及执行者工具与评估者工具/技能非空且每项都声明 name、mode、description。
        返回任务字典；任何一项不满足都抛 ValueError，以免带着残缺配置进入闭环。"""
        task = read_json(path)
        if not isinstance(task, dict):
            raise ValueError(f"Task file not found or invalid: {path}")
        required = {
            "name",
            "objective",
            "metric",
            "rules",
            "executor_tools",
            "evaluator_tools",
            "evaluator_skills",
            "evaluation_rules",
            "required_evidence",
            "human_review",
        }
        missing = sorted(required - set(task))
        if missing:
            raise ValueError(f"Task missing required fields: {', '.join(missing)}")
        for key in ("name", "direction", "initial", "target"):
            if key not in task["metric"]:
                raise ValueError(f"Task metric missing required field: {key}")
        if not task["executor_tools"] or not task["evaluator_tools"] or not task["evaluator_skills"]:
            raise ValueError("Task must declare Executor tools and Evaluator tools/skills")
        for key in ("executor_tools", "evaluator_tools", "evaluator_skills"):
            for declaration in task[key]:
                if not isinstance(declaration, dict) or not all(
                    declaration.get(field) for field in ("name", "mode", "description")
                ):
                    raise ValueError(
                        f"Task {key} entries must declare name, mode, and description"
                    )
        return task

    def _print_role_input(self, role: str, prompt: str) -> None:
        """在 show_role_io 打开时，把即将发给该角色的完整提示词打到标准输出，便于人工旁观闭环输入。"""
        print(f"\n{'=' * 16} {role.upper()} INPUT {'=' * 16}", flush=True)
        print(prompt.rstrip(), flush=True)

    @staticmethod
    def _print_role_output(role: str, record: dict[str, Any], elapsed: float) -> None:
        """在 show_role_io 打开时，把角色结构化输出与耗时以 JSON 打印出来，用于调试角色返回是否符合约定结构。"""
        print(f"\n{'=' * 16} {role.upper()} OUTPUT ({elapsed:.2f}s) {'=' * 16}", flush=True)
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
