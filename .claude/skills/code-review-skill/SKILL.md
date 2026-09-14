---
name: code-review-skill
description: Review a candidate change and return a concise pass/fail result. Use when algorithm-evoloop Evaluator requests code review, including the bundled no-code demonstration that verifies Skill loading.
---

# Code Review

1. Check whether the input contains a real candidate change.
2. If the input explicitly says this is the bundled demonstration with no real code, do not invent findings. Record `skill_loaded=true`, `passed=true`, and `result=代码审核通过`.
3. If a real change is present, check correctness, scope, evidence, and obvious risk. Pass only when no blocking issue is found.
4. Return the result through the Evaluator record requested by the caller. Keep the result concise.
