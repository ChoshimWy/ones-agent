"""Approval-gated, checkpointed publication orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Protocol
from urllib.parse import urlsplit

from .approval import (
    ApprovalError,
    validate_for_approval,
    verify_approval,
)
from .contracts import (
    ApprovalPackage,
    MultiRepositoryPublicationResult,
    PreparedWorktree,
    PublicationResult,
    RepositoryMapping,
    RepositoryPublicationResult,
    WorkflowRun,
    WorkflowState,
)
from .ones_comment import comment_marker
from .pr_provider import PullRequestProviderError, parse_repository_identity
from .state_store import FileRunStore


class PublicationError(RuntimeError):
    """Base safe publication failure."""


class PublicationBlocked(PublicationError):
    """Publication stopped at a recoverable, persisted checkpoint."""


class ApprovalRebuilder(Protocol):
    def rebuild(self, run: WorkflowRun) -> ApprovalPackage: ...


class PublicationRepository(Protocol):
    def prepare_commit_intent(self, run: WorkflowRun, approval: ApprovalPackage) -> str: ...
    def find_approved_commit(self, run: WorkflowRun) -> str | None: ...
    def commit_approved(self, run: WorkflowRun) -> str: ...
    def remote_branch_oid(self, run: WorkflowRun) -> str | None: ...
    def assert_remote_base_unchanged(
        self, prepared: PreparedWorktree, mapping: RepositoryMapping
    ) -> None: ...
    def push_approved(self, run: WorkflowRun) -> None: ...


class PullRequestClient(Protocol):
    def find(self, *, repo_url: str, head: str, base: str, marker: str) -> str | None: ...
    def create(
        self, *, repo_url: str, head: str, base: str, title: str, body: str, marker: str
    ) -> str: ...


class Commenter(Protocol):
    def ensure_comment(self, run: WorkflowRun) -> str: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_pr_url(value: str, expected_host: str) -> str:
    if type(value) is not str or not value.strip():
        raise PublicationBlocked("PR provider returned no usable URL")
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        raise PublicationBlocked("PR provider returned an unsafe URL") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != expected_host.casefold()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise PublicationBlocked("PR provider returned an unsafe URL")
    return value.strip()


def _validate_provider(provider: str, provider_host: str, repo_url: str) -> None:
    if provider not in {"github", "gitlab"}:
        raise PublicationBlocked("Unknown PR provider")
    try:
        parse_repository_identity(repo_url, provider_host)
    except PullRequestProviderError:
        raise PublicationBlocked("Repository host does not match the configured PR provider")


@dataclass(slots=True)
class Publisher:
    store: FileRunStore
    repository: PublicationRepository
    approval_rebuilder: ApprovalRebuilder | Callable[[WorkflowRun], ApprovalPackage]
    pr_client: PullRequestClient
    commenter: Commenter
    provider: str
    provider_host: str

    def publish(self, run: WorkflowRun) -> WorkflowRun:
        """Publish or resume one run without repeating persisted/factual effects."""

        with self.store.operation_lock(run.run_id, "publish"):
            return self._publish_locked(self.store.load(run.run_id))

    def _publish_locked(self, run: WorkflowRun) -> WorkflowRun:

        if run.repository_group is not None:
            return self._publish_group_locked(run)

        if run.state is WorkflowState.COMPLETED:
            return run
        if run.state is WorkflowState.PARTIAL_SUCCESS:
            return self._retry_comment_locked(run)
        if run.state is WorkflowState.WAITING_APPROVAL:
            run = self._enter_publishing(run)
        elif run.state is not WorkflowState.PUBLISHING:
            raise PublicationBlocked("Publication requires WAITING_APPROVAL or PUBLISHING")
        elif not run.publication.approved_fingerprint:
            run = self._recover_missing_intent(run)

        self._assert_persisted_provider(run)
        run = self._ensure_commit(run)
        run = self._ensure_push(run)
        run = self._ensure_pr(run)
        return self._ensure_comment(run)

    def _publish_group_locked(self, run: WorkflowRun) -> WorkflowRun:
        if run.state is WorkflowState.COMPLETED:
            return run
        recovering = run.group_publication is not None
        if run.state is WorkflowState.WAITING_APPROVAL:
            approval, current = self._rebuild_approved_package(run)
            run = self.store.transition(
                run.run_id, run.version, WorkflowState.PUBLISHING,
                "approved repository group publication",
            )
            run = self._save_group_intent(run, approval, current)
        elif run.state not in {WorkflowState.PUBLISHING, WorkflowState.PARTIAL_SUCCESS}:
            raise PublicationBlocked(
                "Group publication requires WAITING_APPROVAL, PUBLISHING, or PARTIAL_SUCCESS"
            )
        elif run.state is WorkflowState.PARTIAL_SUCCESS:
            run = self.store.transition(
                run.run_id, run.version, WorkflowState.PUBLISHING,
                "resume repository group publication",
            )
        if run.state is WorkflowState.PUBLISHING and run.group_publication is None:
            approval, current = self._rebuild_approved_package(run)
            run = self._save_group_intent(run, approval, current)
        if run.group_publication is None or not run.group_publication.repositories:
            raise PublicationBlocked("Repository group publication intent is missing")
        active_key: str | None = None
        try:
            if recovering:
                self._rebuild_approved_package(run)
                self._reconcile_group_facts(run)
            self._assert_group_persisted_provider(run)
            for item in run.group_publication.repositories:
                active_key = item.repository_key
                if not item.commit_hash:
                    projection = self._group_projection(run, item)
                    commit = self.repository.find_approved_commit(projection)
                    if not commit:
                        commit = self.repository.commit_approved(projection)
                    run = self._save_group_repository_fact(
                        run, item.repository_key, commit_hash=commit, error=""
                    )
            for item in run.group_publication.repositories:
                active_key = item.repository_key
                run = self._ensure_group_remote(run, item.repository_key)
        except PublicationBlocked as error:
            return self._group_partial(
                self.store.load(run.run_id), str(error), repository_key=active_key
            )
        except Exception:
            return self._group_partial(
                self.store.load(run.run_id),
                "Repository group publication failed safely",
                repository_key=active_key,
            )
        return self._ensure_group_comment(run)

    def _assert_persisted_provider(self, run: WorkflowRun) -> None:
        if (
            self.provider != run.publication.provider
            or self.provider_host.casefold() != run.publication.provider_host
        ):
            raise PublicationBlocked("Configured PR provider differs from publication intent")

    def _assert_group_persisted_provider(self, run: WorkflowRun) -> None:
        if run.group_publication is None or any(
            item.provider != self.provider
            or item.provider_host.casefold() != self.provider_host.casefold()
            for item in run.group_publication.repositories
        ):
            raise PublicationBlocked(
                "Configured PR provider differs from repository group intent"
            )

    def _assert_group_remote_bases(self, run: WorkflowRun) -> None:
        if not run.repository_evidence:
            raise PublicationBlocked("Repository group evidence is missing")
        try:
            for evidence in run.repository_evidence:
                self.repository.assert_remote_base_unchanged(
                    evidence.prepared_worktree, evidence.mapping
                )
        except Exception:
            raise PublicationBlocked(
                "A repository base branch changed before publication"
            ) from None

    def _reconcile_group_facts(self, run: WorkflowRun) -> None:
        assert run.group_publication is not None
        for item in run.group_publication.repositories:
            projection = self._group_projection(run, item)
            if item.commit_hash:
                try:
                    local = self.repository.find_approved_commit(projection)
                except Exception:
                    raise PublicationBlocked(
                        "A persisted repository commit could not be verified"
                    ) from None
                if local != item.commit_hash:
                    raise PublicationBlocked(
                        "A persisted repository commit no longer matches"
                    )
            if item.push_completed_at is not None:
                try:
                    remote = self.repository.remote_branch_oid(projection)
                except Exception:
                    raise PublicationBlocked(
                        "A persisted remote branch could not be verified"
                    ) from None
                if remote != item.commit_hash:
                    raise PublicationBlocked(
                        "A persisted remote branch no longer matches"
                    )
            if item.pr_url:
                try:
                    current_url = self.pr_client.find(
                        repo_url=item.repo_url, head=item.pr_head,
                        base=item.pr_base, marker=item.pr_marker,
                    )
                except Exception:
                    raise PublicationBlocked(
                        "A persisted pull request could not be verified"
                    ) from None
                if (
                    current_url is None
                    or _safe_pr_url(current_url, item.provider_host) != item.pr_url
                ):
                    raise PublicationBlocked(
                        "A persisted pull request no longer matches"
                    )

    def retry_comment(self, run: WorkflowRun) -> WorkflowRun:
        with self.store.operation_lock(run.run_id, "publish"):
            current = self.store.load(run.run_id)
            if current.state is WorkflowState.COMPLETED:
                return current
            return self._retry_comment_locked(current)

    def _retry_comment_locked(self, run: WorkflowRun) -> WorkflowRun:
        if run.state is not WorkflowState.PARTIAL_SUCCESS:
            raise PublicationBlocked("Comment retry requires PARTIAL_SUCCESS")
        run = self.store.transition(
            run.run_id, run.version, WorkflowState.PUBLISHING, "retry ONES comment"
        )
        return self._ensure_comment(run)

    def _enter_publishing(self, run: WorkflowRun) -> WorkflowRun:
        approval, current = self._rebuild_approved_package(run)
        run = self.store.transition(
            run.run_id, run.version, WorkflowState.PUBLISHING, "approved publication"
        )
        return self._save_intent(run, approval, current)

    def _recover_missing_intent(self, run: WorkflowRun) -> WorkflowRun:
        """Repair the only safe pre-effect crash window after state transition."""

        if run.publication != PublicationResult():
            raise PublicationBlocked("Publication intent is incomplete and cannot be recovered")
        approval, current = self._rebuild_approved_package(run)
        return self._save_intent(run, approval, current)

    def _rebuild_approved_package(
        self, run: WorkflowRun
    ) -> tuple[ApprovalPackage, ApprovalPackage]:
        approval = run.approval
        if (
            approval is None
            or not approval.fingerprint
            or not approval.approved_by
            or approval.approved_at is None
        ):
            raise PublicationBlocked("A complete signed approval is required")
        try:
            current = (
                self.approval_rebuilder.rebuild(run)
                if hasattr(self.approval_rebuilder, "rebuild")
                else self.approval_rebuilder(run)
            )
            current = validate_for_approval(current)
            verify_approval(approval.fingerprint, current)
        except ApprovalError:
            raise PublicationBlocked("Approval evidence is no longer current") from None
        except Exception:
            raise PublicationBlocked("Current approval evidence could not be rebuilt") from None
        if current.repository_group is None:
            _validate_provider(self.provider, self.provider_host, current.repo_url)
        else:
            for item in current.repositories:
                if item.changed_files:
                    _validate_provider(
                        self.provider, self.provider_host, item.mapping.repo_url
                    )
        return approval, current

    def _save_group_intent(
        self, run: WorkflowRun, approval: ApprovalPackage, current: ApprovalPackage
    ) -> WorkflowRun:
        current_by_key = {item.repository_key: item for item in current.repositories}
        evidence_by_key = {item.repository_key: item for item in run.repository_evidence}
        intents: list[RepositoryPublicationResult] = []
        for key in current.repository_group.topological_keys():  # type: ignore[union-attr]
            item = current_by_key[key]
            if not item.changed_files:
                continue
            provisional = RepositoryPublicationResult(
                repository_key=key,
                approved_fingerprint=approval.fingerprint,
                repo_url=item.mapping.repo_url,
                provider=self.provider,
                provider_host=self.provider_host.casefold(),
                expected_parent=item.head_commit,
                expected_tree=item.tree_hash,
                commit_message=item.commit_message,
                remote_branch=item.branch,
                pr_marker=f"ones-dev-run:{run.run_id}:{key}",
                pr_base=item.mapping.base_branch,
                pr_head=item.branch,
                pr_title=item.pr_title,
                pr_body=item.pr_body,
                comment_marker=comment_marker(run.run_id),
            )
            projection = self._group_projection(
                run, provisional, approval_item=item,
                evidence_item=evidence_by_key[key],
            )
            try:
                tree = self.repository.prepare_commit_intent(
                    projection, projection.approval
                )
            except Exception:
                raise PublicationBlocked(
                    "Approved repository group snapshot could not be prepared"
                ) from None
            if tree != item.tree_hash:
                raise PublicationBlocked(
                    "Approved repository tree differs from signed evidence"
                )
            intents.append(provisional.validated_update(expected_tree=tree))
        if not intents:
            raise PublicationBlocked("Approved repository group has no changes to publish")
        group_publication = MultiRepositoryPublicationResult(
            order=current.repository_group.topological_keys(),  # type: ignore[union-attr]
            repositories=tuple(intents),
            comment_marker=comment_marker(run.run_id),
        )
        return self.store.save(
            run.validated_update(group_publication=group_publication), run.version
        )

    def _group_projection(
        self,
        run: WorkflowRun,
        publication: RepositoryPublicationResult,
        *,
        approval_item: object | None = None,
        evidence_item: object | None = None,
    ) -> WorkflowRun:
        approval = run.approval
        if approval is None:
            raise PublicationBlocked("Signed aggregate approval is missing")
        item = approval_item or next(
            value for value in approval.repositories
            if value.repository_key == publication.repository_key
        )
        evidence = evidence_item or next(
            value for value in run.repository_evidence
            if value.repository_key == publication.repository_key
        )
        singular = approval.model_copy(update={
            "repository_group": None, "repositories": (), "integration_tests": (),
            "repository": item.mapping, "repo_url": item.mapping.repo_url,
            "base_branch": item.mapping.base_branch, "base_commit": item.base_commit,
            "head_commit": item.head_commit, "diff_hash": item.diff_hash,
            "diff_summary": item.diff_summary, "branch": item.branch,
            "changed_files": item.changed_files, "tests": item.tests,
            "commit_message": item.commit_message, "pr_title": item.pr_title,
            "pr_body": item.pr_body,
        })
        return run.model_copy(update={
            "repository": evidence.mapping,
            "prepared_worktree": evidence.prepared_worktree,
            "tested_snapshot": evidence.tested_snapshot,
            "approval": singular,
            "publication": PublicationResult.model_validate(
                publication.model_dump(exclude={"repository_key"})
            ),
        })

    def _save_group_repository_fact(
        self, run: WorkflowRun, key: str, **changes: object
    ) -> WorkflowRun:
        assert run.group_publication is not None
        repositories = tuple(
            item.validated_update(**changes) if item.repository_key == key else item
            for item in run.group_publication.repositories
        )
        updated = run.group_publication.validated_update(repositories=repositories, error="")
        return self.store.save(run.validated_update(group_publication=updated), run.version)

    def _ensure_group_remote(self, run: WorkflowRun, key: str) -> WorkflowRun:
        assert run.group_publication is not None
        item = next(value for value in run.group_publication.repositories if value.repository_key == key)
        projection = self._group_projection(run, item)
        if item.push_completed_at is None:
            # Re-check every repository base immediately before each individual
            # push. Earlier repositories may take an arbitrary amount of time to
            # publish and must not leave a later repository using a stale base.
            self._assert_group_remote_bases(run)
            remote = self.repository.remote_branch_oid(projection)
            if remote and remote != item.commit_hash:
                raise PublicationBlocked("Remote branch points to a different commit")
            if remote is None:
                try:
                    self.repository.push_approved(projection)
                except Exception:
                    remote = self.repository.remote_branch_oid(projection)
                    if remote != item.commit_hash:
                        raise PublicationBlocked(
                            "Push outcome is uncertain; manual verification is required"
                        ) from None
                else:
                    remote = self.repository.remote_branch_oid(projection)
                    if remote != item.commit_hash:
                        raise PublicationBlocked("Remote branch did not confirm approved commit")
            run = self._save_group_repository_fact(
                run, key, push_completed_at=_utc_now(), error=""
            )
            item = next(value for value in run.group_publication.repositories if value.repository_key == key)
        if item.pr_url:
            return run
        kwargs = {
            "repo_url": item.repo_url, "head": item.pr_head,
            "base": item.pr_base, "marker": item.pr_marker,
        }
        try:
            pr_url = self.pr_client.find(**kwargs)
            if not pr_url:
                try:
                    pr_url = self.pr_client.create(
                        **kwargs, title=item.pr_title, body=item.pr_body
                    )
                except Exception:
                    pr_url = self.pr_client.find(**kwargs)
                    if not pr_url:
                        raise PublicationBlocked(
                            "PR creation outcome is uncertain"
                        ) from None
        except PublicationBlocked:
            raise
        except Exception:
            raise PublicationBlocked("PR provider could not be safely queried") from None
        return self._save_group_repository_fact(
            run, key, pr_url=_safe_pr_url(pr_url, item.provider_host), error=""
        )

    def _group_partial(
        self, run: WorkflowRun, error: str, *, repository_key: str | None = None
    ) -> WorkflowRun:
        assert run.group_publication is not None
        repositories = run.group_publication.repositories
        if repository_key is not None:
            repositories = tuple(
                item.validated_update(error=error)
                if item.repository_key == repository_key else item
                for item in repositories
            )
        updated = run.group_publication.validated_update(
            repositories=repositories,
            error=error,
        )
        run = self.store.save(run.validated_update(group_publication=updated), run.version)
        return self.store.transition(
            run.run_id, run.version, WorkflowState.PARTIAL_SUCCESS,
            "repository group publication requires retry",
        )

    def _ensure_group_comment(self, run: WorkflowRun) -> WorkflowRun:
        assert run.group_publication is not None
        if run.group_publication.comment_id:
            return self.store.transition(
                run.run_id, run.version, WorkflowState.COMPLETED,
                "repository group publication completed",
            )
        try:
            locked = getattr(self.commenter, "ensure_comment_locked", None)
            comment_id = locked(run) if callable(locked) else self.commenter.ensure_comment(run)
            if type(comment_id) is not str or not comment_id.strip():
                raise ValueError("missing comment identity")
        except Exception:
            return self._group_partial(run, "ONES comment delivery failed")
        updated = run.group_publication.validated_update(
            comment_id=comment_id.strip(), error=""
        )
        run = self.store.save(run.validated_update(group_publication=updated), run.version)
        return self.store.transition(
            run.run_id, run.version, WorkflowState.COMPLETED,
            "repository group publication completed",
        )

    def _save_intent(
        self,
        run: WorkflowRun,
        approval: ApprovalPackage,
        current: ApprovalPackage,
    ) -> WorkflowRun:
        try:
            expected_tree = self.repository.prepare_commit_intent(run, current)
        except Exception:
            raise PublicationBlocked("Approved repository snapshot could not be prepared") from None
        publication = PublicationResult(
            approved_fingerprint=approval.fingerprint,
            repo_url=current.repo_url,
            provider=self.provider,
            provider_host=self.provider_host.casefold(),
            expected_parent=current.head_commit,
            expected_tree=expected_tree,
            commit_message=current.commit_message,
            remote_branch=current.branch,
            pr_marker=f"ones-dev-run:{run.run_id}",
            pr_base=current.base_branch,
            pr_head=current.branch,
            pr_title=current.pr_title,
            pr_body=current.pr_body,
            comment_marker=comment_marker(run.run_id),
        )
        return self.store.save(run.validated_update(publication=publication), run.version)

    def _ensure_commit(self, run: WorkflowRun) -> WorkflowRun:
        if run.publication.commit_hash:
            return run
        try:
            commit_hash = self.repository.find_approved_commit(run)
            if not commit_hash:
                commit_hash = self.repository.commit_approved(run)
        except Exception:
            raise PublicationBlocked("Approved commit could not be safely completed") from None
        updated = run.publication.validated_update(commit_hash=commit_hash)
        return self.store.save(run.validated_update(publication=updated), run.version)

    def _ensure_push(self, run: WorkflowRun) -> WorkflowRun:
        if run.publication.push_completed_at is not None:
            return run
        try:
            remote = self.repository.remote_branch_oid(run)
            if remote and remote != run.publication.commit_hash:
                raise PublicationBlocked("Remote branch points to a different commit")
            if remote is None:
                try:
                    self.repository.push_approved(run)
                except Exception:
                    remote = self.repository.remote_branch_oid(run)
                    if remote != run.publication.commit_hash:
                        raise PublicationBlocked(
                            "Push outcome is uncertain; manual verification is required"
                        ) from None
                else:
                    remote = self.repository.remote_branch_oid(run)
                    if remote != run.publication.commit_hash:
                        raise PublicationBlocked("Remote branch did not confirm approved commit")
        except PublicationBlocked:
            raise
        except Exception:
            raise PublicationBlocked("Remote branch could not be safely inspected") from None
        updated = run.publication.validated_update(push_completed_at=_utc_now())
        return self.store.save(run.validated_update(publication=updated), run.version)

    def _ensure_pr(self, run: WorkflowRun) -> WorkflowRun:
        if run.publication.pr_url:
            return run
        kwargs = {
            "repo_url": run.publication.repo_url,
            "head": run.publication.pr_head,
            "base": run.publication.pr_base,
            "marker": run.publication.pr_marker,
        }
        try:
            pr_url = self.pr_client.find(**kwargs)
            if not pr_url:
                try:
                    pr_url = self.pr_client.create(
                        **kwargs,
                        title=run.publication.pr_title,
                        body=run.publication.pr_body,
                    )
                except Exception:
                    pr_url = self.pr_client.find(**kwargs)
                    if not pr_url:
                        raise PublicationBlocked(
                            "PR creation outcome is uncertain; no ONES comment was attempted"
                        ) from None
        except PublicationBlocked:
            raise
        except Exception:
            raise PublicationBlocked("PR provider could not be safely queried") from None
        pr_url = _safe_pr_url(pr_url, run.publication.provider_host)
        updated = run.publication.validated_update(pr_url=pr_url)
        return self.store.save(run.validated_update(publication=updated), run.version)

    def _ensure_comment(self, run: WorkflowRun) -> WorkflowRun:
        if run.publication.comment_id:
            return self.store.transition(
                run.run_id, run.version, WorkflowState.COMPLETED, "publication completed"
            )
        try:
            locked = getattr(self.commenter, "ensure_comment_locked", None)
            comment_id = locked(run) if callable(locked) else self.commenter.ensure_comment(run)
            if type(comment_id) is not str or not comment_id.strip():
                raise ValueError("missing comment identity")
        except Exception:
            failed = run.publication.validated_update(
                error="ONES comment delivery failed"
            )
            run = self.store.save(run.validated_update(publication=failed), run.version)
            return self.store.transition(
                run.run_id,
                run.version,
                WorkflowState.PARTIAL_SUCCESS,
                "PR published but ONES comment requires retry",
            )
        updated = run.publication.validated_update(comment_id=comment_id.strip(), error="")
        run = self.store.save(run.validated_update(publication=updated), run.version)
        return self.store.transition(
            run.run_id, run.version, WorkflowState.COMPLETED, "publication completed"
        )


__all__ = ["PublicationBlocked", "PublicationError", "Publisher"]
