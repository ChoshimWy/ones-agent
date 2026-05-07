from __future__ import annotations

import gc
import tempfile
import unittest
from pathlib import Path

from config.settings import GitSettings
from src.contracts import ExecutionRequest, ProjectRef, RepoResolution, RepoTarget
from src.core.engine import Engine
from src.services.execution_service import ExecutionService, ExecutionValidationError


def _make_request(**overrides: object) -> ExecutionRequest:
    request = ExecutionRequest(
        defect_id="ONES-BUG-123",
        project=ProjectRef(id="proj-1", name="Auth Platform"),
        repo_resolution=RepoResolution(
            defect_id="ONES-BUG-123",
            project=ProjectRef(id="proj-1", name="Auth Platform"),
            selected_repo=RepoTarget(
                repo_url="https://example.com/auth-platform.git",
                repo_name="auth-platform",
                default_branch="main",
            ),
            selected_branch="main",
            confidence=1.0,
            source="project_repo_mapping",
            rationale="Resolved from the configured project-to-repo mapping.",
        ),
        request_type="bugfix",
        proposed_branch_name="fix/ONES-BUG-123-fix-tenant-context",
        target_branch="main",
        requested_by="qa@example.com",
        reason="Fix tenant context",
        confidence=0.82,
        source="analysis",
        metadata={},
    )

    for key, value in overrides.items():
        setattr(request, key, value)
    return request


class FakeGitOps:
    def __init__(self, settings: GitSettings, work_dir: str):
        self.settings = settings
        self.work_dir = work_dir
        self.clone_calls = 0
        self.checkout_calls: list[tuple[Path, str, str, str]] = []
        self.commit_calls = 0
        self.push_calls = 0
        self.pr_calls = 0
        self.repo_dir = Path(work_dir) / "auth-platform"

    def clone_repo(self) -> Path:
        self.clone_calls += 1
        return self.repo_dir

    def checkout_branch(self, repo_dir: Path | str, work_item_id: str, work_type: str, title: str) -> str:
        path = Path(repo_dir)
        self.checkout_calls.append((path, work_item_id, work_type, title))
        prefix = "feat" if work_type == "requirement" else "fix"
        slug = title.lower().replace(" ", "-")
        return f"{prefix}/{work_item_id}-{slug}"

    def commit_changes(self, repo_dir: Path | str, work_item_id: str, summary: str) -> str:
        self.commit_calls += 1
        raise AssertionError("commit_changes should not be called in phase-1 execution")

    def push_branch(self, repo_dir: Path | str) -> None:
        self.push_calls += 1
        raise AssertionError("push_branch should not be called in phase-1 execution")

    def create_pr(self, repo_dir: Path | str, title: str, body: str, target_branch: str = "") -> str:
        self.pr_calls += 1
        raise AssertionError("create_pr should not be called in phase-1 execution")


