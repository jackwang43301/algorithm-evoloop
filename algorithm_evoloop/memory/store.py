from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..schemas import append_jsonl, append_text, ensure_dir, read_json, utc_now, write_json, write_text


class MemoryStore:
    """长期记忆存储：把洞察、失败、禁止重复项以及人工决策落盘到工作区的 memory 目录。

    规划者读取这里的摘要作为上下文，反思者通过 apply_updates 写入新的记忆条目。
    """

    def __init__(self, workspace: Path):
        """初始化记忆目录下各文件路径：memory.json 为结构化主存，三个 Markdown 为可读追加日志。

        human_decisions.jsonl 以 JSONL 追加方式记录每次人工治理决定。此处只计算路径，不创建文件。
        """
        self.workspace = workspace
        self.memory_dir = workspace / "memory"
        self.memory_json = self.memory_dir / "memory.json"
        self.insights_md = self.memory_dir / "insights.md"
        self.failures_md = self.memory_dir / "failures.md"
        self.do_not_repeat_md = self.memory_dir / "do_not_repeat.md"
        self.human_decisions_jsonl = self.memory_dir / "human_decisions.jsonl"

    def initialize(self) -> None:
        """幂等地准备记忆目录与文件：缺失时才创建，已存在的文件内容保持不变。

        memory.json 初始化为 insights/failures/do_not_repeat 三个空列表，
        三个 Markdown 各写入标题行，人工决策 JSONL 初始化为空文件。
        """
        ensure_dir(self.memory_dir)
        if not self.memory_json.exists():
            write_json(self.memory_json, {"insights": [], "failures": [], "do_not_repeat": []})
        for path, title in [
            (self.insights_md, "# Insights\n\n"),
            (self.failures_md, "# Failures\n\n"),
            (self.do_not_repeat_md, "# Do Not Repeat\n\n"),
        ]:
            if not path.exists():
                write_text(path, title)
        if not self.human_decisions_jsonl.exists():
            write_text(self.human_decisions_jsonl, "")

    def summary(self) -> dict[str, Any]:
        """返回给规划者使用的记忆摘要：insights、failures、do_not_repeat 各取最新 5 条。

        另含 human_decisions 字段，为最近 5 条人工决策记录；调用前会先确保记忆文件已存在。
        """
        self.initialize()
        memory = read_json(self.memory_json, {"insights": [], "failures": [], "do_not_repeat": []})
        return {
            "insights": memory.get("insights", [])[-5:],
            "failures": memory.get("failures", [])[-5:],
            "do_not_repeat": memory.get("do_not_repeat", [])[-5:],
            "human_decisions": self._recent_human_decisions(limit=5),
        }

    def apply_updates(self, updates: list[dict[str, Any]], run_id: str, iteration: int) -> list[dict[str, Any]]:
        """把反思者产出的记忆更新写入存储，返回实际生效的条目列表。

        仅接受 type 为 insight/failure/do_not_repeat 的更新，其余类型被静默跳过；
        每条生成含 id、run_id、iteration、text、created_at 的条目，并追加到对应 Markdown。
        """
        self.initialize()
        memory = read_json(self.memory_json, {"insights": [], "failures": [], "do_not_repeat": []})
        applied = []
        for update in updates:
            kind = update["type"]
            if kind not in {"insight", "failure", "do_not_repeat"}:
                continue
            bucket = "insights" if kind == "insight" else "failures" if kind == "failure" else "do_not_repeat"
            # id 用桶名 + run_id + 三位迭代号 + 桶内序号拼成，保证同一次运行内不重复
            entry = {
                "id": f"{bucket}-{run_id}-iter-{iteration:03d}-{len(memory[bucket]) + 1}",
                "run_id": run_id,
                "iteration": iteration,
                "text": update["text"],
                "created_at": utc_now(),
            }
            memory[bucket].append(entry)
            applied.append(entry)
            line = f"- `{entry['id']}` {entry['text']}\n"
            if kind == "insight":
                append_text(self.insights_md, line)
            elif kind == "failure":
                append_text(self.failures_md, line)
            else:
                append_text(self.do_not_repeat_md, line)
        write_json(self.memory_json, memory)
        return applied

    def record_human_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        """把一条人工决策追加到 human_decisions.jsonl，返回补齐 id 后的副本。

        不修改传入字典；缺少 id 时按 run_id、三位迭代号与 role（默认 review）自动生成。
        """
        self.initialize()
        recorded = dict(decision)
        if not recorded.get("id"):
            scope = recorded.get("role", "review")
            recorded["id"] = (
                f"human-{recorded['run_id']}-iter-{int(recorded['iteration']):03d}-{scope}"
            )
        append_jsonl(self.human_decisions_jsonl, recorded)
        return recorded

    def _recent_human_decisions(self, limit: int) -> list[dict[str, Any]]:
        """读取人工决策 JSONL 的最后 limit 行并解析为字典列表。

        文件不存在时返回空列表；空行与无法解析的坏行被跳过，不会中断整体读取。
        """
        if not self.human_decisions_jsonl.exists():
            return []
        lines = self.human_decisions_jsonl.read_text(encoding="utf-8").splitlines()
        decisions = []
        for line in lines[-limit:]:
            if not line.strip():
                continue
            try:
                decisions.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return decisions
