from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from algorithm_evoloop.cli import build_parser, default_task_path, framework_root
from algorithm_evoloop.llm_client import LLMConfig


ROOT = Path(__file__).resolve().parents[1]


class StructureTest(unittest.TestCase):
    def test_public_cli_is_llm_api_only(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["run"])
        self.assertFalse(hasattr(args, "engine"))
        self.assertFalse(hasattr(args, "ducc_bin"))
        self.assertIsNone(args.iterations)
        self.assertIsNone(args.base_url)
        self.assertIsNone(args.model)
        self.assertIsNone(args.llm_config)
        self.assertEqual(args.task, ROOT / "examples" / "simple_case" / "task.json")
        env = {
            "LLM_BASE_URL": "https://example.invalid/v1",
            "LLM_MODEL": "demo-model",
            "LLM_API_KEY": "secret",
        }
        config = LLMConfig.load(env=env)
        self.assertEqual(config.base_url, "https://example.invalid/v1")
        self.assertEqual(config.model, "demo-model")
        self.assertEqual(config.endpoint, "https://example.invalid/v1/chat/completions")
        self.assertEqual(config.redacted()["api_key"], "***")

    def test_main_example_is_one_task_file(self) -> None:
        example = ROOT / "examples" / "simple_case"
        self.assertEqual([path.name for path in example.iterdir() if path.is_file()], ["task.json"])
        old_example = ROOT / "examples" / "toy_task"
        self.assertFalse(old_example.exists() and any(path.is_file() for path in old_example.rglob("*")))
        task = json.loads((example / "task.json").read_text())
        self.assertEqual(task["max_iterations"], 3)
        self.assertEqual([item["score"] for item in task["demo_fixtures"]], [0.62, 0.61, 0.78])
        self.assertEqual(task["executor_tools"][0]["name"], "mock_experiment_runner")
        self.assertEqual(task["evaluator_tools"][0]["name"], "mock_result_analyzer")
        self.assertEqual(task["evaluator_skills"][0]["name"], "code-review-skill")
        for key in ("executor_tools", "evaluator_tools", "evaluator_skills"):
            self.assertEqual(set(task[key][0]), {"name", "mode", "description"})

    def test_repository_root_is_the_single_resource_source(self) -> None:
        self.assertFalse((ROOT / "algorithm_evoloop" / "resources").exists())
        self.assertEqual(framework_root(), ROOT)
        self.assertEqual(default_task_path(), ROOT / "examples" / "simple_case" / "task.json")
        self.assertTrue((ROOT / "CLAUDE.md").is_file())
        self.assertTrue((ROOT / ".claude" / "settings.json").is_file())
        self.assertEqual(
            {path.name for path in (ROOT / "prompts").glob("*.md")},
            {"_shared_header.md", "planner.md", "executor.md", "evaluator.md", "reflector.md"},
        )
        self.assertTrue((ROOT / "examples" / "simple_case" / "task.json").is_file())
        self.assertTrue(
            (ROOT / ".claude" / "skills" / "code-review-skill" / "SKILL.md").is_file()
        )
        image = ROOT / "assets" / "algorithm_evoloop_framework.png"
        self.assertTrue(image.is_file())
        self.assertGreater(image.stat().st_size, 0)
        self.assertNotIn("package-data", (ROOT / "pyproject.toml").read_text())

    def test_skill_is_minimal_and_project_owned(self) -> None:
        skill_dir = ROOT / ".claude" / "skills" / "code-review-skill"
        files = [path.relative_to(skill_dir).as_posix() for path in skill_dir.rglob("*") if path.is_file()]
        self.assertEqual(files, ["SKILL.md"])
        text = (skill_dir / "SKILL.md").read_text()
        self.assertIn("name: code-review-skill", text)
        self.assertIn("代码审核通过", text)
        self.assertFalse((ROOT / "THIRD_PARTY_NOTICES.md").exists())

    def test_readme_centers_real_llm_loop_and_existing_figure(self) -> None:
        readme = (ROOT / "README.md").read_text()
        line_count = len(readme.splitlines())
        self.assertGreaterEqual(line_count, 200)
        self.assertLessEqual(line_count, 300)
        self.assertIn("assets/algorithm_evoloop_framework.png", readme)
        self.assertIn("--human-decision rollback", readme)
        self.assertNotIn("--engine sample", readme)
        self.assertNotIn("ducc", readme)

    def test_owned_docs_use_chinese_explanations_without_process_traces(self) -> None:
        docs = [ROOT / "README.md", ROOT / "CLAUDE.md", ROOT / "program.md"]
        process_phrases = [
            "这张图来自",
            "现有内部",
            "当前版本",
            "第一目标",
            "方法论汇报",
            "为什么使用简单 CASE",
            "主示例",
            "公开 CLI",
        ]
        unexplained_english = [
            "Multi-Agent",
            "Harness",
            "Memory",
            "Human Review",
            "Tool",
            "Skill",
            "Agent",
            "artifacts",
            "champion",
            "verdict",
            "record",
            "CASE",
            "Prompt",
            "fixture",
            "adapter",
            "Reactive",
        ]
        for path in docs:
            text = path.read_text(encoding="utf-8")
            for phrase in process_phrases:
                self.assertNotIn(phrase, text, f"process trace found in {path}: {phrase}")
            prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
            prose = re.sub(r"`[^`]+`", "", prose)
            for term in unexplained_english:
                self.assertNotIn(term, prose, f"untranslated term found in {path}: {term}")

        task = json.loads((ROOT / "examples" / "simple_case" / "task.json").read_text())
        self.assertNotIn(" score ", task["objective"])
        self.assertTrue(all("score=" not in item["tool_result"] for item in task["demo_fixtures"]))

    def test_owned_text_is_sanitized(self) -> None:
        banned = [
            re.compile(r"/Users/"),
            re.compile(r"/home/"),
            re.compile(r"\bparking\b", re.IGNORECASE),
            re.compile(r"\bslam\b", re.IGNORECASE),
            re.compile(r"\bROS\b"),
        ]
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".md", ".json", ".toml"}:
                continue
            if set(path.relative_to(ROOT).parts) & {".git", "build", "reference", "tests", "__pycache__"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in banned:
                self.assertIsNone(pattern.search(text), f"{pattern.pattern} found in {path}")


if __name__ == "__main__":
    unittest.main()
