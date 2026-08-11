from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
import threading
import time

from src.developer_workflow.contracts import PublicationResult, WorkflowRun
from src.developer_workflow.ones_comment import (
    CommentDeliveryUncertain,
    OnesCommenter,
    build_comment_text,
)


@dataclass
class FakeGateway:
    comments: list[dict[str, str]] = field(default_factory=list)
    add_calls: list[tuple[str, str]] = field(default_factory=list)
    fail_after_add: bool = False

    def list_comments_sync(self, item_id: str) -> list[dict[str, str]]:
        return list(self.comments)

    def add_comment_sync(self, item_id: str, text: str) -> dict[str, str]:
        self.add_calls.append((item_id, text))
        created = {"id": "comment-1", "text": text}
        self.comments.append(created)
        if self.fail_after_add:
            raise TimeoutError("secret-token must not be exposed")
        return created


@dataclass
class FakeLeaseStore:
    run: WorkflowRun

    @contextmanager
    def operation_lock(self, run_id, purpose):
        yield

    def load(self, run_id):
        return self.run


def commenter(gateway: FakeGateway, run: WorkflowRun) -> OnesCommenter:
    return OnesCommenter(gateway, FakeLeaseStore(run))


def _run() -> WorkflowRun:
    return WorkflowRun.new("requirement", "REQ-1").validated_update(
        publication=PublicationResult(
            approved_fingerprint="a" * 64,
            repo_url="https://github.example/team/repo.git",
            provider="github",
            provider_host="github.example",
            expected_parent="b" * 40,
            expected_tree="c" * 40,
            commit_message="feat: test",
            commit_hash="d" * 40,
            remote_branch="requirement/REQ-1-test",
            push_completed_at=datetime(2026, 8, 10, tzinfo=UTC),
            pr_marker="ones-dev-run:test",
            pr_base="main",
            pr_head="requirement/REQ-1-test",
            pr_title="Title",
            pr_body="Body",
            pr_url="https://github.example/team/repo/pull/1",
            comment_marker="pending",
        ),
    )


def test_comment_requires_pr_url_before_any_ones_call() -> None:
    gateway = FakeGateway()
    run = WorkflowRun.new("requirement", "REQ-1")

    with pytest.raises(ValueError, match="PR URL"):
        commenter(gateway, run).ensure_comment(run)

    assert gateway.add_calls == []


def test_existing_marker_reuses_comment_without_post() -> None:
    run = _run()
    marker = f"<!-- ones-dev-run:{run.run_id} -->"
    gateway = FakeGateway(comments=[{"id": "existing", "text": f"done\n{marker}"}])

    result = commenter(gateway, run).ensure_comment(run)

    assert result == "existing"
    assert gateway.add_calls == []


def test_uncertain_add_rereads_marker_and_recovers_existing_comment() -> None:
    gateway = FakeGateway(fail_after_add=True)
    run = _run()

    result = commenter(gateway, run).ensure_comment(run)

    assert result == "comment-1"
    assert len(gateway.add_calls) == 1


def test_uncertain_add_without_observable_fact_does_not_blindly_retry() -> None:
    class UncertainGateway(FakeGateway):
        def add_comment_sync(self, item_id: str, text: str) -> dict[str, str]:
            self.add_calls.append((item_id, text))
            raise TimeoutError("secret-token")

    gateway = UncertainGateway()

    with pytest.raises(CommentDeliveryUncertain) as caught:
        run = _run()
        commenter(gateway, run).ensure_comment(run)

    assert "secret-token" not in str(caught.value)
    assert len(gateway.add_calls) == 1


def test_comment_text_contains_pr_summary_and_stable_marker() -> None:
    run = _run()
    text = build_comment_text(run, summary="Implemented safely", tests_summary="3 passed")

    assert run.publication.pr_url in text
    assert "Implemented safely" in text
    assert "3 passed" in text
    assert text.endswith(f"<!-- ones-dev-run:{run.run_id} -->")


@pytest.mark.parametrize("unsafe", ["\ud800", "x\x00y", "token=ghp_abcdefghijklmnopqrstuvwxyz123456"])
def test_comment_rejects_invalid_or_secret_bearing_text(unsafe: str) -> None:
    with pytest.raises(ValueError):
        build_comment_text(_run(), summary=unsafe, tests_summary="ok")


def test_comment_length_is_bounded() -> None:
    with pytest.raises(ValueError, match="length"):
        build_comment_text(_run(), summary="x" * 20000, tests_summary="ok", max_length=4096)


def test_two_direct_commenters_with_separate_stores_post_once(tmp_path) -> None:
    from src.developer_workflow.state_store import FileRunStore

    run = _run()
    store1 = FileRunStore(tmp_path / "runs")
    run = store1.create(run)
    store2 = FileRunStore(tmp_path / "runs")

    @dataclass
    class SlowGateway(FakeGateway):
        def add_comment_sync(self, item_id, text):
            time.sleep(0.05)
            return super().add_comment_sync(item_id, text)

    gateway = SlowGateway()
    barrier = threading.Barrier(2)
    results: list[str] = []

    def execute(store):
        barrier.wait()
        results.append(OnesCommenter(gateway, store).ensure_comment(run))

    threads = [threading.Thread(target=execute, args=(store,)) for store in (store1, store2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sorted(results) == ["comment-1", "comment-1"]
    assert len(gateway.add_calls) == 1
