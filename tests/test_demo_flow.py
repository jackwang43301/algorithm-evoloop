from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from algorithm_evoloop.engine import Engine
from algorithm_evoloop.human.review import HumanReview, NON_INTERACTIVE_REVIEW_MESSAGE
from algorithm_evoloop.llm_client import (
    LLMClient,
    LLMConfig,
    LLMConfigError,
    LLMRequestError,
    parse_json_object,
)
from algorithm_evoloop.memory.store import MemoryStore
from algorithm_evoloop.prompt_builder import PromptBuilder
from algorithm_evoloop.role_runner import LLMRunner, SampleRunner, discover_skills


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "simple_case" / "task.json"


class ScriptedSampleRunner(SampleRunner):
    def __init__(self, records: dict[str, dict[str, object]]):
        self.records = records

    def _record(self, role: str, context: dict[str, object]) -> dict[str, object]:
        return json.loads(json.dumps(self.records[role]))


class DemoFlowTest(unittest.TestCase):
    def test_sample_three_rounds_cover_handoff_memory_human_and_git(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sih-demo-") as tmp:
            workspace = Path(tmp) / "workspace"
            engine = Engine(
                task_path=TASK,
                workspace=workspace,
                framework_root=ROOT,
                runner=SampleRunner(),
                human_decision="rollback",
            )
            summary = engine.run()

            self.assertEqual(summary["status"], "target_reached")
            self.assertEqual(summary["current_champion"]["score"], 0.78)
            self.assertEqual(
                [item["final_decision"] for item in summary["iterations"]],
                ["keep", "rollback", "keep"],
            )
            self.assertEqual(
                [item["proposed_action"] for item in summary["iterations"]],
                ["keep", "request_human_review", "keep"],
            )

            run_dir = workspace / "runs" / summary["run_id"]
            for number in range(1, 4):
                iter_dir = run_dir / f"iter_{number:03d}"
                for name in (
                    "plan.json",
                    "execution.json",
                    "evaluation.json",
                    "reflection.json",
                    "metrics.json",
                    "logs.txt",
                ):
                    self.assertTrue((iter_dir / name).exists(), name)
                self.assertEqual(
                    (iter_dir / "artifacts" / "evidence.json").exists(),
                    number in {1, 3},
                )

            execution = json.loads((run_dir / "iter_001" / "execution.json").read_text())
            self.assertEqual(execution["tool_calls"][0]["name"], "mock_experiment_runner")
            self.assertEqual(execution["tool_calls"][0]["mode"], "mock")
            self.assertEqual(execution["raw_metrics"], {"score": 0.62})
            self.assertNotIn("metrics", execution)
            self.assertNotIn("verdict", execution)
            self.assertNotIn("evidence_complete", execution)
            evaluation = json.loads((run_dir / "iter_001" / "evaluation.json").read_text())
            self.assertEqual(evaluation["tool_calls"][0]["name"], "mock_result_analyzer")
            self.assertTrue(evaluation["skill_calls"][0]["skill_loaded"])
            self.assertEqual(evaluation["code_review"]["result"], "代码审核通过")

            second_reflection = json.loads((run_dir / "iter_002" / "reflection.json").read_text())
            self.assertEqual(second_reflection["human_review"]["trigger"], "insufficient_evidence")
            third_plan = json.loads((run_dir / "iter_003" / "plan.json").read_text())
            self.assertTrue(any("failures-" in item or "do_not_repeat-" in item for item in third_plan["memory_used"]))
            self.assertTrue(any(item.startswith("human-") for item in third_plan["memory_used"]))
            self.assertTrue(any("补齐" in step for step in third_plan["steps"]))

            memory = MemoryStore(workspace).summary()
            self.assertTrue(memory["failures"])
            self.assertTrue(memory["do_not_repeat"])
            self.assertEqual(memory["human_decisions"][0]["recommended_action"], "rollback")
            self.assertEqual(memory["human_decisions"][0]["actor"], "preset_human_review")

            records = [
                json.loads(line)
                for line in (workspace / "versions" / "version_records.jsonl").read_text().splitlines()
            ]
            self.assertEqual([item["final_decision"] for item in records], ["keep", "rollback", "keep"])
            self.assertEqual(records[1]["proposed_action"], "request_human_review")
            self.assertEqual(records[1]["evaluation_verdict"], "insufficient_evidence")
            self.assertTrue(records[1]["human_decision_id"].startswith("human-"))
            self.assertTrue(records[1]["memory_update_ids"])
            second_trace = summary["iterations"][1]["trace"]
            self.assertEqual(second_trace["executor_tools"], ["mock_experiment_runner"])
            self.assertEqual(second_trace["evaluator_tools"], ["mock_result_analyzer"])
            self.assertEqual(second_trace["evaluator_skills"], ["code-review-skill"])
            self.assertEqual(second_trace["human_decision_id"], records[1]["human_decision_id"])

    def test_prompt_context_does_not_expose_engine_internals(self) -> None:
        builder = PromptBuilder(ROOT / "prompts")
        prompt = builder.dynamic_prompt(
            "planner",
            {
                "iteration_id": "iter_003",
                "objective": "提升 score",
                "metric": {"name": "score", "target": 0.75},
                "champion": {"score": 0.62, "commit": "secret"},
                "rules": ["证据完整"],
                "memory": {"do_not_repeat": [{"id": "d1", "text": "补齐证据"}]},
                "workspace": "/private/path",
                "history_summary": {"commit": "secret"},
            },
        )
        self.assertNotIn("workspace", prompt)
        self.assertNotIn("/private/path", prompt)
        self.assertNotIn("commit", prompt)
        self.assertNotIn("history_summary", prompt)
        self.assertIn("补齐证据", prompt)

    def test_evaluator_prompt_invokes_skill_command(self) -> None:
        prompt = PromptBuilder(ROOT / "prompts").dynamic_prompt(
            "evaluator",
            {
                "metric": {"name": "score", "direction": "maximize"},
                "champion": {"score": 0.4},
                "execution": {"raw_metrics": {"score": 0.62}, "artifacts": ["artifacts/evidence.json"]},
                "evidence": {
                    "available_paths": ["metrics.json", "logs.txt", "artifacts/evidence.json"],
                    "required_paths": ["metrics.json", "logs.txt", "artifacts/evidence.json"],
                },
                "evaluator_tools": [{"name": "mock_result_analyzer", "mode": "mock"}],
                "evaluator_skills": [
                    {
                        "name": "code-review-skill",
                        "mode": "prompt",
                        "description": "加载代码审核技能。",
                    }
                ],
                "evaluation_rules": ["证据完整且分数提高才算改善。"],
            },
        )
        self.assertTrue(prompt.startswith("使用技能：code-review-skill"))

    def test_declared_skills_are_discovered_from_project_files(self) -> None:
        skills = discover_skills(ROOT / ".claude" / "skills")
        self.assertEqual([item["name"] for item in skills], ["code-review-skill"])
        self.assertIn("代码审核通过", skills[0]["body"])

    def test_engine_does_not_recompute_evaluation_or_rewrite_reflection(self) -> None:
        records = self._scripted_records(
            raw_score=0.99,
            candidate_score=0.41,
            verdict="inconclusive",
            proposed_action="branch",
            artifacts=["artifacts/evidence.json"],
            evidence_paths=["metrics.json", "logs.txt", "artifacts/evidence.json"],
            missing_evidence=[],
            evaluation_human_review={"required": False, "trigger": None},
            reflection_human_review={"required": False, "trigger": None},
        )
        with tempfile.TemporaryDirectory(prefix="sih-boundary-") as tmp:
            workspace = Path(tmp) / "workspace"
            engine = Engine(
                task_path=TASK,
                workspace=workspace,
                framework_root=ROOT,
                runner=ScriptedSampleRunner(records),
            )
            self.assertFalse(hasattr(engine, "_normalize_evaluation"))
            self.assertFalse(hasattr(engine, "_guard_reflection"))
            summary = engine.run(max_iterations=1)
            self.assertEqual(summary["iterations"][0]["verdict"], "inconclusive")
            self.assertEqual(summary["iterations"][0]["proposed_action"], "branch")
            self.assertEqual(summary["iterations"][0]["final_decision"], "branch")
            run_dir = workspace / "runs" / summary["run_id"] / "iter_001"
            evaluation = json.loads((run_dir / "evaluation.json").read_text())
            reflection = json.loads((run_dir / "reflection.json").read_text())
            self.assertEqual(evaluation["metric"]["candidate"], 0.41)
            self.assertEqual(evaluation["verdict"], "inconclusive")
            self.assertEqual(reflection["proposed_action"], "branch")

    def test_evaluator_can_trigger_human_review_without_reflector_request(self) -> None:
        records = self._scripted_records(
            raw_score=0.61,
            candidate_score=0.61,
            verdict="insufficient_evidence",
            proposed_action="keep",
            artifacts=[],
            evidence_paths=["metrics.json", "logs.txt"],
            missing_evidence=["artifacts/evidence.json"],
            evaluation_human_review={"required": True, "trigger": "insufficient_evidence"},
            reflection_human_review={"required": False, "trigger": None},
        )
        with tempfile.TemporaryDirectory(prefix="sih-evaluator-human-") as tmp:
            workspace = Path(tmp) / "workspace"
            engine = Engine(
                task_path=TASK,
                workspace=workspace,
                framework_root=ROOT,
                runner=ScriptedSampleRunner(records),
                human_decision="rollback",
            )
            summary = engine.run(max_iterations=1)
            item = summary["iterations"][0]
            self.assertEqual(item["proposed_action"], "keep")
            self.assertEqual(item["final_decision"], "rollback")
            self.assertTrue(item["trace"]["human_decision_id"].startswith("human-"))
            memory = MemoryStore(workspace).summary()
            self.assertEqual(memory["human_decisions"][0]["id"], item["trace"]["human_decision_id"])
            version = json.loads(
                (workspace / "versions" / "version_records.jsonl").read_text().splitlines()[0]
            )
            self.assertEqual(version["human_decision_id"], item["trace"]["human_decision_id"])

    def test_non_interactive_human_review_without_preset_blocks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sih-non-interactive-review-") as tmp, patch.object(
            HumanReview, "is_interactive", return_value=False
        ):
            workspace = Path(tmp) / "workspace"
            summary = Engine(
                task_path=TASK,
                workspace=workspace,
                framework_root=ROOT,
                runner=SampleRunner(),
            ).run()

            self.assertEqual(summary["status"], "blocked")
            self.assertEqual(
                [item["final_decision"] for item in summary["iterations"]],
                ["keep", "block"],
            )
            self.assertEqual(summary["next_action"], NON_INTERACTIVE_REVIEW_MESSAGE)
            requests = (workspace / "human" / "review_requests.jsonl").read_text().splitlines()
            self.assertEqual(len(requests), 1)
            self.assertEqual(json.loads(requests[0])["iteration"], 2)
            decision = MemoryStore(workspace).summary()["human_decisions"][0]
            self.assertEqual(decision["actor"], "human_review_unavailable")
            self.assertEqual(decision["recommended_action"], "block")

    def test_interactive_human_review_uses_prompted_action(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sih-interactive-review-") as tmp:
            review = HumanReview(Path(tmp) / "workspace")
            context = {
                "run_id": "run-test",
                "iteration": 2,
                "trigger": "insufficient_evidence",
                "evaluation": {"verdict": "insufficient_evidence"},
                "reflection": {"reason": "证据不足，需要人工审查。"},
            }
            with patch.object(review, "is_interactive", return_value=True), patch(
                "builtins.input", return_value="branch"
            ) as input_mock, patch("builtins.print"):
                decision = review.request(context)

            self.assertEqual(decision["actor"], "human_prompt")
            self.assertEqual(decision["recommended_action"], "branch")
            input_mock.assert_called_once_with("> ")
            requests = (review.requests_path).read_text().splitlines()
            self.assertEqual(len(requests), 1)

    def test_keep_requires_loaded_skills_and_passing_code_review(self) -> None:
        cases = [
            ("required_skill_not_loaded", False, True),
            ("code_review_failed", True, False),
        ]
        for expected_trigger, skill_loaded, code_review_passed in cases:
            with self.subTest(expected_trigger=expected_trigger), tempfile.TemporaryDirectory(
                prefix="sih-keep-governance-"
            ) as tmp:
                records = self._scripted_records(
                    raw_score=0.62,
                    candidate_score=0.62,
                    verdict="improved",
                    proposed_action="keep",
                    artifacts=["artifacts/evidence.json"],
                    evidence_paths=["metrics.json", "logs.txt", "artifacts/evidence.json"],
                    missing_evidence=[],
                    evaluation_human_review={"required": False, "trigger": None},
                    reflection_human_review={"required": False, "trigger": None},
                    skill_loaded=skill_loaded,
                    code_review_passed=code_review_passed,
                )
                workspace = Path(tmp) / "workspace"
                engine = Engine(
                    task_path=TASK,
                    workspace=workspace,
                    framework_root=ROOT,
                    runner=ScriptedSampleRunner(records),
                    human_decision="rollback",
                )
                summary = engine.run(max_iterations=1)
                item = summary["iterations"][0]
                self.assertEqual(item["verdict"], "improved")
                self.assertEqual(item["proposed_action"], "keep")
                self.assertEqual(item["final_decision"], "rollback")
                request = json.loads(
                    (workspace / "human" / "review_requests.jsonl").read_text().splitlines()[0]
                )
                self.assertEqual(request["trigger"], expected_trigger)
                evaluation = json.loads(
                    (
                        workspace
                        / "runs"
                        / summary["run_id"]
                        / "iter_001"
                        / "evaluation.json"
                    ).read_text()
                )
                self.assertEqual(evaluation["verdict"], "improved")
                self.assertEqual(evaluation["skill_calls"][0]["skill_loaded"], skill_loaded)
                self.assertEqual(evaluation["code_review"]["passed"], code_review_passed)

    @staticmethod
    def _scripted_records(
        *,
        raw_score: float,
        candidate_score: float,
        verdict: str,
        proposed_action: str,
        artifacts: list[str],
        evidence_paths: list[str],
        missing_evidence: list[str],
        evaluation_human_review: dict[str, object],
        reflection_human_review: dict[str, object],
        skill_loaded: bool = True,
        code_review_passed: bool = True,
    ) -> dict[str, dict[str, object]]:
        return {
            "planner": {
                "iteration_id": "iter_001",
                "objective": "验证角色边界。",
                "hypothesis": "执行一项小改动。",
                "steps": ["执行实验", "收集原始结果"],
                "success_criteria": ["形成可评估证据"],
                "memory_used": [],
            },
            "executor": {
                "change_summary": "执行候选改动。",
                "tool_calls": [
                    {
                        "name": "mock_experiment_runner",
                        "mode": "mock",
                        "status": "success",
                        "result": "返回原始结果。",
                    }
                ],
                "raw_metrics": {"score": raw_score},
                "logs": ["实验执行完成。"],
                "artifacts": artifacts,
            },
            "evaluator": {
                "metric": {
                    "name": "score",
                    "candidate": candidate_score,
                    "champion": 0.4,
                    "direction": "maximize",
                },
                "evidence_complete": not missing_evidence,
                "evidence_paths": evidence_paths,
                "missing_evidence": missing_evidence,
                "tool_calls": [
                    {
                        "name": "mock_result_analyzer",
                        "mode": "mock",
                        "status": "success",
                        "result": "完成独立评估。",
                    }
                ],
                "skill_calls": [
                    {
                        "name": "code-review-skill",
                        "skill_loaded": skill_loaded,
                        "result": "代码审核通过" if skill_loaded else "技能未加载",
                    }
                ],
                "code_review": {
                    "passed": code_review_passed,
                    "result": "代码审核通过" if code_review_passed else "代码审核未通过",
                },
                "verdict": verdict,
                "human_review": evaluation_human_review,
            },
            "reflector": {
                "proposed_action": proposed_action,
                "reason": "直接使用 Evaluator 结论。",
                "memory_updates": [{"type": "failure", "text": "记录本轮结论。"}],
                "human_review": reflection_human_review,
            },
        }

    @unittest.skipUnless(os.environ.get("RUN_LLM_INTEGRATION") == "1", "set RUN_LLM_INTEGRATION=1")
    def test_real_llm_three_round_acceptance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sih-llm-integration-") as tmp:
            runner = LLMRunner(
                client=LLMClient(LLMConfig.load()),
                prompt_builder=PromptBuilder(ROOT / "prompts"),
                skill_root=ROOT / ".claude" / "skills",
            )
            engine = Engine(
                task_path=TASK,
                workspace=Path(tmp) / "workspace",
                framework_root=ROOT,
                runner=runner,
                human_decision="rollback",
            )
            summary = engine.run()
            self.assertEqual(summary["status"], "target_reached")
            self.assertEqual(summary["current_champion"]["score"], 0.78)
            self.assertEqual(
                [item["final_decision"] for item in summary["iterations"]],
                ["keep", "rollback", "keep"],
            )
            run_dir = Path(tmp) / "workspace" / "runs" / summary["run_id"]
            evaluator_log = json.loads((run_dir / "iter_001" / ".llm_logs" / "evaluator.json").read_text())
            self.assertIn("code-review-skill", evaluator_log["available_skills"])
            third_plan = json.loads((run_dir / "iter_003" / "plan.json").read_text())
            self.assertTrue(third_plan["memory_used"])
            self.assertTrue(any("证据" in step for step in third_plan["steps"]))


class StubResponse:
    def __init__(self, payload: dict[str, object]):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class StubOpener:
    """Replays queued responses or errors and records the requests it received."""

    def __init__(self, outcomes: list[object]):
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, object]] = []

    def __call__(self, request, timeout=None):
        self.requests.append(json.loads(request.data.decode("utf-8")))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return StubResponse(outcome)


