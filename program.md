# 程序设计

`algorithm-evoloop` 由小型 Python 编排引擎、大模型接口角色窗口和普通文件记录组成：

```text
规划者（Planner）→ 执行者（Executor）→ 评估者（Evaluator）→ 反思者（Reflector）
                  → 编排引擎决策 → 长期记忆与版本治理 → 下一轮
```

- `cli.py`：提供 `run`、`init` 和 `llm-smoke` 命令；接口地址、模型、密钥来自配置文件、`LLM_*` 环境变量或命令行参数。
- `engine.py`：组装最小角色上下文，校验结构和文件引用，执行最终治理，更新长期记忆、人工审查、Git 和状态文件；不承担业务评估。
- `role_runner.py`：`LLMRunner` 是正常运行底座；`SampleRunner` 用于可重复测试。
- `llm_client.py`：只依赖标准库的对话补全客户端，负责结构化输出降级、重试和密钥脱敏。
- `prompt_builder.py`：按角色过滤输入，不把编排引擎的内部路径和版本信息交给角色。
- `schemas.py`：定义四类结构化记录、评估结论、反思建议和最终治理动作。
- `memory/store.py`、`human/review.py`、`tools/versioning.py`：分别负责长期记忆、人工介入和隔离的 Git 候选仓库。

编排引擎管理路径、版本提交和运行历史；角色只通过结构化记录自动交接。Executor 产出原始结果，Evaluator 负责评估结论，Reflector 负责治理建议。评估者的技能加载状态来自框架读取 `.claude/skills/*/SKILL.md` 后的实际注入结果，不只依赖模型声明。
