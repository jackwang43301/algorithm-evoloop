from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from algorithm_evoloop.schemas import ensure_dir, read_json, validate_role_record, write_text
from algorithm_evoloop.tools.versioning import GitAdapter
from algorithm_evoloop.workspace_guard import WorkspaceGuard, WorkspaceGuardViolation


ROOT = Path(__file__).resolve().parents[1]
TASK = json.loads((ROOT / "examples" / "simple_case" / "task.json").read_text())


class WorkspaceGuardTest(unittest.TestCase):
    def test_executor_record_rejects_evaluation_fields(self) -> None:
        record = {
            "change_summary": "执行候选改动。",
            "tool_calls": [
                {
                    "name": "mock_experiment_runner",
                    "mode": "mock",
                    "status": "success",
                    "result": "返回原始结果。",
                }
            ],
            "raw_metrics": {"score": 0.62},
            "logs": ["执行完成。"],
            "artifacts": ["artifacts/evidence.json"],
            "verdict": "improved",
        }
        with self.assertRaisesRegex(ValueError, "unexpected keys: verdict"):
            validate_role_record("executor", record)

    def test_git_adapter_keeps_and_rolls_back_candidate_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sih-version-") as tmp:
            workspace = Path(tmp) / "workspace"
            adapter = GitAdapter(workspace, ROOT)
            baseline = adapter.initialize(TASK)
            first = adapter.commit_candidate(
                "run-test",
                1,
                {
                    "change_summary": "first",
                    "tool_calls": [],
                    "raw_metrics": {"score": 0.62},
                },
            )
            adapter.keep(first["commit_sha"])
            second = adapter.commit_candidate(
                "run-test",
                2,
                {
                    "change_summary": "second",
                    "tool_calls": [],
                    "raw_metrics": {"score": 0.61},
                },
            )
            adapter.branch("explore/test", second["commit_sha"], first["commit_sha"])
            state = read_json(workspace / "target_repo" / "candidate" / "state.json")
            self.assertEqual(state["raw_metrics"]["score"], 0.62)
            self.assertNotEqual(baseline["baseline_commit"], first["commit_sha"])

    def test_guard_restores_unauthorized_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sih-guard-") as tmp:
            workspace = Path(tmp) / "workspace"
            iter_dir = ensure_dir(workspace / "runs" / "run-test" / "iter_001")
            status = workspace / "STATUS.md"
            write_text(status, "safe\n")
            guard = WorkspaceGuard(workspace)
            with self.assertRaises(WorkspaceGuardViolation):
                with guard.protect("planner", iter_dir):
                    write_text(iter_dir / "plan.json", "{}\n")
                    write_text(status, "changed\n")
            self.assertEqual(status.read_text(), "safe\n")


if __name__ == "__main__":
    unittest.main()
