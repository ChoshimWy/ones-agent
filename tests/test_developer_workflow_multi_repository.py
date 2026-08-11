from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from pathlib import Path

import pytest

from src.contracts import WikiPageSnapshot
from src.developer_workflow.approval import approval_fingerprint, validate_for_approval
from src.developer_workflow.defect_flow import validate_group_root_cause_evidence
from src.developer_workflow.contracts import (
    AcceptanceCoverage,
    ApprovalPackage,
    CodexResult,
    CommandOutcome,
    CommandResult,
    PreparedWorktree,
    RepositoryChangeClaim,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRole,
    RepositoryApprovalEvidence,
    RepositoryRunEvidence,
    RepositorySnapshot,
    RootCauseEvidence,
    RootCauseSupportingPoint,
)
from src.developer_workflow.group_evidence import (
    GroupEvidenceError,
    aggregate_claims,
    assert_group_commands_passed,
    assert_group_claims,
    assert_group_snapshots_equal,
    run_group_commands,
)
from src.developer_workflow.repository_group import PreparedRepository


OID = "a" * 40


def _group(tmp_path: Path) -> tuple[RepositoryGroupMapping, tuple[PreparedRepository, ...]]:
    mappings = (
        RepositoryMapping(
            key="shared-sdk", project_id="project", iteration_id="iteration",
            repo_url="https://example.invalid/shared-sdk.git", repo_name="shared-sdk",
            role=RepositoryRole.DEPENDENCY,
            lint_commands=("ruff check src",), test_commands=("pytest",),
            allowed_paths=("src", "tests"),
        ),
        RepositoryMapping(
            key="desktop-app", project_id="project", iteration_id="iteration",
            repo_url="https://example.invalid/desktop-app.git", repo_name="desktop-app",
            role=RepositoryRole.PRIMARY, depends_on=("shared-sdk",),
            build_commands=("python -m build",), test_commands=("pytest",),
            allowed_paths=("src", "tests"),
        ),
    )
    group = RepositoryGroupMapping(
        key="desktop-suite", project_id="project", iteration_id="iteration",
        primary_repository="desktop-app", repositories=mappings,
        integration_test_commands=("pytest tests/integration",),
    )
    prepared: list[PreparedRepository] = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for mapping in mappings:
        worktree = workspace / mapping.key
        mirror = tmp_path / f"{mapping.key}.git"
        worktree.mkdir()
        mirror.mkdir()
        prepared.append(PreparedRepository(
            repository_key=mapping.key,
            mapping=mapping,
            prepared=PreparedWorktree(
                path=worktree.resolve(), branch=f"bugfix/DEF-1-{mapping.key}",
                base_commit=OID, head_commit=OID, mirror_path=mirror.resolve(),
            ),
        ))
    return group, tuple(prepared)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def run(self, command: str, *, cwd: Path) -> CommandResult:
        self.calls.append((command, cwd))
        now = datetime.now(UTC)
        return CommandResult(
            command=command,
            argv=tuple(command.split()),
            exit_code=0,
            outcome=CommandOutcome.PASSED,
            summary="passed",
            started_at=now,
            finished_at=now,
        )


def test_group_commands_follow_topology_then_primary_integration(tmp_path: Path) -> None:
    group, prepared = _group(tmp_path)
    runner = RecordingRunner()

    repository_results, integration_results = run_group_commands(
        group, prepared, runner
    )

    assert [(key, result.command) for key, result in repository_results] == [
        ("shared-sdk", "ruff check src"),
        ("shared-sdk", "pytest"),
        ("desktop-app", "python -m build"),
        ("desktop-app", "pytest"),
    ]
    assert tuple(result.command for result in integration_results) == (
        "pytest tests/integration",
    )
    assert runner.calls[-1][1] == prepared[1].prepared.path


def test_group_rejects_ambiguous_integration_command_argv(tmp_path: Path) -> None:
    group, _ = _group(tmp_path)

    with pytest.raises(ValueError, match="integration commands"):
        group.validated_update(
            integration_test_commands=("pytest", "\"pytest\"")
        )
    with pytest.raises(ValueError, match="integration commands"):
        group.validated_update(integration_test_commands=("pytest",))


