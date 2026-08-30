"""Conservative Git reconstruction for Codex execution recovery."""

from pathlib import Path

from agentic_engineering_os.application.codex_runtime import GitExecutionObservation

from .git_adapter import GitAdapter, GitOperationError


class ExecutionGitObserver:
    def observe(self, cwd: str) -> GitExecutionObservation:
        path = Path(cwd)
        head = None
        clean = None
        changed_paths = None
        errors: list[str] = []
        try:
            adapter = GitAdapter(path)
            head = adapter.current_head(path)
        except (GitOperationError, OSError, ValueError) as error:
            errors.append(f"HEAD: {type(error).__name__}: {error}")
        try:
            adapter = GitAdapter(path)
            clean = adapter.is_clean(path, exclude_execution_state=True)
        except (GitOperationError, OSError, ValueError) as error:
            errors.append(f"CLEAN: {type(error).__name__}: {error}")
        try:
            adapter = GitAdapter(path)
            changed_paths = tuple(
                item
                for item in adapter.worktree_changed_paths(path)
                if item != ".agentic-engineering-os/executions.json"
                and not (
                    item.startswith(".agentic-engineering-os/.executions.")
                    and item.endswith(".tmp")
                )
            )
        except (GitOperationError, OSError, ValueError) as error:
            errors.append(f"PATHS: {type(error).__name__}: {error}")
        return GitExecutionObservation(
            head, clean, "; ".join(errors) or None, changed_paths
        )