def _reply(text: str) -> dict[str, object]:
    return {"choices": [{"message": {"content": text}}]}


class LLMClientTest(unittest.TestCase):
    def _config(self, **overrides: object) -> LLMConfig:
        data = {
            "base_url": "https://example.invalid/v1/",
            "model": "demo-model",
            "api_key": "secret",
            "max_retries": 1,
        }
        data.update(overrides)
        return LLMConfig(**data)

    def test_config_precedence_puts_overrides_above_env_and_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sih-llm-config-") as tmp:
            path = Path(tmp) / "llm.json"
            path.write_text(
                json.dumps(
                    {
                        "base_url": "https://from-file/v1",
                        "model": "file-model",
                        "chat_path": "/completions",
                    }
                ),
                encoding="utf-8",
            )
            env = {"LLM_MODEL": "env-model", "LLM_KEY_HOLDER": "env-secret"}
            config = LLMConfig.load(
                config_path=path,
                overrides={"model": None, "api_key_env": "LLM_KEY_HOLDER", "temperature": 0.3},
                env=env,
            )
        self.assertEqual(config.base_url, "https://from-file/v1")
        self.assertEqual(config.model, "env-model")
        self.assertEqual(config.endpoint, "https://from-file/v1/completions")
        self.assertEqual(config.api_key, "env-secret")
        self.assertEqual(config.temperature, 0.3)

    def test_missing_endpoint_configuration_is_rejected(self) -> None:
        with self.assertRaises(LLMConfigError):
            LLMConfig.load(env={"LLM_MODEL": "demo-model"})

    def test_parse_json_object_accepts_code_fences_and_trailing_text(self) -> None:
        text = "```json\n{\"verdict\": \"improved\"}\n```"
        self.assertEqual(parse_json_object(text), {"verdict": "improved"})
        self.assertEqual(parse_json_object("说明：{\"a\": 1} 完成"), {"a": 1})
        with self.assertRaises(LLMRequestError):
            parse_json_object("没有对象")

    def test_structured_output_degrades_and_retries(self) -> None:
        opener = StubOpener(
            [
                urllib.error.HTTPError("url", 500, "boom", None, None),
                _reply("这不是 JSON"),
                _reply("{\"verdict\": \"improved\"}"),
            ]
        )
        client = LLMClient(self._config(), opener=opener)
        with patch("algorithm_evoloop.llm_client.time.sleep"):
            result = client.complete("系统提示", "用户提示", {"type": "object"}, "evaluator_record")
        self.assertEqual(result["record"], {"verdict": "improved"})
        self.assertEqual(result["mode"], "json_object")
        self.assertEqual(opener.requests[0]["response_format"]["type"], "json_schema")
        self.assertEqual(
            opener.requests[0]["response_format"]["json_schema"]["name"], "evaluator_record"
        )
        self.assertEqual(opener.requests[-1]["response_format"], {"type": "json_object"})
        self.assertEqual([item.get("status") for item in result["attempts"]], [500, 200, None, 200])

    def test_repeated_failures_raise_with_attempt_trace(self) -> None:
        opener = StubOpener([urllib.error.HTTPError("url", 401, "denied", None, None)] * 3)
        client = LLMClient(self._config(structured_output="json_object"), opener=opener)
        with patch("algorithm_evoloop.llm_client.time.sleep"), self.assertRaises(LLMRequestError) as ctx:
            client.complete("系统提示", "用户提示", {"type": "object"})
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(ctx.exception.attempts)


if __name__ == "__main__":
    unittest.main()
