"""Production reconstruction of approval evidence at the publication boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Protocol

from src.contracts import DefectRecord, RequirementRecord, WikiPageSnapshot

from .approval import validate_for_approval
from .command_utils import display_argv, parse_command_argv
from .contracts import ApprovalPackage, RepositorySnapshot, WorkflowRun, WorkflowType
from .test_evidence import select_defect_final_tests, select_requirement_final_tests


class ApprovalRebuildError(RuntimeError):
    """Live approval evidence cannot be reconstructed safely."""


class ReadOnlyApprovalGateway(Protocol):
    def get_normalized_requirement_sync(self, issue_id: str) -> RequirementRecord: ...
    def get_normalized_defect_sync(self, issue_id: str, **kwargs: object) -> DefectRecord: ...
    def get_wiki_snapshot_by_ids_sync(
        self, space_id: str, page_id: str, *, source_url: str | None = None
    ) -> WikiPageSnapshot: ...


class ApprovalSnapshotRepository(Protocol):
    def assert_remote_base_unchanged(self, prepared: object, mapping: object) -> None: ...
    def assert_head_unchanged(self, prepared: object) -> None: ...
    def snapshot(self, prepared: object, mapping: object) -> RepositorySnapshot: ...


def _digest(value: object) -> str:
    try:
        payload = json.dumps(
            asdict(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError):
        raise ApprovalRebuildError("ONES source snapshot is invalid") from None
    return hashlib.sha256(payload).hexdigest()


@dataclass(slots=True)
class WorkflowApprovalRebuilder:
    """Re-read all machine evidence; retain only explicitly human-authored fields."""

    gateway: ReadOnlyApprovalGateway
    repository: ApprovalSnapshotRepository

    def rebuild(self, run: WorkflowRun) -> ApprovalPackage:
        current = run.approval
        mapping, prepared, tested = (
            run.repository, run.prepared_worktree, run.tested_snapshot
        )
        if current is None or mapping is None or prepared is None or tested is None:
            raise ApprovalRebuildError("persisted approval evidence is incomplete")
        if (
            current.repository != mapping
            or current.repo_url != mapping.repo_url
            or current.base_branch != mapping.base_branch
            or current.base_commit != prepared.base_commit
            or current.branch != prepared.branch
        ):
            raise ApprovalRebuildError("repository identity no longer matches the run")

        self.repository.assert_remote_base_unchanged(prepared, mapping)
        self.repository.assert_head_unchanged(prepared)
        snapshot = self.repository.snapshot(prepared, mapping)
        self.repository.assert_head_unchanged(prepared)
        if snapshot.head_commit != prepared.head_commit:
            raise ApprovalRebuildError("repository HEAD changed")
        if snapshot != tested:
            raise ApprovalRebuildError("repository snapshot differs from tested evidence")

        review = run.review
        review_findings = () if review is None else (
            review.review_findings or ((review.summary,) if review.summary else ())
        )
        risks = tuple(dict.fromkeys(
            item for result in (*run.codex_results, *((review,) if review else ()))
            for item in result.risks
        ))
        evidence = tuple(dict.fromkeys(
            item for result in (*run.codex_results, *((review,) if review else ()))
            for item in result.evidence
        ))
        wiki = tuple(self._wiki_snapshot(item) for item in run.wiki_snapshots)

        if run.type is WorkflowType.REQUIREMENT:
            source = self.gateway.get_normalized_requirement_sync(run.work_item_id)
            if source.requirement_id != run.work_item_id:
                raise ApprovalRebuildError("ONES requirement identity changed")
            rebuilt = current.model_copy(update={
                "work_item_id": source.requirement_id,
                "work_item_title": source.title,
                "work_item_status": source.status.name or source.status.id,
                "source_versions": {"requirement_sha256": _digest(source)},
                "wiki_hashes": {item.page_id: item.content_sha256 for item in wiki},
                "wiki_snapshots": wiki,
                "repository": mapping,
                "repo_url": mapping.repo_url,
                "base_branch": mapping.base_branch,
                "base_commit": prepared.base_commit,
                "head_commit": snapshot.head_commit,
                "diff_hash": snapshot.diff_sha256,
                "diff_summary": self._diff_summary(snapshot),
                "branch": prepared.branch,
                "changed_files": snapshot.changed_files,
                "coverage": {
                    f"{item.criterion_id}: {item.criterion_text}":
                    f"files={','.join(item.files)}; tests={','.join(item.tests)}"
                    for item in run.acceptance_coverage
                },
                "evidence": evidence or ("verified repository diff and configured tests",),
                "tests": select_requirement_final_tests(run.test_results, mapping),
                "review": review_findings,
                "risks": risks,
            })
        else:
            source = self.gateway.get_normalized_defect_sync(
                run.work_item_id,
                project_id=run.project_id or None,
                sprint_id=run.iteration_id or None,
                assignee=run.assignee_id or None,
            )
            if source.defect_id != run.work_item_id:
                raise ApprovalRebuildError("ONES defect identity changed")
            reproduction_command = self._defect_reproduction_command(run)
            rebuilt = current.model_copy(update={
                "work_item_id": source.defect_id,
                "work_item_title": source.title,
                "work_item_status": source.status.name or source.status.id,
                "source_versions": {"defect_sha256": _digest(source)},
                "wiki_hashes": {item.page_id: item.content_sha256 for item in wiki},
                "wiki_snapshots": wiki,
                "repository": mapping,
                "repo_url": mapping.repo_url,
                "base_branch": mapping.base_branch,
                "base_commit": prepared.base_commit,
                "head_commit": snapshot.head_commit,
                "diff_hash": snapshot.diff_sha256,
                "diff_summary": self._diff_summary(snapshot),
                "branch": prepared.branch,
                "changed_files": snapshot.changed_files,
                "evidence": tuple(
                    f"{item.file_path}:{item.location} - {item.mechanism}"
                    for item in run.root_cause_evidence
                ),
                "tests": select_defect_final_tests(
                    run.test_results,
                    mapping,
                    reproduction_command=reproduction_command,
                    reproduction_argv=self._defect_reproduction_argv(run),
                ),
                "review": review_findings,
                "risks": (*risks, f"risk_level={run.risk_level}"),
                "root_cause_evidence": run.root_cause_evidence,
                "behavior_before": run.behavior_before,
                "behavior_after": run.behavior_after,
                "impact_scope": run.impact_scope,
                "risk_level": run.risk_level,
                "pre_fix_tests": run.pre_fix_test_results,
                "reproduction_command": reproduction_command,
                "reproduction_test_sha256": run.reproduction_test_sha256,
            })
        return validate_for_approval(rebuilt)

    def _wiki_snapshot(self, expected: WikiPageSnapshot) -> WikiPageSnapshot:
        actual = self.gateway.get_wiki_snapshot_by_ids_sync(
            expected.space_id, expected.page_id, source_url=expected.source_url
        )
        if (
            actual.team_id != expected.team_id
            or actual.space_id != expected.space_id
            or actual.page_id != expected.page_id
            or actual.source_url != expected.source_url
        ):
            raise ApprovalRebuildError("ONES wiki identity changed")
        return actual

    @staticmethod
    def _diff_summary(snapshot: RepositorySnapshot) -> str:
        return (
            f"changed {len(snapshot.changed_files)} file(s): "
            f"{', '.join(snapshot.changed_files)}"
        )

    @staticmethod
    def _defect_reproduction_command(run: WorkflowRun) -> str:
        invocations = {
            (item.reproduction_command, item.test_selector)
            for item in run.root_cause_evidence
        }
        if len(invocations) != 1:
            raise ApprovalRebuildError("defect reproduction evidence is inconsistent")
        command, selector = next(iter(invocations))
        try:
            return display_argv((*parse_command_argv(command), selector))
        except (ValueError, OSError):
            raise ApprovalRebuildError("defect reproduction evidence is invalid") from None

    @staticmethod
    def _defect_reproduction_argv(run: WorkflowRun) -> tuple[str, ...]:
        invocations = {
            (item.reproduction_command, item.test_selector)
            for item in run.root_cause_evidence
        }
        if len(invocations) != 1:
            raise ApprovalRebuildError("defect reproduction evidence is inconsistent")
        command, selector = next(iter(invocations))
        try:
            return (*parse_command_argv(command), selector)
        except ValueError:
            raise ApprovalRebuildError("defect reproduction evidence is invalid") from None


__all__ = ["ApprovalRebuildError", "WorkflowApprovalRebuilder"]
