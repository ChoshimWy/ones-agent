"""Idempotent, marker-based ONES comment delivery after PR publication."""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from .contracts import WorkflowRun


class CommentError(RuntimeError):
    """A comment could not be delivered safely."""


class CommentDeliveryUncertain(CommentError):
    """The write outcome cannot be proved and must not be blindly repeated."""


class CommentGateway(Protocol):
    def list_comments_sync(self, item_id: str) -> list[dict[str, str]]: ...

    def add_comment_sync(self, item_id: str, text: str) -> dict[str, str]: ...


class CommentLeaseStore(Protocol):
    def operation_lock(self, run_id: str, purpose: str) -> AbstractContextManager[None]: ...
    def load(self, run_id: str) -> WorkflowRun: ...


_SECRET_PATTERN = re.compile(
    r"(?i)(?:token|password|secret|authorization)\s*[:=]|gh[pousr]_[A-Za-z0-9]{20,}"
)


def _safe_text(value: str, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError(f"{label} must be valid UTF-8") from None
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValueError(f"{label} contains unsafe control characters")
    if _SECRET_PATTERN.search(value):
        raise ValueError(f"{label} appears to contain credential material")
    return value


def comment_marker(run_id: str) -> str:
    if re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise ValueError("run_id is invalid")
    return f"<!-- ones-dev-run:{run_id} -->"


def build_comment_text(
    run: WorkflowRun,
    *,
    summary: str,
    tests_summary: str,
    max_length: int = 16_000,
) -> str:
    """Build a bounded, UTF-8-safe comment carrying a stable run marker."""

    if run.group_publication is not None:
        published = tuple(
            (item.repository_key, item.commit_hash, item.pr_url)
            for item in run.group_publication.repositories
        )
        if not published or any(not commit or not url for _, commit, url in published):
            raise ValueError("Every repository PR is required before commenting")
        pr_url = "\n".join(
            f"- {_safe_text(key, label='repository key')}: "
            f"{_safe_text(commit, label='commit')} {_safe_text(url, label='PR URL')}"
            for key, commit, url in published
        )
    else:
        pr_url = _safe_text(run.publication.pr_url.strip(), label="PR URL")
        if not pr_url:
            raise ValueError("PR URL is required before commenting")
    summary = _safe_text(summary.strip(), label="summary")
    tests_summary = _safe_text(tests_summary.strip(), label="tests summary")
    marker = comment_marker(run.run_id)
    handoff = ""
    if run.approval is not None and run.approval.draft_pr:
        handoff = (f"\n交付状态：Draft PR，等待 PR 人工验证（{len(run.approval.deferred_verification)} 项）。"
                   "未完成的实机/环境验证不视为通过；请在对应提交上验证，合并和发布仍受门禁约束。\n")
    body = (
        f"AI 开发工作流已创建 PR：{pr_url}\n\n"
        f"{handoff}"
        f"实现摘要：{summary or '见 PR 描述'}\n"
        f"测试结果：{tests_summary or '见审批包'}\n\n{marker}"
    )
    if max_length < len(marker) or len(body.encode("utf-8")) > max_length:
        raise ValueError("ONES comment exceeds configured length limit")
    return body


def _comment_identity(comment: dict[str, str], marker: str) -> str | None:
    if type(comment) is not dict:
        return None
    comment_id = comment.get("id")
    text = comment.get("text")
    if (
        type(comment_id) is str
        and comment_id.strip()
        and type(text) is str
        and marker in text
    ):
        return comment_id.strip()
    return None


@dataclass(slots=True)
class OnesCommenter:
    gateway: CommentGateway
    store: CommentLeaseStore
    max_length: int = 16_000

    def ensure_comment(self, run: WorkflowRun) -> str:
        """Return an existing/created comment id without duplicating uncertain writes."""

        with self.store.operation_lock(run.run_id, "publish"):
            return self.ensure_comment_locked(self.store.load(run.run_id))

    def ensure_comment_locked(self, run: WorkflowRun) -> str:
        """Comment while the caller holds this run's publish operation lease."""

        if run.group_publication is not None:
            if not run.group_publication.repositories or any(
                not item.pr_url for item in run.group_publication.repositories
            ):
                raise ValueError("Every repository PR is required before ONES comment")
        elif not run.publication.pr_url.strip():
            raise ValueError("PR URL is required before ONES comment")
        marker = comment_marker(run.run_id)
        existing = self._find(run.work_item_id, marker)
        if existing:
            return existing
        summary = run.review.summary if run.review is not None else ""
        tests_summary = "; ".join(result.summary for result in run.test_results)
        text = build_comment_text(
            run,
            summary=summary,
            tests_summary=tests_summary,
            max_length=self.max_length,
        )
        try:
            created = self.gateway.add_comment_sync(run.work_item_id, text)
        except Exception:
            recovered = self._find(run.work_item_id, marker)
            if recovered:
                return recovered
            raise CommentDeliveryUncertain(
                "ONES comment outcome is uncertain; manual verification is required"
            ) from None
        created_id = _comment_identity(created, marker)
        if not created_id:
            recovered = self._find(run.work_item_id, marker)
            if recovered:
                return recovered
            raise CommentDeliveryUncertain(
                "ONES comment response did not prove the created comment identity"
            )
        return created_id

    def _find(self, item_id: str, marker: str) -> str | None:
        comments = self.gateway.list_comments_sync(item_id)
        for comment in comments:
            identity = _comment_identity(comment, marker)
            if identity:
                return identity
        return None


__all__ = [
    "CommentDeliveryUncertain",
    "CommentError",
    "OnesCommenter",
    "build_comment_text",
    "comment_marker",
]
