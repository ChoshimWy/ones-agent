from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.developer_workflow.contracts import (
    CommandResult,
    PreparedWorktree,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRole,
    RepositoryRunEvidence,
)
from src.developer_workflow.test_evidence import (
    FinalTestEvidenceError,
    select_defect_final_tests,
    select_requirement_final_tests,
    select_group_final_tests,
)
from pathlib import Path


NOW = datetime(2026, 8, 10, tzinfo=UTC)


def result(command: str, argv: tuple[str, ...]) -> CommandResult:
    return CommandResult(
        command=command, argv=argv, exit_code=0, summary="passed",
        started_at=NOW, finished_at=NOW,
    )


def mapping() -> RepositoryMapping:
    return RepositoryMapping(
        key="repo", project_id="P", iteration_id="I",
        repo_url="https://github.example/Team/Repo.git", repo_name="Repo",
        lint_commands=("ruff check src",), build_commands=("python -m build",),
        test_commands=("pytest -q",), allowed_paths=("src",),
    )


def test_requirement_selects_only_exact_last_round() -> None:
    configured = (
        result("ruff check src", ("ruff", "check", "src")),
        result("python -m build", ("python", "-m", "build")),
        result("pytest -q", ("pytest", "-q")),
    )
    old = result("pytest -q", ("pytest", "-q"))
    assert select_requirement_final_tests((old, *configured), mapping()) == configured


@pytest.mark.parametrize("mutation", ["short", "order", "argv"])
def test_requirement_rejects_incomplete_or_inexact_final_round(mutation) -> None:
    configured = [
        result("ruff check src", ("ruff", "check", "src")),
        result("python -m build", ("python", "-m", "build")),
        result("pytest -q", ("pytest", "-q")),
    ]
    if mutation == "short":
        configured.pop(0)
    elif mutation == "order":
        configured[-1], configured[-2] = configured[-2], configured[-1]
    else:
        configured[-1] = result("pytest -q", ("pytest",))
    with pytest.raises(FinalTestEvidenceError):
        select_requirement_final_tests(tuple(configured), mapping())


def test_defect_selects_focused_then_full_configured_sequence() -> None:
    selected = (
        result("pytest tests/test_bug.py::test_bug", ("pytest", "tests/test_bug.py::test_bug")),
        result("ruff check src", ("ruff", "check", "src")),
        result("python -m build", ("python", "-m", "build")),
        result("pytest -q", ("pytest", "-q")),
    )
    pre_fix = result("pytest tests/test_bug.py::test_bug", ("pytest", "tests/test_bug.py::test_bug"))
    assert select_defect_final_tests(
        (pre_fix, *selected), mapping(),
        reproduction_command=selected[0].command,
        reproduction_argv=selected[0].argv,
    ) == selected


def test_group_final_tests_follow_topology_then_integration() -> None:
    first = mapping().validated_update(
        key="sdk", repo_name="sdk", role=RepositoryRole.DEPENDENCY
    )
    second = mapping().validated_update(
        key="app", repo_name="app", depends_on=("sdk",)
    )
    group = RepositoryGroupMapping(
        key="suite", project_id="P", iteration_id="I",
        primary_repository="app", repositories=(first, second),
        integration_test_commands=("pytest integration",),
    )
    evidence = tuple(
        RepositoryRunEvidence(
            repository_key=item.key,
            mapping=item,
            prepared_worktree=PreparedWorktree(
                path=Path.cwd().resolve() / item.key,
                branch=f"codex/{item.key}",
                base_commit="a" * 40,
                head_commit="a" * 40,
                mirror_path=Path.cwd().resolve() / f"{item.key}.git",
            ),
            test_results=(
                result("ruff check src", ("ruff", "check", "src")),
                result("python -m build", ("python", "-m", "build")),
                result("pytest -q", ("pytest", "-q")),
            ),
        )
        for item in (first, second)
    )
    integration = (
        result("pytest integration", ("pytest", "integration")),
    )

    selected = select_group_final_tests(evidence, integration, group)

    assert tuple(key for key, _ in selected) == (
        "sdk", "sdk", "sdk", "app", "app", "app", "app",
    )
    with pytest.raises(FinalTestEvidenceError):
        select_group_final_tests(evidence, (), group)

    without_integration = group.validated_update(integration_test_commands=())
    assert len(select_group_final_tests(evidence, (), without_integration)) == 6
