from __future__ import annotations

import hashlib
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class WorkspaceGuardViolation(RuntimeError):
    """角色写入了白名单之外的路径时抛出的异常，携带角色名与越权路径清单。"""

    def __init__(self, role: str, violations: list[str]):
        """记录角色与越权项列表，并生成形如 "角色 modified paths outside its allowlist: ..." 的消息。"""
        self.role = role
        self.violations = violations
        super().__init__(f"{role} modified paths outside its allowlist: {', '.join(violations)}")


@dataclass
class _SnapshotEntry:
    """单个受保护文件的快照条目：工作区内相对路径、内容指纹、临时备份文件位置。"""

    rel: str
    digest: str
    backup: Path


def _sha256(path: Path) -> str:
    """以 1MB 分块读取文件并返回其 sha256 十六进制摘要，用于比对文件内容是否被改动。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class WorkspaceGuard:
    """工作区写入守卫：按角色白名单快照并还原越权改动，保证角色只能写自己该写的文件。"""

    def __init__(self, workspace: Path):
        """保存工作区根目录的绝对路径（已 resolve），后续所有相对路径判断都以它为基准。"""
        self.workspace = workspace.resolve()

    @contextmanager
    def protect(self, role: str, iter_dir: Path) -> Iterator[None]:
        """上下文管理器：进入时快照所有该角色不允许写的文件，退出时还原被越权改动的内容。

        iter_dir 为当前迭代目录，用于拼出角色允许写的相对路径。
        若发现新建、删除或修改了白名单外的路径，还原后抛出 WorkspaceGuardViolation。
        """
        iter_dir = iter_dir.resolve()
        with tempfile.TemporaryDirectory(prefix="sih-guard-") as tmp:
            backup_root = Path(tmp)
            snapshot = self._snapshot(role, iter_dir, backup_root)
            before_files = set(snapshot)
            try:
                yield
            finally:
                violations = self._restore_unauthorized(role, iter_dir, backup_root, snapshot, before_files)
            if violations:
                raise WorkspaceGuardViolation(role, violations)

    def _snapshot(
        self,
        role: str,
        iter_dir: Path,
        backup_root: Path,
    ) -> dict[str, _SnapshotEntry]:
        """遍历工作区，为该角色“不允许写”的每个文件或符号链接备份一份并记录指纹。

        返回以相对路径为键的快照字典；工作区尚不存在时返回空字典。
        符号链接不复制内容，只记录 "symlink:目标" 形式的指纹。
        """
        snapshot: dict[str, _SnapshotEntry] = {}
        if not self.workspace.exists():
            return snapshot
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file() and not path.is_symlink():
                continue
            rel = path.relative_to(self.workspace).as_posix()
            if self._is_allowed(role, rel, iter_dir):
                continue
            backup = backup_root / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                target = path.readlink()
                backup.symlink_to(target)
                digest = f"symlink:{target}"
            else:
                shutil.copy2(path, backup)
                digest = _sha256(path)
            snapshot[rel] = _SnapshotEntry(rel=rel, digest=digest, backup=backup)
        return snapshot

    def _restore_unauthorized(
        self,
        role: str,
        iter_dir: Path,
        backup_root: Path,
        snapshot: dict[str, _SnapshotEntry],
        before_files: set[str],
    ) -> list[str]:
        """还原越权改动并返回违规清单，元素形如 created:/deleted:/modified: 加相对路径。

        先删除白名单外新建的文件，再逐条比对快照指纹，把被删或被改的文件从备份还原。
        最后清理越权新建的空目录；返回空列表表示该角色本次没有越界。
        """
        violations: list[str] = []
        after_files = set()
        if self.workspace.exists():
            for path in self.workspace.rglob("*"):
                if path.is_file() or path.is_symlink():
                    after_files.add(path.relative_to(self.workspace).as_posix())

        for rel in sorted(after_files - before_files):
            if self._is_allowed(role, rel, iter_dir):
                continue
            target = self.workspace / rel
            if target.exists() or target.is_symlink():
                target.unlink()
            violations.append(f"created:{rel}")

        for rel, entry in snapshot.items():
            target = self.workspace / rel
            changed = False
            if not target.exists() and not target.is_symlink():
                changed = True
                violations.append(f"deleted:{rel}")
            elif target.is_symlink():
                digest = f"symlink:{target.readlink()}"
                changed = digest != entry.digest
            else:
                digest = _sha256(target)
                changed = digest != entry.digest
            if changed:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    target.unlink()
                if entry.backup.is_symlink():
                    target.symlink_to(entry.backup.readlink())
                else:
                    shutil.copy2(entry.backup, target)
                # 若该路径已被记为 created/deleted，就不再重复追加 modified，避免同一文件重复上报。
                if not any(item.endswith(rel) for item in violations):
                    violations.append(f"modified:{rel}")

        self._remove_empty_unauthorized_dirs(role, iter_dir)
        return violations

    def _remove_empty_unauthorized_dirs(self, role: str, iter_dir: Path) -> None:
        """清理该角色无权创建的空目录；由深到浅逐层尝试删除，非空目录的 OSError 被忽略。"""
        if not self.workspace.exists():
            return
        # 按路径层级从深到浅排序，确保子目录先于父目录被尝试删除。
        dirs = sorted((p for p in self.workspace.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
        for path in dirs:
            rel = path.relative_to(self.workspace).as_posix()
            if self._is_allowed(role, rel + "/", iter_dir):
                continue
            try:
                path.rmdir()
            except OSError:
                pass

    def _is_allowed(self, role: str, rel: str, iter_dir: Path) -> bool:
        """判断角色能否写工作区内相对路径 rel，允许返回 True，其余一律 False（默认拒绝）。

        规划者只能写 plan.json；执行者可写候选代码与执行产物；评估者、反思者各写自己的结论文件。
        .git、STATUS.md 以及 memory/ versions/ human/ runs/ 等治理目录对所有角色禁写。
        """
        iter_rel = iter_dir.relative_to(self.workspace).as_posix()
        if rel.startswith(".git/") or rel == ".git":
            return False
        banned_prefixes = ("memory/", "versions/", "human/", "runs/")
        banned_exact = {"STATUS.md"}
        if rel in banned_exact:
            return False
        if role == "planner":
            return rel == f"{iter_rel}/plan.json"
        if role == "executor":
            return (
                rel.startswith("target_repo/candidate/")
                or rel == f"{iter_rel}/execution.json"
                or rel == f"{iter_rel}/logs.txt"
                or rel == f"{iter_rel}/metrics.json"
                or rel.startswith(f"{iter_rel}/artifacts/")
            )
        if role == "evaluator":
            return rel == f"{iter_rel}/evaluation.json"
        if role == "reflector":
            return rel == f"{iter_rel}/reflection.json"
        if rel.startswith(banned_prefixes):
            return False
        return False