class TestExecutionService(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = Engine(db_path=str(Path(self.temp_dir.name) / "agent.db"))
        self.created_git_ops: list[FakeGitOps] = []

        def factory(settings: GitSettings, work_dir: str) -> FakeGitOps:
            git_ops = FakeGitOps(settings, work_dir)
            self.created_git_ops.append(git_ops)
            return git_ops

        self.service = ExecutionService(
            work_dir="data/test-execution",
            git_ops_factory=factory,
            engine=self.engine,
        )

    def tearDown(self) -> None:
        del self.service
        del self.engine
        gc.collect()
        self.temp_dir.cleanup()

    def test_execute_creates_branch_for_supported_request(self):
        request = _make_request()

        result = self.service.execute(request)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.defect_id, "ONES-BUG-123")
        self.assertEqual(result.base_branch, "main")
        self.assertEqual(result.branch_name, "fix/ONES-BUG-123-fix-tenant-context")
        self.assertEqual(result.operations, ("clone_repo", "checkout_branch"))
        self.assertTrue(result.execution_id.startswith("exec-"))
        self.assertEqual(len(result.request_key), 64)
        self.assertFalse(result.idempotent_reuse)
        self.assertEqual(result.request_count, 1)
        self.assertEqual(len(self.created_git_ops), 1)
        git_ops = self.created_git_ops[0]
        self.assertEqual(git_ops.settings.repo_url, "https://example.com/auth-platform.git")
        self.assertEqual(git_ops.settings.default_branch, "main")
        self.assertEqual(git_ops.clone_calls, 1)
        self.assertEqual(
            git_ops.checkout_calls,
            [(Path("data/test-execution") / "auth-platform", "ONES-BUG-123", "defect", "Fix tenant context")],
        )
        self.assertEqual(git_ops.commit_calls, 0)
        self.assertEqual(git_ops.push_calls, 0)
        self.assertEqual(git_ops.pr_calls, 0)

    def test_execute_uses_stable_branch_naming_by_request_type(self):
        bugfix_result = self.service.execute(_make_request())

        requirement_request = _make_request(
            defect_id="ONES-REQ-321",
            repo_resolution=RepoResolution(
                defect_id="ONES-REQ-321",
                project=ProjectRef(id="proj-1", name="Auth Platform"),
                selected_repo=RepoTarget(
                    repo_url="https://example.com/auth-platform.git",
                    repo_name="auth-platform",
                    default_branch="main",
                ),
                selected_branch="main",
                confidence=1.0,
                source="project_repo_mapping",
                rationale="Resolved from the configured project-to-repo mapping.",
            ),
            request_type="requirement_development",
            proposed_branch_name="feat/ONES-REQ-321-add-tenant-context",
            reason="Add tenant context",
        )

        requirement_result = self.service.execute(requirement_request)

        self.assertEqual(bugfix_result.branch_name, "fix/ONES-BUG-123-fix-tenant-context")
        self.assertEqual(requirement_result.branch_name, "feat/ONES-REQ-321-add-tenant-context")
        self.assertEqual(len(self.created_git_ops), 2)
        self.assertEqual(self.created_git_ops[0].checkout_calls[0][2], "defect")
        self.assertEqual(self.created_git_ops[1].checkout_calls[0][2], "requirement")

    def test_execute_reuses_existing_record_for_duplicate_equivalent_requests(self):
        request = _make_request()

        first = self.service.execute(request)
        second = self.service.execute(request)

        self.assertEqual(first.execution_id, second.execution_id)
        self.assertEqual(first.request_key, second.request_key)
        self.assertEqual(second.branch_name, first.branch_name)
        self.assertTrue(second.idempotent_reuse)
        self.assertEqual(second.request_count, 2)
        self.assertEqual(len(self.created_git_ops), 1)
        self.assertEqual(self.created_git_ops[0].clone_calls, 1)
        self.assertEqual(len(self.created_git_ops[0].checkout_calls), 1)

    def test_execute_persists_execution_record_for_ui_and_api_reads(self):
        result = self.service.execute(_make_request())

        record = self.engine.get_execution_record(result.execution_id)
        assert record is not None
        listed = self.engine.list_execution_records(defect_id="ONES-BUG-123")

        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["requestType"], "bugfix")
        self.assertEqual(record["baseBranch"], "main")
        self.assertEqual(record["branchName"], "fix/ONES-BUG-123-fix-tenant-context")
        self.assertEqual(record["operations"], ("clone_repo", "checkout_branch"))
        self.assertEqual(record["requestCount"], 1)
        self.assertEqual(record["metadata"], {"requested_operations": ["branch_create"], "target_branch": "main"})
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], result.execution_id)

    def test_execute_blocks_analysis_only_or_unsupported_operations(self):
        analysis_only_request = _make_request(metadata={"analysis_only": True})
        with self.assertRaisesRegex(ExecutionValidationError, "Analysis-only requests are blocked"):
            self.service.execute(analysis_only_request)

        unsupported_request = _make_request(metadata={"requested_operations": ["branch_create", "push"]})
        with self.assertRaisesRegex(ExecutionValidationError, "only supports branch_create"):
            self.service.execute(unsupported_request)

        self.assertEqual(self.created_git_ops, [])

    def test_execute_never_invokes_commit_push_or_pr_helpers(self):
        request = _make_request(metadata={"requested_operations": ["branch_create"]})

        result = self.service.execute(request)

        self.assertEqual(result.branch_name, "fix/ONES-BUG-123-fix-tenant-context")
        self.assertEqual(len(self.created_git_ops), 1)
        git_ops = self.created_git_ops[0]
        self.assertEqual(git_ops.commit_calls, 0)
        self.assertEqual(git_ops.push_calls, 0)
        self.assertEqual(git_ops.pr_calls, 0)


if __name__ == "__main__":
    unittest.main()
