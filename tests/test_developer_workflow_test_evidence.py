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
    defect_reproduction_argv,
    defect_verification_prefix,
    select_defect_final_tests,
    select_requirement_final_tests,
    select_group_final_tests,
)
from pathlib import Path


NOW = datetime(2026, 8, 10, tzinfo=UTC)


def test_defect_runner_resolution_is_shared_with_approval_rebuilder(tmp_path: Path) -> None:
    from tests.test_developer_workflow_defect import _root_evidence, _selected_run
    from src.developer_workflow.defect_flow import DefectFlow
    from src.developer_workflow.approval_rebuilder import WorkflowApprovalRebuilder

    executable = tmp_path / ".venv" / "Scripts" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture: never executed")
    selected_mapping = mapping().validated_update(source_path=tmp_path, test_commands=())
    evidence = (_root_evidence().validated_update(reproduction_command="pytest"),)
    run = _selected_run(mapping=selected_mapping).validated_update(root_cause_evidence=evidence)
    expected = (str(executable.resolve()), "-m", "pytest", evidence[0].test_selector)
    assert DefectFlow._reproduction_invocation(run)[0] == expected
    assert WorkflowApprovalRebuilder._defect_reproduction_argv(run) == expected


@pytest.mark.parametrize(
    "runner", ("python -m pytest", "python3 -m pytest", "py -m pytest", "uv run pytest")
)
def test_discovered_pytest_runner_uses_selected_source_virtualenv(
    tmp_path: Path, runner: str,
) -> None:
    from tests.test_developer_workflow_defect import _root_evidence

    executable = tmp_path / ".venv" / "Scripts" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture: never executed")
    selected_mapping = mapping().validated_update(source_path=tmp_path, test_commands=())
    evidence = (_root_evidence().validated_update(reproduction_command=runner),)

    assert defect_reproduction_argv(evidence, selected_mapping) == (
        str(executable.resolve()), "-m", "pytest", evidence[0].test_selector,
    )


def test_configured_pytest_runner_preserves_flags_without_repeating_selector(
    tmp_path: Path,
) -> None:
    from tests.test_developer_workflow_defect import _root_evidence

    executable = tmp_path / ".venv" / "Scripts" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture: never executed")
    selector = _root_evidence().test_selector
    command = f"python -m pytest -q {selector}"
    selected_mapping = mapping().validated_update(
        source_path=tmp_path, test_commands=(command,),
    )
    evidence = (_root_evidence().validated_update(reproduction_command=command),)

    assert defect_reproduction_argv(evidence, selected_mapping) == (
        str(executable.resolve()), "-m", "pytest", "-q", selector,
    )


@pytest.mark.parametrize(
    "command_template",
    (
        "pytest --deselect {selector} tests",
        "pytest {selector} -q",
        "pytest {selector} {selector}",
    ),
)
def test_configured_pytest_runner_rejects_ambiguous_selector_position(
    command_template: str,
) -> None:
    from tests.test_developer_workflow_defect import _root_evidence

    selector = _root_evidence().test_selector
    command = command_template.format(selector=selector)
    selected_mapping = mapping().validated_update(test_commands=(command,))
    evidence = (_root_evidence().validated_update(reproduction_command=command),)

    with pytest.raises(FinalTestEvidenceError, match="selector position"):
        defect_reproduction_argv(evidence, selected_mapping)


def test_discovered_pytest_runner_falls_back_when_source_virtualenv_is_missing(
    tmp_path: Path,
) -> None:
    from tests.test_developer_workflow_defect import _root_evidence

    selected_mapping = mapping().validated_update(source_path=tmp_path, test_commands=())
    evidence = (_root_evidence().validated_update(reproduction_command="python -m pytest"),)

    assert defect_reproduction_argv(evidence, selected_mapping) == (
        "python", "-m", "pytest", evidence[0].test_selector,
    )


@pytest.mark.parametrize("base", ["python -c pass", "pytest -k ignored", "curl https://example.invalid"])
def test_unconfigured_runner_cannot_expand_the_safe_discovery_allowlist(base: str) -> None:
    from tests.test_developer_workflow_defect import _root_evidence

    evidence = (_root_evidence().validated_update(reproduction_command=base),)
    with pytest.raises(FinalTestEvidenceError):
        defect_reproduction_argv(evidence, mapping().validated_update(test_commands=()))


def test_sibling_regression_evidence_is_required_in_final_round() -> None:
    focused = ("python", "-m", "pytest", "tests/test_bug.py::test_bug")
    changed = ("src/app.py", "tests/test_bug.py", "tests/test_regression.py", "tests/conftest.py")
    prefix = defect_verification_prefix(focused, changed)
    assert prefix == (focused, ("python", "-m", "pytest", "tests/test_bug.py", "tests/test_regression.py"))
    from src.developer_workflow.command_utils import display_argv

    selected_mapping = mapping().validated_update(lint_commands=(), build_commands=(), test_commands=())
    records = tuple(result(display_argv(argv), argv) for argv in prefix)
    assert select_defect_final_tests(records, selected_mapping, reproduction_command=records[0].command,
                                    reproduction_argv=focused, changed_files=changed) == records
    with pytest.raises(FinalTestEvidenceError):
        select_defect_final_tests(records[:1], selected_mapping, reproduction_command=records[0].command,
                                 reproduction_argv=focused, changed_files=changed)


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