@pytest.mark.parametrize("failure_index", [0, 4])
def test_any_repository_or_integration_failure_rejects_group_evidence(
    tmp_path: Path, failure_index: int
) -> None:
    group, prepared = _group(tmp_path)

    class FailingRunner(RecordingRunner):
        def run(self, command: str, *, cwd: Path) -> CommandResult:
            result = super().run(command, cwd=cwd)
            if len(self.calls) - 1 == failure_index:
                return result.model_copy(
                    update={
                        "exit_code": 1,
                        "outcome": CommandOutcome.TEST_FAILED,
                        "summary": "failed",
                    }
                )
            return result

    repository_results, integration_results = run_group_commands(
        group, prepared, FailingRunner()
    )
    with pytest.raises(GroupEvidenceError, match="did not pass"):
        assert_group_commands_passed(repository_results, integration_results)


def test_repository_failure_stops_before_later_repositories_and_integration(
    tmp_path: Path,
) -> None:
    group, prepared = _group(tmp_path)

    class FailingFirstRunner(RecordingRunner):
        def run(self, command: str, *, cwd: Path) -> CommandResult:
            self.calls.append((command, cwd))
            now = datetime.now(UTC)
            return CommandResult(
                command=command,
                argv=tuple(command.split()),
                exit_code=1,
                outcome=CommandOutcome.COMMAND_ERROR,
                summary="failed",
                started_at=now,
                finished_at=now,
            )

    runner = FailingFirstRunner()
    repository_results, integration_results = run_group_commands(
        group, prepared, runner
    )

    assert len(repository_results) == 1
    assert integration_results == ()
    assert [command for command, _ in runner.calls] == ["ruff check src"]


def test_group_runner_rejects_substituted_argv(tmp_path: Path) -> None:
    group, prepared = _group(tmp_path)

    class SubstitutingRunner(RecordingRunner):
        def run(self, command: str, *, cwd: Path) -> CommandResult:
            result = super().run(command, cwd=cwd)
            return result.model_copy(update={"argv": ("echo", "substituted")})

    with pytest.raises(GroupEvidenceError):
        run_group_commands(group, prepared, SubstitutingRunner())


def test_group_claims_must_equal_all_repository_snapshots(tmp_path: Path) -> None:
    group, _ = _group(tmp_path)
    snapshots = {
        "shared-sdk": RepositorySnapshot(
            head_commit=OID, diff_sha256="b" * 64,
            changed_files=("src/shortcut.py",), patch="diff", is_clean=False,
        ),
        "desktop-app": RepositorySnapshot(
            head_commit=OID, diff_sha256="c" * 64,
            changed_files=("src/window.py",), patch="diff", is_clean=False,
        ),
    }
    result = CodexResult(
        summary="fixed",
        repository_changes=(
            RepositoryChangeClaim(repository_key="shared-sdk", path="src/shortcut.py"),
            RepositoryChangeClaim(repository_key="desktop-app", path="src/window.py"),
        ),
    )

    assert_group_claims(result, snapshots, group)

    with pytest.raises(GroupEvidenceError, match="do not match"):
        assert_group_claims(
            result.validated_update(repository_changes=result.repository_changes[:1]),
            snapshots,
            group,
        )


def test_group_claims_are_aggregated_in_model_order() -> None:
    result = CodexResult(
        summary="fixed",
        repository_changes=(
            RepositoryChangeClaim(repository_key="shared-sdk", path="src/a.py"),
            RepositoryChangeClaim(repository_key="desktop-app", path="src/b.py"),
            RepositoryChangeClaim(repository_key="shared-sdk", path="tests/test_a.py"),
        ),
    )

    assert aggregate_claims(result) == {
        "shared-sdk": ("src/a.py", "tests/test_a.py"),
        "desktop-app": ("src/b.py",),
    }


