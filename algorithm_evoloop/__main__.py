"""包入口：支持以 `python -m algorithm_evoloop` 的方式执行命令行主流程。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())

