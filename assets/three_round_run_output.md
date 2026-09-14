# 三轮示例运行输出说明

运行命令：

```sh
python -m algorithm_evoloop.cli run \
  --workspace /tmp/algorithm-evoloop-demo \
  --human-decision rollback
```

该命令会打印每个角色的输入和输出。输出较长是有意设计：它展示了 Planner、Executor、Evaluator、Reflector 如何自动交接，以及每个角色到底基于什么信息做出结构化结果。

## 输出块如何阅读

每一轮都会按以下顺序打印：

```text
================ PLANNER INPUT ================
...给 Planner 的上下文...

================ PLANNER OUTPUT ================
...Planner 返回的计划 JSON...

================ EXECUTOR INPUT ================
...计划、工具和当轮 fixture...

================ EXECUTOR OUTPUT ================
...执行结果、原始指标、日志和产物...

================ EVALUATOR INPUT ================
...执行结果、证据要求、评估规则和技能...

================ EVALUATOR OUTPUT ================
...评估结论、证据检查、技能调用结果...

================ REFLECTOR INPUT ================
...评估结论和人工审查规则...

================ REFLECTOR OUTPUT ================
...治理建议和记忆更新...
```

重点不是逐字阅读所有 JSON，而是确认四件事：

1. Planner 是否读取了目标、当前最优值和 Memory，并产出 `plan.json`。
2. Executor 是否只产出原始实验事实，例如 `raw_metrics`、`logs`、`artifacts`。
3. Evaluator 是否基于证据、评估规则、工具和 Skill 产出 `verdict`。
4. Reflector 是否把评估结论转化为 `proposed_action` 和 `memory_updates`。

## 关键字段

| 字段 | 出现位置 | 含义 |
|---|---|---|
| `hypothesis` | Planner 输出 | 本轮想验证的假设 |
| `memory_used` | Planner 输出 | 本轮计划引用了哪些长期记忆或人工决定 |
| `steps` | Planner 输出 | 本轮计划执行的步骤 |
| `success_criteria` | Planner 输出 | 本轮认定成功的条件 |
| `change_summary` | Executor 输出 | 本轮实际做了什么改动 |
| `tool_calls` | Executor / Evaluator 输出 | 本轮调用或模拟调用了哪些工具 |
| `raw_metrics` | Executor 输出 | 实验产生的原始观测，不是评估结论 |
| `logs` | Executor 输出 | 工具运行日志，与 `logs.txt` 对应 |
| `artifacts` | Executor 输出 | 本轮生成的产物路径，例如 `artifacts/evidence.json` |
| `metric` | Evaluator 输出 | 候选值与当前最优值的对比，含指标名和优化方向 |
| `evidence_complete` | Evaluator 输出 | Evaluator 对证据是否齐全的判断 |
| `evidence_paths` | Evaluator 输出 | 实际找到的证据路径 |
| `missing_evidence` | Evaluator 输出 | 缺失的必要证据 |
| `skill_calls` | Evaluator 输出 | 评估阶段加载或调用的 Skill |
| `code_review` | Evaluator 输出 | 代码审核结论，来自 Skill 调用结果 |
| `verdict` | Evaluator 输出 | 评估结论，例如 `improved` 或 `insufficient_evidence` |
| `human_review` | Evaluator / Reflector 输出 | 是否需要人工介入以及触发原因 |
| `proposed_action` | Reflector 输出 | 建议治理动作，例如 `keep`、`rollback`、`request_human_review` |
| `reason` | Reflector 输出 | 给出该治理动作的理由 |
| `memory_updates` | Reflector 输出 | 本轮要写入长期记忆的经验、失败或 do-not-repeat |

## 三轮关键输出节选

以下节选按当前代码的实际记录结构整理（省略了与说明无关的字段）。分数和证据完整性由 `examples/simple_case/task.json` 的预置结果决定，因此每次运行一致；角色文本的具体措辞由所用模型决定，会有差异。

### 第一轮：有效提升，保留

```json
{
  "iteration_id": "iter_001",
  "hypothesis": "采用一项小而可验证的改动可以推进目标。",
  "memory_used": [],
  "steps": [
    "选择一个小改动",
    "调用 mock_experiment_runner",
    "收集指标、日志和证据"
  ],
  "success_criteria": [
    "分数优于当前 champion",
    "证据完整",
    "代码审核通过"
  ]
}
```

```json
{
  "change_summary": "执行第一项小改动并收集完整证据。",
  "raw_metrics": {
    "score": 0.62
  },
  "artifacts": [
    "artifacts/evidence.json"
  ]
}
```

