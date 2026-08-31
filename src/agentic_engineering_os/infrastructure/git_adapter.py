"""Minimal shell-free Git CLI adapter for isolated worktree operations."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


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
class GitPrimaryState:
    branch_name: str
    head_commit: str
    clean: bool


@dataclass(frozen=True, slots=True)
class GitReadOnlyState:
    """Repository facts observed with optional Git locks disabled."""

    top_level: Path
    branch_name: str | None
    detached: bool
    head_commit: str
    clean: bool
    worktrees: tuple[GitWorktree, ...]


@dataclass(frozen=True, slots=True)
class GitDiffEntry:
    status: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GitMergePreflight:
    mergeable: bool


@dataclass(frozen=True, slots=True)
class GitMergeResult:
    merged: bool
    head_commit: str


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

    def observe_read_only(self) -> GitReadOnlyState:
        """Observe Git identity and worktrees without refreshing the index."""

        if not self._repository_root.exists() or not self._repository_root.is_dir():
            raise GitOperationError(
                "INVALID_REPOSITORY", "repository root does not exist or is not a directory"
            )
        environment = {"GIT_OPTIONAL_LOCKS": "0"}
        top_result = self._execute(
            self._repository_root,
            (0, 128),
            "rev-parse",
            "--show-toplevel",
            environment_overrides=environment,
        )
        if top_result.returncode != 0:
            raise GitOperationError(
                "NOT_GIT_REPOSITORY",
                "target path is not inside a Git worktree",
                command=top_result.args,
                exit_code=top_result.returncode,
            )
        try:
            top_level = Path(top_result.stdout.strip()).resolve(strict=True)
        except OSError as error:
            raise GitOperationError(
                "INVALID_GIT_OUTPUT", "Git top-level path cannot be resolved"
            ) from error
        head_result = self._execute(
            self._repository_root,
            (0,),
            "rev-parse",
            "HEAD",
            environment_overrides=environment,
        )
        head = head_result.stdout.strip().casefold()
        if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
            raise GitOperationError(
                "INVALID_GIT_OUTPUT", "Git HEAD is not a full commit SHA"
            )
        branch_result = self._execute(
            self._repository_root,
            (0, 1),
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            environment_overrides=environment,
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
        if branch_result.returncode == 0 and not branch:
            raise GitOperationError(
                "INVALID_GIT_OUTPUT", "Git returned an empty branch name"
            )
        status_result = self._execute(
            self._repository_root,
            (0,),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            environment_overrides=environment,
        )
        worktree_result = self._execute(
            self._repository_root,
            (0,),
            "worktree",
            "list",
            "--porcelain",
            environment_overrides=environment,
        )
        worktrees = _parse_worktrees(worktree_result.stdout)
        if not worktrees:
            raise GitOperationError(
                "INVALID_GIT_OUTPUT", "Git returned no worktree records"
            )
        return GitReadOnlyState(
            top_level=top_level,
            branch_name=branch,
            detached=branch is None,
            head_commit=head,
            clean=not status_result.stdout,
            worktrees=worktrees,
        )

    def is_ignored(self, relative_path: str) -> bool:
        """Return Git's ignore decision for one canonical repository-relative path."""

        if (
            not isinstance(relative_path, str)
            or not relative_path
            or "\\" in relative_path
            or relative_path.startswith("/")
        ):
            raise GitOperationError(
                "INVALID_RELATIVE_PATH", "ignore query path must be canonical and relative"
            )
        candidate = PurePosixPath(relative_path)
        if str(candidate) != relative_path or any(
            part in {"", ".", ".."} for part in candidate.parts
        ):
            raise GitOperationError(
                "INVALID_RELATIVE_PATH", "ignore query path must be canonical and relative"
            )
        result = self._run_allowed(
            (0, 1), "check-ignore", "--quiet", "--no-index", "--", relative_path
        )
        return result.returncode == 0

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
        return _parse_worktrees(output)

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

    def is_clean(
        self,
        worktree_path: Path,
        *,
        exclude_registry: bool = False,
        exclude_execution_state: bool = False,
    ) -> bool:
        arguments = ["status", "--porcelain=v1", "--untracked-files=all"]
        if exclude_registry or exclude_execution_state:
            exclusions = []
            if exclude_registry:
                exclusions.append(":(exclude).agentic-engineering-os/worktrees.json")
            if exclude_execution_state:
                exclusions.extend(
                    (
                        ":(exclude).agentic-engineering-os/executions.json",
                        ":(exclude).agentic-engineering-os/.executions.*.tmp",
                    )
                )
            arguments.extend(
                [
                    "--",
                    ".",
                    *exclusions,
                ]
            )
        return not self._run_at(worktree_path, *arguments).stdout

    def worktree_changed_paths(self, worktree_path: Path) -> tuple[str, ...]:
        """Return every tracked or untracked path from strict porcelain output."""

        output = self._run_at(
            worktree_path,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ).stdout
        fields = output.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(fields):
            field = fields[index]
            index += 1
            if not field:
                continue
            if len(field) < 4 or field[2] != " ":
                raise GitOperationError(
                    "INVALID_GIT_OUTPUT", "worktree status entry is malformed"
                )
            status = field[:2]
            paths.append(field[3:].replace("\\", "/"))
            if "R" in status or "C" in status:
                if index >= len(fields) or not fields[index]:
                    raise GitOperationError(
                        "INVALID_GIT_OUTPUT", "rename/copy source path is absent"
                    )
                paths.append(fields[index].replace("\\", "/"))
                index += 1
        normalized = tuple(
            sorted(set(paths), key=lambda value: (value.casefold(), value))
        )
        if any(not value or value.startswith("/") or ".." in Path(value).parts for value in normalized):
            raise GitOperationError("INVALID_GIT_OUTPUT", "worktree path is unsafe")
        return normalized

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run_allowed(
            (0, 1), "merge-base", "--is-ancestor", ancestor, descendant
        )
        return result.returncode == 0

    def commit_parents(self, commit: str) -> tuple[str, ...]:
        fields = self._run("rev-list", "--parents", "-n", "1", commit).stdout.split()
        if not fields or self.resolve_commit(fields[0]) != self.resolve_commit(commit):
            raise GitOperationError(
                "INVALID_GIT_OUTPUT", "Git did not return the requested commit"
            )
        parents = tuple(item.casefold() for item in fields[1:])
        if any(
            len(item) != 40
            or any(character not in "0123456789abcdef" for character in item)
            for item in parents
        ):
            raise GitOperationError(
                "INVALID_GIT_OUTPUT", "Git returned an invalid parent commit"
            )
        return parents

    def primary_state(self) -> GitPrimaryState:
        self.verify_repository()
        return GitPrimaryState(
            branch_name=self.current_branch(self._repository_root),
            head_commit=self.current_head(self._repository_root),
            clean=self.is_clean(self._repository_root, exclude_registry=True),
        )

    def diff_name_status(
        self, baseline_commit: str, result_commit: str
    ) -> tuple[GitDiffEntry, ...]:
        output = self._run(
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--diff-filter=ACDMR",
            f"{baseline_commit}..{result_commit}",
        ).stdout
        tokens = output.split("\0")
        if tokens and tokens[-1] == "":
            tokens.pop()
        entries: list[GitDiffEntry] = []
        index = 0
        while index < len(tokens):
            status_token = tokens[index]
            index += 1
            status = status_token[:1]
            path_count = 2 if status in {"C", "R"} else 1
            if status not in {"A", "C", "D", "M", "R"} or index + path_count > len(tokens):
                raise GitOperationError(
                    "INVALID_GIT_OUTPUT", "Git diff name-status output is malformed"
                )
            paths = tuple(tokens[index : index + path_count])
            index += path_count
            if any(not path for path in paths):
                raise GitOperationError(
                    "INVALID_GIT_OUTPUT", "Git diff returned an empty path"
                )
            entries.append(GitDiffEntry(status=status, paths=paths))
        return tuple(entries)

    def merge_preflight(
        self,
        baseline_commit: str,
        left_commit: str,
        right_commit: str,
    ) -> GitMergePreflight:
        common_dir_text = self._run(
            "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout.strip()
        try:
            common_objects = (Path(common_dir_text).resolve(strict=True) / "objects")
        except OSError as error:
            raise GitOperationError(
                "INVALID_REPOSITORY", "Git common object directory is unavailable"
            ) from error
        if not common_objects.is_dir():
            raise GitOperationError(
                "INVALID_REPOSITORY", "Git common object directory is unavailable"
            )
        with tempfile.TemporaryDirectory(prefix="agentic-os-merge-tree-") as temporary:
            temporary_objects = Path(temporary) / "objects"
            temporary_objects.mkdir()
            result = self._execute(
                self._repository_root,
                (0, 1),
                "merge-tree",
                "--write-tree",
                "--quiet",
                "--merge-base",
                baseline_commit,
                left_commit,
                right_commit,
                environment_overrides={
                    "GIT_OBJECT_DIRECTORY": str(temporary_objects),
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common_objects),
                },
            )
        return GitMergePreflight(mergeable=result.returncode == 0)

    def merge_no_ff(
        self,
        worktree_path: Path,
        commit: str,
        *,
        message: str,
    ) -> GitMergeResult:
        """Merge one exact commit, preserving an explicit integration boundary."""

        result = self._execute(
            worktree_path,
            (0, 1),
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            message,
            commit,
        )
        return GitMergeResult(
            merged=result.returncode == 0,
            head_commit=self.current_head(worktree_path),
        )

    def merge_in_progress(self, worktree_path: Path) -> bool:
        result = self._execute(
            worktree_path,
            (0, 1, 128),
            "rev-parse",
            "--verify",
            "MERGE_HEAD",
        )
        return result.returncode == 0

    def abort_merge(self, worktree_path: Path) -> None:
        self._run_at(worktree_path, "merge", "--abort")

    def fast_forward(
        self, worktree_path: Path, expected_old: str, commit: str
    ) -> str:
        """Advance a clean checkout only when Git proves a fast-forward."""

        if self.current_head(worktree_path) != expected_old:
            raise GitOperationError(
                "STALE_REF", "worktree HEAD differs from the expected old commit"
            )
        if not self.is_clean(worktree_path, exclude_registry=True):
            raise GitOperationError(
                "DIRTY_WORKTREE", "worktree became dirty before fast-forward"
            )
        self._run_at(worktree_path, "merge", "--ff-only", commit)
        return self.current_head(worktree_path)

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
        environment_overrides: dict[str, str] | None = None,
    ) -> _CommandResult:
        command = ("git", "-C", str(root), *arguments)
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        if environment_overrides:
            environment.update(environment_overrides)
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


def _parse_worktrees(output: str) -> tuple[GitWorktree, ...]:
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
        if not key or key in current:
            raise GitOperationError(
                "INVALID_GIT_OUTPUT", "malformed Git worktree record"
            )
        current[key] = value
    return tuple(records)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False))).casefold()
