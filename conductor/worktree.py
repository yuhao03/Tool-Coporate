"""git worktree 隔离: 让并发 acting 步骤各自在独立 worktree 改文件, 避免冲突。

流程(每个 acting 步骤):
1. prepare(): 基于当前 HEAD 建一个新分支的 worktree
2. 后端在该 worktree 里改文件 (req.cwd = worktree)
3. commit(): 提交该 worktree 的改动
4. merge_back(): 顺序(加锁)把分支合并回主工作树; 冲突则放弃合并并返回冲突文件
5. cleanup(): 移除 worktree 与分支

要求: 主工作目录是干净的 git 仓库; 否则降级为不隔离(在主目录直接跑)。
"""

from __future__ import annotations

import logging
import secrets
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("conductor.worktree")

_AUTHOR_NAME = "Conductor"
_AUTHOR_EMAIL = "conductor@local"


def _git(args: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str, str]:
    proc = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def is_git_repo(d: Path) -> bool:
    rc, _, _ = _git(["rev-parse", "--is-inside-work-tree"], d)
    return rc == 0


def current_diff(d: Path, max_chars: int = 8000) -> str:
    """返回 d 相对 HEAD 的未提交改动 diff(供 GLM 审核); 非 git 仓库返回空串。"""
    if not is_git_repo(d):
        return ""
    rc, out, _ = _git(["diff", "HEAD"], d)
    if rc != 0 or not out.strip():
        rc, out, _ = _git(["diff"], d)
    out = (out or "").strip()
    return out[:max_chars] if out else ""


def has_git() -> bool:
    from shutil import which
    return which("git") is not None


class WorktreeIsolator:
    """为并发的 acting 步骤提供 worktree 隔离 + 顺序合并."""

    def __init__(self, main_dir: Path | str) -> None:
        self.main = Path(main_dir).resolve()
        self._lock = threading.RLock()
        self._created: list[tuple[Path, str]] = []  # (worktree_dir, branch)
        self._available: bool | None = None

    def available(self) -> bool:
        if self._available is None:
            self._available = has_git() and is_git_repo(self.main)
        return self._available

    def prepare(self, label: str) -> tuple[Path, str] | None:
        """建一个新分支 worktree, 返回 (worktree_dir, branch); 不可用/失败返回 None(降级)."""
        if not self.available():
            return None
        branch = f"conductor/{label}-{secrets.token_hex(3)}"
        wt = self.main.parent / ".conductor-worktrees" / branch.replace("/", "-")
        rc, _, err = _git(["worktree", "add", "-b", branch, str(wt)], self.main)
        if rc != 0:
            log.warning("worktree 创建失败: %s", err.strip())
            return None
        self._created.append((wt, branch))
        return wt, branch

    def commit(self, worktree: Path, message: str) -> bool:
        """提交 worktree 内所有改动(含空提交), 内置作者身份避免依赖全局配置."""
        if not self.available():
            return False
        _git(["add", "-A"], worktree)
        rc, _, err = _git(
            ["-c", f"user.name={_AUTHOR_NAME}", "-c", f"user.email={_AUTHOR_EMAIL}",
             "commit", "--allow-empty", "-m", message],
            worktree,
        )
        if rc != 0:
            log.warning("worktree 提交失败: %s", err.strip())
            return False
        return True

    def merge_back(self, branch: str) -> tuple[bool, list[str] | str]:
        """把分支合并回主工作树(顺序加锁). 返回 (ok, 冲突文件列表或错误文本)."""
        with self._lock:
            rc, out, err = _git(
                ["-c", f"user.name={_AUTHOR_NAME}", "-c", f"user.email={_AUTHOR_EMAIL}",
                 "merge", "--no-ff", "--no-edit", branch],
                self.main,
            )
            if rc == 0:
                return True, []
            # 冲突: 取冲突文件名, 然后放弃合并保持主树干净
            _, cout, _ = _git(["diff", "--name-only", "--diff-filter=U"], self.main)
            conflicts = [f for f in cout.strip().splitlines() if f]
            _git(["merge", "--abort"], self.main)
            return False, conflicts or err.strip() or "合并失败(未知原因)"

    def cleanup(self) -> None:
        for wt, branch in self._created:
            _git(["worktree", "remove", "--force", str(wt)], self.main)
            _git(["branch", "-D", branch], self.main)
        self._created = []
        # 尝试清理空的 worktree 容器目录
        container = self.main.parent / ".conductor-worktrees"
        try:
            if container.exists() and not any(container.iterdir()):
                container.rmdir()
        except OSError:
            pass
