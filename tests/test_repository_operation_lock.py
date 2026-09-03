from __future__ import annotations

import pytest

from agentic_engineering_os.application import project_configuration_fingerprint
from agentic_engineering_os.infrastructure import (
    PersistenceError,
    RepositoryOperationLock,
    RepositoryReconnaissance,
)
from test_existing_repository_adoption import adopt, configuration, existing_repository, git


def test_repository_operation_lock_serializes_mutating_invocations(tmp_path) -> None:
    (tmp_path / ".agentic-engineering-os").mkdir()
    first = RepositoryOperationLock(tmp_path)
    assert tmp_path.resolve() not in first.lock_path.parents

    with first:
        assert first.lock_path.is_file()
        with pytest.raises(PersistenceError) as captured:
            with RepositoryOperationLock(tmp_path):
                pass
        assert captured.value.code == "CONCURRENT_MISSION_OPERATION"

    assert not first.lock_path.exists()
    with RepositoryOperationLock(tmp_path):
        assert first.lock_path.is_file()


def test_repository_operation_lock_is_invisible_to_mission_preflight(tmp_path) -> None:
    root = existing_repository(tmp_path)
    desired = configuration()
    adopt(root, desired)
    git(root, "add", ".")
    git(root, "commit", "-m", "adopted")

    with RepositoryOperationLock(root):
        profile = RepositoryReconnaissance().inspect(root)

    assert profile.git.clean.value is True
    assert profile.agentic_os.config_semantic_fingerprint == (
        project_configuration_fingerprint(desired)
    )
