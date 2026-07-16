"""Local Git workspace isolation: per-task worktrees allocated from a validated source repo.

The broker never executes a task directly against a caller-supplied workspace.
For isolated tasks it validates the workspace is a Git repository/worktree,
captures HEAD, and creates a broker-owned worktree under
`<home>/worktrees/<task-id>` on a deterministic branch. The source workspace
is read to discover its toplevel and HEAD; it is never written to.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WorkspaceError(ValueError):
    pass


def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result


def _git_bytes(args: list[str]) -> bytes:
    result = subprocess.run(["git", *args], capture_output=True)
    if result.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def canonical_source(workspace: str) -> str:
    """Resolve `workspace` to its Git toplevel directory, validating it is a repo/worktree."""
    path = Path(workspace)
    if not path.is_dir():
        raise WorkspaceError(f"Workspace is not a directory: {workspace}")
    result = _git(["-C", str(path), "rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        raise WorkspaceError(f"Workspace is not a Git repository or worktree: {workspace}")
    return str(Path(result.stdout.strip()).resolve())


def capture_head(source: str) -> str:
    return _git(["-C", source, "rev-parse", "HEAD"]).stdout.strip()


@dataclass(frozen=True)
class WorktreeAllocation:
    task_id: str
    source: str
    worktree_path: str
    branch: str
    base_sha: str


class WorkspaceManager:
    """Owns creation, diffing, and idempotent removal of broker-managed worktrees."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.worktrees_root = self.home / "worktrees"
        self.worktrees_root.mkdir(parents=True, exist_ok=True)

    def branch_name(self, task_id: str) -> str:
        return f"recollect/{task_id}"

    def worktree_path(self, task_id: str) -> Path:
        return self.worktrees_root / task_id

    def create_worktree(self, source: str, task_id: str, base_sha: str) -> WorktreeAllocation:
        branch = self.branch_name(task_id)
        path = self.worktree_path(task_id)
        if path.exists():
            raise WorkspaceError(f"Worktree path already exists: {path}")
        _git(["-C", source, "worktree", "add", "-b", branch, str(path), base_sha])
        return WorktreeAllocation(task_id=task_id, source=source, worktree_path=str(path), branch=branch, base_sha=base_sha)

    def capture_status(self, worktree_path: str, base_sha: str) -> dict[str, Any]:
        """Stage everything (including untracked files) and diff against base_sha.

        Staging first means the diff captures the full current state of the
        worktree — committed, staged, or merely modified/untracked — as one
        comparison against the captured base, regardless of how the task
        left its changes.
        """
        _git(["-C", worktree_path, "add", "-A"])
        name_status = _git(["-C", worktree_path, "diff", "--cached", "--name-status", base_sha])
        changed_paths = []
        for line in name_status.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            changed_paths.append({"status": parts[0], "paths": parts[1:]})
        diff_bytes = _git_bytes(["-C", worktree_path, "diff", "--cached", "--binary", base_sha])
        return {
            "changed_paths": changed_paths,
            "diff_bytes": diff_bytes,
            "diff_status": "changed" if changed_paths else "clean",
        }

    def release(self, source: str, worktree_path: str) -> dict[str, Any]:
        """Idempotently remove a broker-owned worktree. Never touches `source`'s content."""
        path = Path(worktree_path)
        try:
            path.resolve().relative_to(self.worktrees_root.resolve())
        except ValueError:
            raise WorkspaceError(f"Refusing to remove a path outside the broker-owned worktrees root: {path}")
        already_absent = not path.exists()
        removed_via_git = False
        if not already_absent:
            result = _git(["-C", source, "worktree", "remove", "--force", str(path)], check=False)
            removed_via_git = result.returncode == 0
            if not removed_via_git:
                shutil.rmtree(path, ignore_errors=True)
        _git(["-C", source, "worktree", "prune"], check=False)
        return {"already_absent": already_absent, "removed_via_git": removed_via_git, "path_remaining": path.exists()}