def test_group_snapshot_gate_compares_every_repository(tmp_path: Path) -> None:
    group, prepared = _group(tmp_path)
    snapshots = {
        item.repository_key: RepositorySnapshot(
            head_commit=OID,
            diff_sha256=("b" if item.repository_key == "shared-sdk" else "c") * 64,
            changed_files=("src/change.py",),
            patch="diff",
            is_clean=False,
        )
        for item in prepared
    }
    evidence = tuple(
        RepositoryRunEvidence(
            repository_key=item.repository_key,
            mapping=item.mapping,
            prepared_worktree=item.prepared,
            changed_files=snapshots[item.repository_key].changed_files,
            tested_snapshot=snapshots[item.repository_key],
        )
        for item in prepared
    )

    assert_group_snapshots_equal(evidence, snapshots, group)

    changed = dict(snapshots)
    changed["shared-sdk"] = changed["shared-sdk"].model_copy(
        update={"diff_sha256": "d" * 64}
    )
    with pytest.raises(GroupEvidenceError, match="differs from tested evidence"):
        assert_group_snapshots_equal(evidence, changed, group)


def test_acceptance_coverage_can_bind_files_to_repository_keys() -> None:
    coverage = AcceptanceCoverage(
        criterion_id="AC-1",
        criterion_text="window recreation remains safe",
        repository_files=(
            RepositoryChangeClaim(
                repository_key="shared-sdk", path="src/shortcut.py"
            ),
            RepositoryChangeClaim(
                repository_key="desktop-app", path="src/window.py"
            ),
        ),
        tests=("pytest",),
    )

    assert coverage.files == ()
    assert tuple(item.repository_key for item in coverage.repository_files) == (
        "shared-sdk",
        "desktop-app",
    )

    with pytest.raises(ValueError, match="one file claim mode"):
        AcceptanceCoverage(
            criterion_id="AC-1",
            criterion_text="invalid mixed mode",
            files=("src/window.py",),
            repository_files=(
                RepositoryChangeClaim(
                    repository_key="desktop-app", path="src/window.py"
                ),
            ),
            tests=("pytest",),
        )


def test_root_cause_paths_can_be_bound_to_distinct_repositories() -> None:
    evidence = RootCauseEvidence(
        file_path="src/window.py",
        repository_file=RepositoryChangeClaim(
            repository_key="desktop-app", path="src/window.py"
        ),
        location="Window.rebuild",
        symbol="Window.rebuild",
        mechanism="application reuses a shortcut owned by the dependency",
        code_excerpt="shortcut.activate()",
        reproduction_test="tests/test_shortcut.py",
        reproduction_file=RepositoryChangeClaim(
            repository_key="shared-sdk", path="tests/test_shortcut.py"
        ),
        test_selector="tests/test_shortcut.py::test_destroyed_shortcut",
        reproduction_command="pytest",
        confidence=0.9,
        insufficient_evidence=False,
        impacted_files=("src/window.py",),
        impacted_repository_files=(
            RepositoryChangeClaim(
                repository_key="desktop-app", path="src/window.py"
            ),
        ),
        fix_steps=("guard the destroyed shortcut",),
        supporting_points=(
            RootCauseSupportingPoint(
                kind="cross_file",
                description="dependency destroys the shortcut",
                source="shared-sdk",
                file_path="src/shortcut.py",
                repository_file=RepositoryChangeClaim(
                    repository_key="shared-sdk", path="src/shortcut.py"
                ),
                snippet="del self.shortcut",
                direct_root_cause=True,
            ),
        ),
    )

    assert evidence.repository_file is not None
    assert evidence.reproduction_file is not None
    assert evidence.supporting_points[0].repository_file is not None


