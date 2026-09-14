from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from .engine import Engine
from .llm_client import LLMClient, LLMConfig, LLMConfigError
from .prompt_builder import PromptBuilder
from .role_runner import LLMRunner, RoleExecutionError
from .schemas import ensure_dir, validate_role_record, write_json


DEFAULT_ROLE_TIMEOUT_SECONDS = 180
ROLES = ("planner", "executor", "evaluator", "reflector")


def framework_root() -> Path:
    """返回框架根目录（本文件所在包的上一级），用于定位 prompts、examples 与 skills。"""
    return Path(__file__).resolve().parents[1]


def default_task_path() -> Path:
    """返回内置示例任务协议的默认路径：框架根目录下 examples/simple_case/task.json。"""
    return framework_root() / "examples" / "simple_case" / "task.json"


def default_workspace_path() -> Path:
    """推导默认工作区目录，名为 .algorithm_evoloop_workspace。

    若当前目录就在框架仓库内，则放到框架根目录的同级（避免产物污染仓库）；否则放在当前目录下。
    """
    root = framework_root().resolve()
    cwd = Path.cwd().resolve()
    # cwd 等于根目录或位于根目录之下，都视为“在仓库内运行”。
    if cwd == root or root in cwd.parents:
        return root.parent / ".algorithm_evoloop_workspace"
    return cwd / ".algorithm_evoloop_workspace"


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器，提供 run / init / llm-smoke 三个子命令。

    run 与 init 共享全部通用参数；llm-smoke 只需要工作区、大模型参数与角色超时。
    未指定子命令时由 main 兜底当作 run 处理。
    """
    parser = argparse.ArgumentParser(prog="algorithm-evoloop")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="运行四角色自迭代闭环")
    add_common_args(run_parser)

    init_parser = subparsers.add_parser("init", help="初始化运行工作区")
    add_common_args(init_parser)

    smoke_parser = subparsers.add_parser("llm-smoke", help="用一次真实接口调用验证 Planner 窗口")
    smoke_parser.add_argument("--workspace", type=Path, default=default_workspace_path())
    add_llm_args(smoke_parser)
    smoke_parser.add_argument("--role-timeout", type=int, default=DEFAULT_ROLE_TIMEOUT_SECONDS)
    return parser


def add_llm_args(parser: argparse.ArgumentParser) -> None:
    """给解析器挂上大模型接口相关参数（地址、模型、路径、密钥环境变量名、温度、重试、输出方式）。

    这些参数默认值都是 None，表示“未显式指定”，从而让配置加载时可按优先级回退。
    """
    parser.add_argument("--llm-config", type=Path, default=None, help="大模型接口配置 JSON 路径")
    parser.add_argument("--base-url", default=None, help="接口地址，例如 https://host/v1")
    parser.add_argument("--model", default=None, help="模型名称")
    parser.add_argument("--chat-path", default=None, help="对话补全路径，默认 /chat/completions")
    parser.add_argument("--api-key-env", default=None, help="读取密钥的环境变量名")
    parser.add_argument("--temperature", type=float, default=None, help="采样温度")
    parser.add_argument("--max-retries", type=int, default=None, help="单次调用的重试次数")
    parser.add_argument(
        "--structured-output",
        choices=["json_schema", "json_object", "text"],
        default=None,
        help="结构化输出方式，失败时自动降级",
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """给 run / init 子命令挂上通用参数：任务协议、工作区、轮数、版本适配器、大模型参数等。

    --iterations 为 None 时表示沿用 task.json 中的最大轮数；--pause-before 可重复指定多个角色。
    """
    parser.add_argument("--task", type=Path, default=default_task_path(), help="任务协议 JSON")
    parser.add_argument("--workspace", type=Path, default=default_workspace_path(), help="运行产物目录")
    parser.add_argument("--iterations", type=int, default=None, help="最大轮数；默认读取 task.json")
    parser.add_argument("--version-adapter", choices=["git", "file-snapshot"], default="git")
    add_llm_args(parser)
    parser.add_argument("--role-timeout", type=int, default=DEFAULT_ROLE_TIMEOUT_SECONDS)
    parser.add_argument("--quiet-role-io", action="store_true", help="不在终端打印角色输入输出")
    parser.add_argument(
        "--human-decision",
        choices=["keep", "rollback", "branch", "block"],
        default=None,
        help="为非交互 Human Review 预设治理动作",
    )
    parser.add_argument(
        "--pause-before",
        choices=ROLES,
        action="append",
        default=[],
        help="在指定角色前暂停，允许人工追加指导；可重复使用",
    )


def build_runner(args: argparse.Namespace, root: Path, prompts_dir: Path) -> LLMRunner:
    """按“命令行参数 > 环境变量 > 配置文件”的优先级组装配置，返回可驱动四角色的 LLMRunner。

    命令行未给出的项传入 None，交由 LLMConfig.load 回退到环境变量再回退到配置文件默认值；
    配置不合法时由 LLMConfig.load 抛出 LLMConfigError，由调用方转成退出码 2。
    """
    config = LLMConfig.load(
        config_path=args.llm_config,
        overrides={
            "base_url": args.base_url,
            "model": args.model,
            "chat_path": args.chat_path,
            "api_key_env": args.api_key_env,
            "temperature": args.temperature,
            "max_retries": args.max_retries,
            "structured_output": args.structured_output,
            "timeout_seconds": args.role_timeout,
        },
    )
    return LLMRunner(
        client=LLMClient(config),
        prompt_builder=PromptBuilder(prompts_dir),
        skill_root=root / ".claude" / "skills",
    )


def main(argv: list[str] | None = None) -> int:
    """命令行入口：解析参数、构建运行器并分派到 llm-smoke / init / run。

    返回进程退出码：配置无效返回 2；run 结束时状态为 blocked 返回 1；其余成功返回 0。
    未显式指定子命令时默认按 run 执行。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    root = framework_root()
    prompts_dir = root / "prompts"
    try:
        runner = build_runner(args, root, prompts_dir)
    except LLMConfigError as exc:
        print(f"大模型接口配置无效：{exc}")
        return 2
    if command == "llm-smoke":
        return run_llm_smoke(args, runner)

    engine = Engine(
        task_path=args.task,
        workspace=args.workspace,
        framework_root=root,
        version_adapter=args.version_adapter,
        runner=runner,
        prompts_dir=prompts_dir,
        show_role_io=not args.quiet_role_io,
        human_decision=args.human_decision,
        pause_before=set(args.pause_before),
    )
    if command == "init":
        result = engine.initialize()
        print(f"Initialized workspace: {result['workspace']}")
        return 0

    summary = engine.run(max_iterations=args.iterations)
    print(f"Run complete: {summary['run_id']}")
    print(f"Final champion: {summary['current_champion']['score']}")
    print(f"Status: {summary['status']}")
    if summary["status"] == "blocked":
        print(f"Next action: {summary['next_action']}")
    return 0 if summary["status"] != "blocked" else 1


