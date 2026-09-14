from __future__ import annotations

import hashlib
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..schemas import ensure_dir, read_json, utc_now, write_json, write_text


class VersionAdapter(ABC):
    """版本治理接口：把候选状态的保留、回退和分支操作与具体实现解耦。"""

    @abstractmethod
    def initialize(self, task: dict[str, Any]) -> dict[str, str]:
        """准备版本仓库并写入基线候选，返回工作区、候选仓库和基线提交标识。"""
        raise NotImplementedError

    @abstractmethod
    def commit_candidate(
        self, run_id: str, iteration: int, execution: dict[str, Any]
    ) -> dict[str, str]:
        """把本轮执行结果写成新的候选版本，返回补丁号、新提交和父提交。"""
        raise NotImplementedError

    @abstractmethod
    def keep(self, commit_sha: str) -> None:
        """保留该提交作为新的最优版本。"""
        raise NotImplementedError

    @abstractmethod
    def rollback(self, champion_commit: str) -> None:
        """把候选状态回退到当前最优版本，不影响计划、日志、长期记忆和产物。"""
        raise NotImplementedError

    @abstractmethod
    def branch(self, branch_name: str, commit_sha: str, champion_commit: str) -> None:
        """把该提交留在独立分支上备查，同时把候选状态回退到最优版本。"""
        raise NotImplementedError


