# Claude 使用说明

本项目是基于可配置大模型接口的轻量多智能体自迭代框架。主流程必须由同一个接口依次运行规划者（Planner）、执行者（Executor）、评估者（Evaluator）和反思者（Reflector）。

```sh
export LLM_BASE_URL=https://your-endpoint/v1
export LLM_MODEL=your-model
export LLM_API_KEY=your-key
python -m algorithm_evoloop.cli llm-smoke
python -m algorithm_evoloop.cli run --human-decision rollback
```

示例任务协议位于 `examples/simple_case/task.json`。`SampleRunner` 只用于可重复的自动化测试，正常运行使用 `LLMRunner`。评估者可用的技能来自 `.claude/skills/*/SKILL.md`，由框架读取并注入提示词。

保持以下边界：

- 角色提示词不包含工作区路径、版本提交、完整历史或其他编排细节。
- Executor 只记录执行结果和原始指标，Evaluator 独立输出评估结论。
- 编排引擎只做结构校验和治理，不重算 `verdict`，也不改写 `proposed_action`。
- 反思者只提出治理建议，编排引擎记录并执行最终动作。
- 工具、技能、证据、长期记忆、人工决定和版本记录必须可追溯。
- 不提交真实业务背景、私有数据、私有指标、内部路径或可反推出具体场景的产物。
