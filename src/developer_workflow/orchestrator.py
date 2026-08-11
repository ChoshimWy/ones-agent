"""Command boundary for approval-gated developer workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import unicodedata

from .approval import issue_approval
from .config import DeveloperWorkflowConfig, RepositoryMappingNotFound
from .contracts import PublicationResult, WorkflowRun, WorkflowState, WorkflowType
from .defect_flow import DefectCandidateService, DefectFlow
from .publisher import Publisher
from .requirement_flow import RequirementFlow
from .state_store import FileRunStore


class InvalidWorkflowAction(RuntimeError):
    """Raised when a command is unsafe for the persisted workflow state."""


_BIDI_CONTROLS = {
    "LRE",
    "RLE",
    "LRO",
    "RLO",
    "PDF",
    "LRI",
    "RLI",
    "FSI",
    "PDI",
}
_BIDI_CONTROL_CHARACTERS = {
    "\u061c",  # Arabic letter mark
    "\u200e",  # left-to-right mark
    "\u200f",  # right-to-left mark
}
_INVALID_REVISION_SCOPE = (
    "revision scope is invalid; start a new defect run to rebuild evidence"
)


def _validated_text(value: str, *, kind: str, max_length: int) -> str:
    """Validate command metadata without reflecting untrusted text in errors."""

    if type(value) is not str or value != value.strip() or not value:
        raise InvalidWorkflowAction(f"{kind} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise InvalidWorkflowAction(f"{kind} is invalid") from None
    if (
        len(value) > max_length
        or len(encoded) > max_length * 4
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            or unicodedata.bidirectional(character) in _BIDI_CONTROLS
            or character in _BIDI_CONTROL_CHARACTERS
            for character in value
        )
    ):
        raise InvalidWorkflowAction(f"{kind} is invalid")
    return value


def _validated_revision_scope(
    scope: Literal["implementation", "repair"] | None,
) -> Literal["implementation", "repair"] | None:
    if scope is None:
        return None
    try:
        validated = _validated_text(scope, kind="revision scope", max_length=32)
    except InvalidWorkflowAction:
        raise InvalidWorkflowAction(_INVALID_REVISION_SCOPE) from None
    if validated == "implementation":
        return "implementation"
    if validated == "repair":
        return "repair"
    raise InvalidWorkflowAction(_INVALID_REVISION_SCOPE)


@dataclass(slots=True)
class DeveloperWorkflowOrchestrator:
    """Coordinate persisted workflow commands through injected services."""

    store: FileRunStore
    requirement_flow: RequirementFlow
    defect_flow: DefectFlow
    publisher: Publisher
    config: DeveloperWorkflowConfig
    defect_candidates: DefectCandidateService

    def start_requirement(self, requirement_id: str) -> WorkflowRun:
        created = self.store.create(
            WorkflowRun.new(WorkflowType.REQUIREMENT, requirement_id)
        )
        with self.store.operation_lock(created.run_id, "orchestrate"):
            current = self.store.load(created.run_id)
            return self.requirement_flow.execute(current)

    def start_defect(
        self,
        project_id: str,
        iteration_id: str,
        assignee: str,
        snapshot_token: str,
        candidate_id: str,
    ) -> WorkflowRun:
        selected = self.defect_candidates.select(
            snapshot_token,
            candidate_id,
            project_id=project_id,
            iteration_id=iteration_id,
            assignee_id=assignee,
        )
        defect = selected.defect
        if (
            selected.type is not WorkflowType.DEFECT
            or defect is None
            or selected.work_item_id != defect.defect_id
            or selected.candidate_id != defect.defect_id
            or selected.project_id != project_id
            or selected.iteration_id != iteration_id
            or selected.assignee_id != assignee
            or defect.project.id != project_id
            or defect.assignee is None
            or defect.assignee.id != assignee
        ):
            raise InvalidWorkflowAction(
                "defect selection requires a verified candidate snapshot"
            )
        created = self.store.create(selected)
        with self.store.operation_lock(created.run_id, "orchestrate"):
            current = self.store.load(created.run_id)
            return self.defect_flow.execute(current)

    def show(self, run_id: str) -> WorkflowRun:
        return self.store.load(run_id)

    def confirm_repository(self, run_id: str, mapping_key: str) -> WorkflowRun:
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            if run.state is not WorkflowState.VALIDATING:
                raise InvalidWorkflowAction("repository confirmation requires VALIDATING")
            try:
                mapping = self.config.resolve_mapping_key(
                    mapping_key, run.project_id, run.iteration_id
                )
            except RepositoryMappingNotFound:
                raise InvalidWorkflowAction(
                    "repository mapping is not configured for this workflow"
                ) from None
            if not any(
                candidate.key == mapping_key and candidate == mapping
                for candidate in run.repository_candidates
            ):
                raise InvalidWorkflowAction(
                    "repository mapping is not an authorized persisted candidate"
                )
            saved = self.store.save(
                run.validated_update(repository=mapping), expected_version=run.version
            )
            return self._flow_for(saved).execute(saved)

    def resume(self, run_id: str) -> WorkflowRun:
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            if run.state is WorkflowState.PARTIAL_SUCCESS:
                return self.publisher.retry_comment(run)
            if run.state is WorkflowState.PUBLISHING:
                return self.publisher.publish(run)
            if run.state is WorkflowState.BLOCKED and run.resume_state is None:
                raise InvalidWorkflowAction("Blocked run has no safe resume state")
            if (
                run.state is WorkflowState.BLOCKED
                and run.resume_state is WorkflowState.PUBLISHING
            ):
                publishing = self.store.transition(
                    run.run_id,
                    run.version,
                    WorkflowState.PUBLISHING,
                    "resume publication from persisted safe checkpoint",
                )
                return self.publisher.publish(publishing)
            return self._flow_for(run).execute(run)

    def revise(
        self,
        run_id: str,
        feedback: str,
        *,
        scope: Literal["implementation", "repair"] | None = None,
    ) -> WorkflowRun:
        scope = _validated_revision_scope(scope)
        feedback = _validated_text(feedback, kind="revision feedback", max_length=4096)
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            expected_scope = (
                "implementation"
                if run.workflow_type is WorkflowType.REQUIREMENT
                else "repair"
                if run.workflow_type is WorkflowType.DEFECT
                else None
            )
            if expected_scope is None or (scope is not None and scope != expected_scope):
                raise InvalidWorkflowAction(_INVALID_REVISION_SCOPE)
            if run.state not in {
                WorkflowState.WAITING_APPROVAL,
                WorkflowState.BLOCKED,
            }:
                raise InvalidWorkflowAction(
                    "revise requires WAITING_APPROVAL or BLOCKED"
                )
            if run.publication != PublicationResult():
                raise InvalidWorkflowAction(
                    "published workflow evidence cannot be revised; create a new workflow run"
                )
            if run.state is WorkflowState.BLOCKED:
                if run.resume_state not in {
                    WorkflowState.IMPLEMENTING,
                    WorkflowState.TESTING,
                    WorkflowState.AI_REVIEW,
                    WorkflowState.WAITING_APPROVAL,
                }:
                    raise InvalidWorkflowAction(
                        "blocked run has no safe implementation revision checkpoint"
                    )
                if run.resume_state is WorkflowState.IMPLEMENTING:
                    blocked = run
                else:
                    resumed = self.store.transition(
                        run.run_id,
                        run.version,
                        run.resume_state,
                        "restore late workflow checkpoint for revision",
                    )
                    blocked = self.store.transition(
                        resumed.run_id,
                        resumed.version,
                        WorkflowState.BLOCKED,
                        "revision requested",
                        resume_state=WorkflowState.IMPLEMENTING,
                    )
            else:
                blocked = self.store.transition(
                    run.run_id,
                    run.version,
                    WorkflowState.BLOCKED,
                    "revision requested",
                    resume_state=WorkflowState.IMPLEMENTING,
                )
            revised = blocked.for_revision(feedback)
            saved = self.store.save(revised, expected_version=blocked.version)
            return self._flow_for(saved).execute(saved)

    def approve(self, run_id: str, approved_by: str) -> WorkflowRun:
        approved_by = _validated_text(
            approved_by, kind="approver identity", max_length=128
        )
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            if run.state is not WorkflowState.WAITING_APPROVAL:
                raise InvalidWorkflowAction("approve requires WAITING_APPROVAL")
            if run.approval is None:
                raise InvalidWorkflowAction("approval package is missing")
            approval = issue_approval(run.approval, approved_by=approved_by)
            saved = self.store.save(
                run.validated_update(approval=approval), expected_version=run.version
            )
            return self.publisher.publish(saved)

    def cancel(self, run_id: str, actor: str) -> WorkflowRun:
        actor = _validated_text(actor, kind="cancellation actor", max_length=128)
        with self.store.operation_lock(run_id, "orchestrate"):
            run = self.store.load(run_id)
            return self.store.transition(
                run_id,
                run.version,
                WorkflowState.CANCELLED,
                f"cancelled by {actor}",
            )

    def _flow_for(self, run: WorkflowRun) -> RequirementFlow | DefectFlow:
        if run.workflow_type is WorkflowType.REQUIREMENT:
            return self.requirement_flow
        if run.workflow_type is WorkflowType.DEFECT:
            return self.defect_flow
        raise InvalidWorkflowAction("Unknown workflow type")


__all__ = ["DeveloperWorkflowOrchestrator", "InvalidWorkflowAction"]