class GitAdapter(VersionAdapter):
    """默认实现：只在隔离的 Git 仓库中管理编排引擎持有的候选状态。"""

    def __init__(self, workspace: Path, framework_root: Path):
        """记录工作区与框架根目录，候选状态固定位于 `<工作区>/target_repo/candidate`。"""
        self.workspace = workspace.resolve()
        self.framework_root = framework_root.resolve()
        self.target_repo = self.workspace / "target_repo"
        self.candidate_dir = self.target_repo / "candidate"

    def initialize(self, task: dict[str, Any]) -> dict[str, str]:
        """初始化仓库与 main 分支，首次运行按任务初始指标写入基线提交，并校验仓库未越界。"""
        self._validate_boundary()
        ensure_dir(self.candidate_dir)
        write_text(self.workspace / ".algorithm_evoloop_workspace", "owned-by-algorithm-evoloop\n")
        if not (self.target_repo / ".git").exists():
            self._git("init")
        self._git("config", "user.name", "algorithm-evoloop")
        self._git("config", "user.email", "algorithm-evoloop@example.invalid")
        self._git("checkout", "-B", "main")

        metric = task["metric"]
        if not self._has_head():
            self._write_candidate(
                0,
                {
                    "change_summary": "baseline",
                    "tool_calls": [],
                    "raw_metrics": {"score": float(metric["initial"])},
                },
            )
            self._git("add", "candidate")
            self._git("commit", "-m", "baseline candidate")

        top_level = Path(self._git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if top_level != self.target_repo:
            raise RuntimeError("version repository escaped the algorithm-evoloop workspace")
        return {
            "workspace": str(self.workspace),
            "target_repo": str(self.target_repo),
            "baseline_commit": self.current_commit(),
        }

    def commit_candidate(
        self, run_id: str, iteration: int, execution: dict[str, Any]
    ) -> dict[str, str]:
        """写入本轮候选状态并提交，无实际改动时提交空提交以保证每轮都有可追溯的版本。"""
        parent_commit = self.current_commit()
        patch_id = f"{run_id}-iter-{iteration:03d}"
        self._write_candidate(iteration, execution)
        self._git("add", "candidate")
        if self._has_staged_changes():
            self._git("commit", "-m", f"{patch_id} candidate")
        else:
            self._git("commit", "--allow-empty", "-m", f"{patch_id} candidate no-op")
        commit_sha = self.current_commit()
        self._git("tag", "-f", f"algorithm-evoloop/{patch_id}", commit_sha)
        return {"patch_id": patch_id, "commit_sha": commit_sha, "parent_commit": parent_commit}

    def keep(self, commit_sha: str) -> None:
        """保留决定只需确认提交存在，工作区已经处于该提交对应的状态。"""
        self._git("rev-parse", "--verify", commit_sha)

    def rollback(self, champion_commit: str) -> None:
        """把候选仓库硬重置回最优提交，只影响候选状态。"""
        self._git("reset", "--hard", champion_commit)

    def branch(self, branch_name: str, commit_sha: str, champion_commit: str) -> None:
        """先把本轮提交固定到指定分支便于后续复盘，再把主线回退到最优提交。"""
        self._git("branch", "-f", branch_name, commit_sha)
        self.rollback(champion_commit)

    def current_commit(self) -> str:
        """返回候选仓库 HEAD 的提交号。"""
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _write_candidate(self, iteration: int, execution: dict[str, Any]) -> None:
        """把本轮的改动摘要、原始指标和工具调用写入 `candidate/state.json`。"""
        ensure_dir(self.candidate_dir)
        write_json(
            self.candidate_dir / "state.json",
            {
                "iteration": iteration,
                "change_summary": execution["change_summary"],
                "raw_metrics": execution["raw_metrics"],
                "tool_calls": execution["tool_calls"],
            },
        )

    def _validate_boundary(self) -> None:
        """拒绝把工作区放在框架目录内部，避免版本操作影响框架自身文件。"""
        if self.workspace == self.framework_root or self.framework_root in self.workspace.parents:
            raise ValueError("Workspace must be outside the framework directory.")

    def _has_head(self) -> bool:
        """判断仓库是否已有提交，用于决定是否需要创建基线。"""
        return self._git("rev-parse", "--verify", "HEAD", check=False).returncode == 0

    def _has_staged_changes(self) -> bool:
        """判断暂存区是否有改动：`git diff --cached --quiet` 返回 1 表示存在差异。"""
        return self._git("diff", "--cached", "--quiet", check=False).returncode == 1

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """在候选仓库内执行 git 命令；不经过 shell，`check=True` 时失败会带上输出抛异常。"""
        result = subprocess.run(
            ["git", "-C", str(self.target_repo), *args],
            check=False,
            text=True,
            capture_output=True,
            shell=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"git command failed: {' '.join(args)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result


class FileSnapshotAdapter(VersionAdapter):
    """仅在显式禁用 Git 时使用的降级实现：用目录快照代替提交。"""

    def __init__(self, workspace: Path, framework_root: Path):
        """除候选目录外，额外准备 `.file_snapshot_versions` 用于存放历史快照。"""
        self.workspace = workspace.resolve()
        self.framework_root = framework_root.resolve()
        self.target_repo = self.workspace / "target_repo"
        self.candidate_dir = self.target_repo / "candidate"
        self.history_dir = self.workspace / ".file_snapshot_versions"

    def initialize(self, task: dict[str, Any]) -> dict[str, str]:
        """校验工作区边界并准备目录，首次运行生成基线快照，否则沿用最近一次快照。"""
        if self.workspace == self.framework_root or self.framework_root in self.workspace.parents:
            raise ValueError("Workspace must be outside the framework directory.")
        ensure_dir(self.candidate_dir)
        ensure_dir(self.history_dir)
        write_text(self.workspace / ".algorithm_evoloop_workspace", "owned-by-algorithm-evoloop\n")
        if not (self.history_dir / "latest.json").exists():
            metric = task["metric"]
            write_json(
                self.candidate_dir / "state.json",
                {
                    "iteration": 0,
                    "change_summary": "baseline",
                    "raw_metrics": {"score": float(metric["initial"])},
                    "tool_calls": [],
                },
            )
            commit = self._snapshot("baseline")
        else:
            commit = self._latest_commit()
        return {"workspace": str(self.workspace), "target_repo": str(self.target_repo), "baseline_commit": commit}

    def commit_candidate(
        self, run_id: str, iteration: int, execution: dict[str, Any]
    ) -> dict[str, str]:
        """写入本轮候选状态后生成一份快照，快照摘要充当提交号。"""
        parent = self._latest_commit()
        write_json(
            self.candidate_dir / "state.json",
            {
                "iteration": iteration,
                "change_summary": execution["change_summary"],
                "raw_metrics": execution["raw_metrics"],
                "tool_calls": execution["tool_calls"],
            },
        )
        patch_id = f"{run_id}-iter-{iteration:03d}"
        commit = self._snapshot(patch_id)
        return {"patch_id": patch_id, "commit_sha": commit, "parent_commit": parent}

    def keep(self, commit_sha: str) -> None:
        """快照实现中保留决定无需额外动作，当前候选目录即最优状态。"""
        return None

    def rollback(self, champion_commit: str) -> None:
        """用最优快照整体覆盖候选目录。"""
        source = self.history_dir / champion_commit / "candidate"
        if self.candidate_dir.exists():
            shutil.rmtree(self.candidate_dir)
        shutil.copytree(source, self.candidate_dir)

    def branch(self, branch_name: str, commit_sha: str, champion_commit: str) -> None:
        """快照实现没有分支概念，本轮快照已单独留存，因此只做回退。"""
        self.rollback(champion_commit)

    def _snapshot(self, label: str) -> str:
        """按候选状态内容与标签算出摘要作为快照标识，复制目录并更新 `latest.json`。"""
        payload = (self.candidate_dir / "state.json").read_bytes() + label.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.history_dir / digest
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(self.candidate_dir, destination / "candidate")
        write_json(self.history_dir / "latest.json", {"commit": digest, "created_at": utc_now()})
        return digest

    def _latest_commit(self) -> str:
        """读取最近一次快照标识，没有记录时返回 `none`。"""
        return read_json(self.history_dir / "latest.json", {}).get("commit", "none")
