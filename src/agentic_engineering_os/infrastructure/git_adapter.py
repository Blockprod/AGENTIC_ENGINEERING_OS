"""Minimal shell-free Git CLI adapter for isolated worktree operations."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitOperationError(RuntimeError):
    """A Git observation or mutation failed explicitly."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        command: tuple[str, ...] = (),
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class GitWorktree:
    path: Path
    head_commit: str
    branch_name: str | None


@dataclass(frozen=True, slots=True)
class _CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class GitAdapter:
    """Execute only the Git primitives required by WorktreeManager."""

    def __init__(self, repository_root: Path | str) -> None:
        self._repository_root = Path(repository_root).resolve(strict=False)

    @property
    def repository_root(self) -> Path:
        return self._repository_root

    def verify_repository(self) -> Path:
        if not self._repository_root.exists() or not self._repository_root.is_dir():
            raise GitOperationError(
                "INVALID_REPOSITORY", "repository root does not exist or is not a directory"
            )
        output = self._run("rev-parse", "--show-toplevel").stdout.strip()
        try:
            actual = Path(output).resolve(strict=True)
        except OSError as error:
            raise GitOperationError(
                "INVALID_REPOSITORY", "Git top-level path cannot be resolved"
            ) from error
        if _path_key(actual) != _path_key(self._repository_root):
            raise GitOperationError(
                "NOT_PRIMARY_WORKTREE",
                "repository root is not the primary worktree top level",
            )
        worktrees = self.list_worktrees()
        if not worktrees or _path_key(worktrees[0].path) != _path_key(actual):
            raise GitOperationError(
                "NOT_PRIMARY_WORKTREE", "primary worktree cannot be identified"
            )
        return actual

    def resolve_commit(self, commit: str) -> str:
        result = self._run("rev-parse", "--verify", f"{commit}^{{commit}}")
        resolved = result.stdout.strip().casefold()
        if len(resolved) != 40 or any(
            character not in "0123456789abcdef" for character in resolved
        ):
            raise GitOperationError("INVALID_COMMIT", "Git did not return a full commit SHA")
        return resolved

    def validate_branch_name(self, branch_name: str) -> None:
        result = self._run_allowed(
            (0, 1), "check-ref-format", "--branch", branch_name
        )
        if result.returncode != 0:
            raise GitOperationError(
                "INVALID_BRANCH",
                "branch name is rejected by Git",
                command=result.args,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
            )

    def branch_exists(self, branch_name: str) -> bool:
        branches = self._run(
            "for-each-ref", "--format=%(refname:strip=2)", "refs/heads"
        ).stdout.splitlines()
        requested = branch_name.casefold()
        return any(branch.casefold() == requested for branch in branches)

    def branch_tip(self, branch_name: str) -> str:
        return self.resolve_commit(f"refs/heads/{branch_name}")

    def list_worktrees(self) -> tuple[GitWorktree, ...]:
        output = self._run("worktree", "list", "--porcelain").stdout
        records: list[GitWorktree] = []
        current: dict[str, str] = {}
        for line in (*output.splitlines(), ""):
            if not line:
                if current:
                    path_text = current.get("worktree")
                    head = current.get("HEAD")
                    if path_text is None or head is None:
                        raise GitOperationError(
                            "INVALID_GIT_OUTPUT", "incomplete git worktree record"
                        )
                    branch_ref = current.get("branch")
                    branch = (
                        branch_ref.removeprefix("refs/heads/")
                        if branch_ref is not None
                        else None
                    )
                    records.append(
                        GitWorktree(
                            path=Path(path_text).resolve(strict=False),
                            head_commit=head.casefold(),
                            branch_name=branch,
                        )
                    )
                    current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        return tuple(records)

    def add_worktree(self, path: Path, branch_name: str, baseline_commit: str) -> None:
        self._run("worktree", "add", "-b", branch_name, str(path), baseline_commit)

    def remove_worktree(self, path: Path) -> None:
        self._run("worktree", "remove", str(path))

    def current_branch(self, worktree_path: Path) -> str:
        return self._run_at(worktree_path, "symbolic-ref", "--short", "HEAD").stdout.strip()

    def current_head(self, worktree_path: Path) -> str:
        result = self._run_at(worktree_path, "rev-parse", "HEAD").stdout.strip().casefold()
        if len(result) != 40 or any(
            character not in "0123456789abcdef" for character in result
        ):
            raise GitOperationError("INVALID_GIT_OUTPUT", "worktree HEAD is not a full SHA")
        return result

    def is_clean(self, worktree_path: Path, *, exclude_registry: bool = False) -> bool:
        arguments = ["status", "--porcelain=v1", "--untracked-files=all"]
        if exclude_registry:
            arguments.extend(
                [
                    "--",
                    ".",
                    ":(exclude).agentic-engineering-os/worktrees.json",
                ]
            )
        return not self._run_at(worktree_path, *arguments).stdout

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run_allowed(
            (0, 1), "merge-base", "--is-ancestor", ancestor, descendant
        )
        return result.returncode == 0

    def _run(self, *arguments: str) -> _CommandResult:
        return self._run_allowed((0,), *arguments)

    def _run_at(self, root: Path, *arguments: str) -> _CommandResult:
        return self._execute(root, (0,), *arguments)

    def _run_allowed(
        self, allowed_codes: tuple[int, ...], *arguments: str
    ) -> _CommandResult:
        return self._execute(self._repository_root, allowed_codes, *arguments)

    @staticmethod
    def _execute(
        root: Path,
        allowed_codes: tuple[int, ...],
        *arguments: str,
    ) -> _CommandResult:
        command = ("git", "-C", str(root), *arguments)
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            process = subprocess.run(
                list(command),
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                check=False,
            )
        except OSError as error:
            raise GitOperationError(
                "GIT_UNAVAILABLE",
                f"Git command could not start: {type(error).__name__}: {error}",
                command=command,
            ) from error
        result = _CommandResult(
            args=command,
            returncode=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
        )
        if process.returncode not in allowed_codes:
            raise GitOperationError(
                "GIT_COMMAND_FAILED",
                f"Git command failed with exit code {process.returncode}",
                command=command,
                stdout=process.stdout,
                stderr=process.stderr,
                exit_code=process.returncode,
            )
        return result


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False))).casefold()
