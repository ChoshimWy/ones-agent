"""Controlled execution boundary for branch-creation-only GitOps flows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable

from config.settings import GitSettings
from src.contracts import ExecutionRequest, RepoResolution
from src.core.engine import Engine
from src.integrations.git_ops import GitOps, _slugify, build_branch_name

AllowedOperation = str


class ExecutionValidationError(ValueError):
    """Raised when an ExecutionRequest is unsafe or unsupported."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Structured result for phase-1 execution."""

    status: str
    defect_id: str
    repo_url: str
    base_branch: str
    branch_name: str = ""
    repo_dir: str = ""
    operations: tuple[AllowedOperation, ...] = field(default_factory=tuple)
    execution_id: str = ""
    request_key: str = ""
    idempotent_reuse: bool = False
    request_count: int = 1


@dataclass(slots=True)
class ExecutionService:
    """Validate a canonical execution request and create a branch only."""

    actionable_confidence_floor: float = 0.75
    work_dir: str = "data/repos"
    git_ops_factory: Callable[[GitSettings, str], GitOps] | None = None
    engine: Engine | None = None
    engine_db_path: str = "data/agent.db"

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        validated_request = self._validate_request(request)
        repo_resolution = validated_request.repo_resolution
        assert repo_resolution is not None  # Narrowed by _validate_request.

        base_branch = self._resolved_base_branch(validated_request, repo_resolution)
        branch_name = self._expected_branch_name(validated_request)
        request_key = self._request_key(validated_request, repo_resolution, base_branch, branch_name)
        engine = self._get_engine()
        existing_record = engine.get_execution_record_by_request_key(request_key)
        if existing_record is not None:
            existing_record = engine.note_execution_request(existing_record["id"])
            assert existing_record is not None
            if existing_record["status"] != "failed":
                return self._result_from_record(existing_record, repo_resolution.selected_repo.repo_url)

            engine.update_execution_record(
                existing_record["id"],
                status="in_progress",
                error_message="",
            )
            execution_id = existing_record["id"]
        else:
            created_record = engine.create_execution_record(
                request_key=request_key,
                defect_id=validated_request.defect_id,
                project_id=validated_request.project.id,
                project_name=validated_request.project.name,
                request_type=validated_request.request_type,
                repo_url=repo_resolution.selected_repo.repo_url,
                base_branch=base_branch,
                proposed_branch_name=branch_name,
                requested_by=validated_request.requested_by,
                reason=self._branch_title(validated_request),
                confidence=validated_request.confidence,
                source=validated_request.source,
                status="in_progress",
                operations=[],
                metadata=self._audit_metadata(validated_request),
            )
            assert created_record is not None
            execution_id = created_record["id"]

        git_settings = GitSettings(
            repo_url=repo_resolution.selected_repo.repo_url,
            default_branch=base_branch,
            _env_file=None,
        )
        try:
            git_ops = self._build_git_ops(git_settings)
            repo_dir = git_ops.clone_repo()
            branch_name = git_ops.checkout_branch(
                repo_dir,
                validated_request.defect_id,
                self._git_work_type(validated_request.request_type),
                self._branch_title(validated_request),
            )
            record = engine.update_execution_record(
                execution_id,
                status="completed",
                branch_name=branch_name,
                repo_dir=str(repo_dir),
                operations=["clone_repo", "checkout_branch"],
                error_message="",
            )
            assert record is not None
            return self._result_from_record(record, repo_resolution.selected_repo.repo_url)
        except Exception as exc:
            engine.update_execution_record(
                execution_id,
                status="failed",
                branch_name=branch_name,
                operations=[],
                error_message=str(exc),
            )
            raise

    def _get_engine(self) -> Engine:
        return self.engine or Engine(db_path=self.engine_db_path)

    def _build_git_ops(self, git_settings: GitSettings) -> GitOps:
        if self.git_ops_factory is not None:
            return self.git_ops_factory(git_settings, self.work_dir)
        return GitOps(git_settings, work_dir=self.work_dir)

    def _validate_request(self, request: ExecutionRequest) -> ExecutionRequest:
        defect_id = request.defect_id.strip()
        if not defect_id:
            raise ExecutionValidationError("ExecutionRequest.defect_id is required for branch creation.")
        if request.repo_resolution is None:
            raise ExecutionValidationError("ExecutionRequest.repo_resolution is required for branch creation.")

        repo_resolution = request.repo_resolution
        self._validate_repo_resolution(repo_resolution)

        if request.confidence < self.actionable_confidence_floor:
            raise ExecutionValidationError(
                "ExecutionRequest.confidence is below the phase-1 branch-creation threshold of 0.75."
            )

        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        if metadata.get("analysis_only"):
            raise ExecutionValidationError("Analysis-only requests are blocked by the execution service.")

        requested_operations = self._requested_operations(metadata)
        unsupported_operations = sorted(operation for operation in requested_operations if operation != "branch_create")
        if unsupported_operations:
            raise ExecutionValidationError(
                "Phase-1 execution only supports branch_create; unsupported operations requested: "
                + ", ".join(unsupported_operations)
            )

        if any(metadata.get(flag) for flag in ("commit", "push", "create_pr", "open_pr", "submit_pr")):
            raise ExecutionValidationError(
                "Phase-1 execution does not allow commit, push, or PR submission flags in ExecutionRequest metadata."
            )

        proposed_branch_name = request.proposed_branch_name.strip()
        if not proposed_branch_name:
            raise ExecutionValidationError("ExecutionRequest.proposed_branch_name is required in phase 1.")

        expected_branch_name = self._expected_branch_name(request)
        if proposed_branch_name != expected_branch_name:
            raise ExecutionValidationError(
                "Phase-1 execution only supports canonical branch names generated from the approved request intent."
            )

        return request

    @staticmethod
    def _validate_repo_resolution(repo_resolution: RepoResolution) -> None:
        if not (repo_resolution.selected_repo.repo_url.strip() or repo_resolution.selected_repo.repo_name.strip()):
            raise ExecutionValidationError("ExecutionRequest.repo_resolution.selected_repo must identify a repository.")
        if not repo_resolution.selected_branch.strip():
            raise ExecutionValidationError("ExecutionRequest.repo_resolution.selected_branch is required for branch creation.")

    @staticmethod
    def _requested_operations(metadata: dict[str, object]) -> tuple[str, ...]:
        raw_operations = metadata.get("requested_operations")
        if raw_operations is None:
            return ("branch_create",)
        if isinstance(raw_operations, str):
            operations = [raw_operations]
        elif isinstance(raw_operations, (list, tuple, set)):
            operations = [str(item) for item in raw_operations]
        else:
            raise ExecutionValidationError("ExecutionRequest.metadata.requested_operations must be a string or collection.")

        normalized: list[str] = []
        for operation in operations:
            cleaned = operation.strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
        return tuple(normalized or ["branch_create"])

    def _expected_branch_name(self, request: ExecutionRequest) -> str:
        branch_title = self._branch_title(request)
        slug = _slugify(branch_title)
        if not slug:
            raise ExecutionValidationError(
                "ExecutionRequest.reason must produce an ASCII-safe branch slug in phase 1."
            )
        return build_branch_name(
            request.defect_id.strip(),
            self._git_work_type(request.request_type),
            branch_title,
        )

    @staticmethod
    def _branch_title(request: ExecutionRequest) -> str:
        return request.reason.strip() or request.defect_id.strip()

    @staticmethod
    def _git_work_type(request_type: str) -> str:
        return "requirement" if request_type == "requirement_development" else "defect"

    @staticmethod
    def _resolved_base_branch(request: ExecutionRequest, repo_resolution: RepoResolution) -> str:
        return request.target_branch.strip() or repo_resolution.selected_branch.strip()

    @staticmethod
    def _request_key(
        request: ExecutionRequest,
        repo_resolution: RepoResolution,
        base_branch: str,
        branch_name: str,
    ) -> str:
        canonical_payload = {
            "base_branch": base_branch,
            "branch_name": branch_name,
            "defect_id": request.defect_id.strip(),
            "project_id": request.project.id.strip(),
            "repo_url": repo_resolution.selected_repo.repo_url.strip(),
            "request_type": request.request_type,
        }
        encoded = json.dumps(canonical_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _audit_metadata(request: ExecutionRequest) -> dict[str, object]:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        requested_operations = metadata.get("requested_operations")
        return {
            "requested_operations": requested_operations if requested_operations is not None else ["branch_create"],
            "target_branch": request.target_branch,
        }

    @staticmethod
    def _result_from_record(record: dict[str, object], repo_url: str) -> ExecutionResult:
        operations = record.get("operations")
        normalized_operations = operations if isinstance(operations, tuple) else tuple(operations or [])
        return ExecutionResult(
            status=str(record.get("status", "")),
            defect_id=str(record.get("defectId", "")),
            repo_url=str(record.get("repoUrl", repo_url) or repo_url),
            base_branch=str(record.get("baseBranch", "")),
            branch_name=str(record.get("branchName", "")),
            repo_dir=str(record.get("repoDir", "")),
            operations=normalized_operations,
            execution_id=str(record.get("id", "")),
            request_key=str(record.get("requestKey", "")),
            idempotent_reuse=int(record.get("requestCount", 1) or 1) > 1,
            request_count=int(record.get("requestCount", 1) or 1),
        )


__all__ = ["ExecutionResult", "ExecutionService", "ExecutionValidationError"]