def run_llm_smoke(args: argparse.Namespace, runner: LLMRunner) -> int:
    """用一次真实接口调用做连通性冒烟：让规划者产出一份最小 plan.json 并校验结构。

    产物写在工作区 runs/llm_smoke_<时间戳>/iter_001 下；成功返回 0。
    调用或写盘失败返回 1，若异常带回原始响应则落盘到 .llm_logs/planner.error.json 便于排查。
    """
    builder = runner.prompt_builder
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    iter_dir = ensure_dir(args.workspace / "runs" / f"llm_smoke_{stamp}" / "iter_001")
    context = {
        "iteration_id": "iter_001",
        "objective": "验证接口能生成最小 Planner 记录。",
        "metric": {"name": "score", "direction": "maximize", "initial": 0.0, "target": 1.0},
        "champion": {"score": 0.0},
        "rules": ["只生成结构化计划。"],
        "memory": {},
    }
    prompt = builder.dynamic_prompt("planner", context)
    try:
        raw = runner.execute("planner", prompt, args.workspace, iter_dir, context)
        runner.write_log("planner", raw, iter_dir)
        record = runner.collect("planner", raw, iter_dir, args.workspace)
        validate_role_record("planner", record)
    except (OSError, RoleExecutionError) as exc:
        print(f"llm-smoke failed: {exc}")
        if isinstance(exc, RoleExecutionError) and exc.raw:
            write_json(iter_dir / ".llm_logs" / "planner.error.json", exc.raw)
        return 1
    print("llm-smoke passed")
    print(f"record: {iter_dir / 'plan.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