def test_group_root_cause_verifies_each_file_in_its_repository(tmp_path: Path) -> None:
    group, prepared = _group(tmp_path)
    (prepared[0].prepared.path / "src").mkdir()
    (prepared[0].prepared.path / "tests").mkdir()
    (prepared[0].prepared.path / "src" / "shortcut.py").write_text(
        "def destroy():\n    del shortcut\n", encoding="utf-8"
    )
    (prepared[0].prepared.path / "tests" / "test_shortcut.py").write_text(
        "def test_destroyed_shortcut(): pass\n", encoding="utf-8"
    )
    (prepared[1].prepared.path / "src").mkdir()
    (prepared[1].prepared.path / "src" / "window.py").write_text(
        "def rebuild():\n    shortcut.activate()\n", encoding="utf-8"
    )
    evidence = RootCauseEvidence(
        file_path="src/window.py",
        repository_file=RepositoryChangeClaim(
            repository_key="desktop-app", path="src/window.py"
        ),
        location="rebuild",
        symbol="rebuild",
        mechanism="window uses a destroyed shortcut",
        code_excerpt="shortcut.activate()",
        reproduction_test="tests/test_shortcut.py",
        reproduction_file=RepositoryChangeClaim(
            repository_key="shared-sdk", path="tests/test_shortcut.py"
        ),
        test_selector="tests/test_shortcut.py::test_destroyed_shortcut",
        reproduction_command="pytest",
        confidence=0.9,
        insufficient_evidence=False,
        impacted_files=("src/window.py",),
        impacted_repository_files=(
            RepositoryChangeClaim(
                repository_key="desktop-app", path="src/window.py"
            ),
        ),
        fix_steps=("guard the destroyed shortcut",),
        supporting_points=(
            RootCauseSupportingPoint(
                kind="cross_file",
                description="dependency owns destruction",
                source="shared-sdk",
                file_path="src/shortcut.py",
                repository_file=RepositoryChangeClaim(
                    repository_key="shared-sdk", path="src/shortcut.py"
                ),
                snippet="del shortcut",
                direct_root_cause=True,
            ),
        ),
    )

    assert validate_group_root_cause_evidence(
        (evidence,), prepared=prepared, group=group
    ) == (evidence,)

    forged = evidence.model_copy(
        update={
            "repository_file": RepositoryChangeClaim(
                repository_key="shared-sdk", path="src/window.py"
            )
        }
    )
    with pytest.raises(Exception, match="root cause evidence"):
        validate_group_root_cause_evidence(
            (forged,), prepared=prepared, group=group
        )


def test_group_approval_fingerprint_binds_every_repository_and_integration(
    tmp_path: Path,
) -> None:
    group, prepared = _group(tmp_path)
    runner = RecordingRunner()
    repository_results, integration_results = run_group_commands(
        group, prepared, runner
    )
    by_key = {
        key: tuple(result for item_key, result in repository_results if item_key == key)
        for key in group.topological_keys()
    }
    content = "# Acceptance Criteria\n- lifecycle remains safe"
    wiki = WikiPageSnapshot(
        team_id="team", space_id="space", page_id="page", title="Requirement",
        version="1", updated_at="2026-08-11T00:00:00Z",
        normalized_content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        source_url="http://ones/wiki/page",
    )
    repositories = tuple(
        RepositoryApprovalEvidence(
            repository_key=item.repository_key,
            mapping=item.mapping,
            base_commit=item.prepared.base_commit,
            head_commit=item.prepared.head_commit,
            diff_hash=("b" if item.repository_key == "shared-sdk" else "c") * 64,
            diff_summary=f"diff {item.repository_key}",
            branch=item.prepared.branch,
            changed_files=("src/change.py",),
            tests=by_key[item.repository_key],
            tree_hash=("d" if item.repository_key == "shared-sdk" else "e") * 40,
            commit_message=f"fix: {item.repository_key}",
            pr_title=f"Fix {item.repository_key}",
            pr_body=f"Validated {item.repository_key}",
        )
        for item in prepared
    )
    package = ApprovalPackage(
        work_item_id="REQ-1", work_item_title="Lifecycle fix",
        work_item_status="open", source_versions={"ones": "1"},
        wiki_hashes={"page": wiki.content_sha256}, wiki_snapshots=(wiki,),
        repository_group=group, repositories=repositories,
        integration_tests=integration_results,
        coverage={"AC-1": "shared-sdk:src/change.py,desktop-app:src/change.py"},
        review=("reviewed",), risks=("low risk",),
        unrelated_changes_checked=True,
    )

    validated = validate_for_approval(package)
    original = approval_fingerprint(validated)
    changed_repository = repositories[0].model_copy(
        update={"diff_hash": "d" * 64}
    )
    drifted = package.model_copy(
        update={"repositories": (changed_repository, *repositories[1:])}
    )

    assert original != approval_fingerprint(drifted)
    changed_tree = repositories[0].model_copy(update={"tree_hash": "9" * 40})
    tree_drifted = package.model_copy(
        update={"repositories": (changed_tree, *repositories[1:])}
    )
    assert original != approval_fingerprint(tree_drifted)