```json
{
  "metric": {
    "name": "score",
    "candidate": 0.62,
    "champion": 0.4,
    "direction": "maximize"
  },
  "evidence_complete": true,
  "missing_evidence": [],
  "code_review": {
    "passed": true,
    "result": "代码审核通过"
  },
  "verdict": "improved",
  "human_review": {
    "required": false,
    "trigger": null
  }
}
```

```json
{
  "proposed_action": "keep",
  "reason": "指标改善、证据完整且代码审核通过。",
  "memory_updates": [
    {
      "type": "insight",
      "text": "保留有完整证据的小改动。"
    }
  ]
}
```

含义：第一轮候选分数从 `0.40` 提升到 `0.62`，证据完整且代码审核通过，因此 Reflector 建议 `keep`。

### 第二轮：证据不足，人工决定回退

```json
{
  "iteration_id": "iter_002",
  "memory_used": [
    "insights-run_...-iter-001-1"
  ],
  "steps": [
    "选择一个小改动",
    "调用 mock_experiment_runner",
    "补齐并核对完整证据"
  ]
}
```

```json
{
  "change_summary": "尝试第二项改动，但没有形成完整证据链。",
  "raw_metrics": {
    "score": 0.61
  },
  "artifacts": []
}
```

```json
{
  "metric": {
    "name": "score",
    "candidate": 0.61,
    "champion": 0.62,
    "direction": "maximize"
  },
  "evidence_complete": false,
  "evidence_paths": [
    "metrics.json",
    "logs.txt"
  ],
  "missing_evidence": [
    "artifacts/evidence.json"
  ],
  "verdict": "insufficient_evidence",
  "human_review": {
    "required": true,
    "trigger": "insufficient_evidence"
  }
}
```

```json
{
  "proposed_action": "request_human_review",
  "reason": "候选证据不足，不能自动保留。",
  "memory_updates": [
    {
      "type": "failure",
      "text": "本轮候选因证据不足无法确认。"
    },
    {
      "type": "do_not_repeat",
      "text": "不要再次提交证据不完整的候选。"
    }
  ]
}
```

含义：第二轮 `score=0.61` 低于当前最优 `0.62`，并且缺少 `artifacts/evidence.json`。Evaluator 以 `insufficient_evidence` 触发人工审查；命令行参数 `--human-decision rollback` 让 Engine 在非交互模式下执行回退。

### 第三轮：读取记忆后补齐证据，达到目标

```json
{
  "iteration_id": "iter_003",
  "memory_used": [
    "insights-run_...-iter-001-1",
    "failures-run_...-iter-002-1",
    "do_not_repeat-run_...-iter-002-1",
    "human-run_...-iter-002-review"
  ]
}
```

```json
{
  "change_summary": "根据失败记忆补齐证据后再次执行小改动。",
  "raw_metrics": {
    "score": 0.78
  },
  "artifacts": [
    "artifacts/evidence.json"
  ]
}
```

```json
{
  "metric": {
    "name": "score",
    "candidate": 0.78,
    "champion": 0.62,
    "direction": "maximize"
  },
  "evidence_complete": true,
  "verdict": "improved"
}
```

```json
{
  "proposed_action": "keep",
  "reason": "指标改善、证据完整且代码审核通过。"
}
```

最终输出：

```text
Run complete: run_<timestamp>_<id>
Final champion: 0.78
Status: target_reached
```

含义：第三轮 Planner 的 `memory_used` 同时包含第一轮经验、第二轮失败、do-not-repeat 与人工回退决定；Executor 产出完整证据和 `score=0.78`；Evaluator 判定改善；Reflector 建议保留，最终达到目标。

## 输出与文件记录的关系

终端输出只是实时展示。每个角色的完整 JSON 会写入 `--workspace` 指定目录：

```text
/tmp/algorithm-evoloop-demo/runs/<run_id>/iter_001/plan.json
/tmp/algorithm-evoloop-demo/runs/<run_id>/iter_001/execution.json
/tmp/algorithm-evoloop-demo/runs/<run_id>/iter_001/evaluation.json
/tmp/algorithm-evoloop-demo/runs/<run_id>/iter_001/reflection.json
```

如果终端输出太长，可以主要查看这些文件和：

```text
/tmp/algorithm-evoloop-demo/runs/<run_id>/run_summary.json
/tmp/algorithm-evoloop-demo/versions/version_records.jsonl
/tmp/algorithm-evoloop-demo/STATUS.md
```
