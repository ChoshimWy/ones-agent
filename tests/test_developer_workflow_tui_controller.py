from __future__ import annotations

from dataclasses import fields
from contextlib import contextmanager
from types import SimpleNamespace
import threading

import pytest

from src.developer_workflow.contracts import (
    DefectCandidate,
    WorkflowRun,
    WorkflowState,
    WorkflowType,
)
from src.developer_workflow.orchestrator import (
    DeveloperWorkflowOrchestrator,
    InvalidWorkflowAction,
)
from src.developer_workflow.state_store import FileRunStore
from src.developer_workflow.tui.controller import (
    CandidateSessionView,
    StaleTuiActionError,
    TuiController,
    TuiControllerError,
)
from src.developer_workflow.tui.models import DangerousActionRequest, RunDetail, RunFilter
from src.developer_workflow.tui.run_index import RunIndex


class Candidates:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    async def list_candidates(self, project, iteration, assignee, *, status_ids=None):
        self.calls.append((project, iteration, assignee, status_ids))
        value = self.batches.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class Orchestrator:
    def __init__(self, batches=()):
        self.defect_candidates = Candidates(batches)
        self.calls = []
        self.run = WorkflowRun.new(WorkflowType.REQUIREMENT, "REQ-1")

    def show(self, run_id):
        self.calls.append(("show", run_id))
        return self.run

    def start_defect(self, *args):
        self.calls.append(("start_defect", *args))
        return self.run

    def start_requirement(self, item_id):
        self.calls.append(("start_requirement", item_id))
        return self.run

    def confirm_repository(self, run_id, mapping_key, *, expected_version=None):
        self.calls.append(("confirm", run_id, mapping_key, expected_version))
        return self.run

    def resume(self, run_id, *, expected_version=None):
        self.calls.append(("resume", run_id, expected_version))
        return self.run

    def cancel(self, run_id, actor, *, expected_version=None):
        self.calls.append(("cancel", run_id, actor, expected_version))
        return self.run


class Index:
    def list(self, filters, activities=None):
        return (filters, activities)


def candidate(candidate_id="D-1", token="SECRET-TOKEN"):
    return DefectCandidate(
        uuid=candidate_id,
        key="D-1",
        number="1",
        title="broken export",
        priority="high",
        status="doing",
        updated_at="2026-01-01",
        snapshot_token=token,
        status_id="doing",
    )


def test_query_hides_token_and_forwards_exact_status_ids():
    orchestrator = Orchestrator([(candidate(),)])
    controller = TuiController(orchestrator, Index())

    view = controller.query_defects("P", "I", "A", ("doing", "todo"))

    assert isinstance(view, CandidateSessionView)
    assert orchestrator.defect_candidates.calls == [("P", "I", "A", ("doing", "todo"))]
    assert "SECRET-TOKEN" not in repr(view)
    assert all(field.name != "snapshot_token" for field in fields(view))
    assert view.items[0].candidate_id == "D-1"


def test_query_rejects_mixed_tokens_and_does_not_create_empty_or_failed_sessions():
    orchestrator = Orchestrator([
        (),
        (candidate("D-1", "one"), candidate("D-2", "two")),
        RuntimeError("TOKEN-INNER"),
    ])
    controller = TuiController(orchestrator, Index())
    empty = controller.query_defects("P", "I", "A", ())
    assert empty.items == ()
    with pytest.raises(TuiControllerError, match="candidate snapshot is invalid"):
        controller.start_defect(empty.session_id, "D-1")
    with pytest.raises(TuiControllerError, match="candidate snapshot is invalid"):
        controller.query_defects("P", "I", "A", ())
    with pytest.raises(TuiControllerError) as caught:
        controller.query_defects("P", "I", "A", ())
    assert "TOKEN-INNER" not in str(caught.value)


def test_start_defect_consumes_hidden_capability_even_when_orchestrator_fails():
    orchestrator = Orchestrator([(candidate(),)])
    controller = TuiController(orchestrator, Index())
    view = controller.query_defects("P", "I", "A", ())
    orchestrator.start_defect = lambda *args: (_ for _ in ()).throw(RuntimeError("TOKEN-INNER"))

    with pytest.raises(TuiControllerError) as caught:
        controller.start_defect(view.session_id, "D-1")
    assert "TOKEN-INNER" not in str(caught.value)
    with pytest.raises(TuiControllerError, match="candidate snapshot is invalid"):
        controller.start_defect(view.session_id, "D-1")


def test_start_defect_rejects_malformed_identifiers_with_fixed_error():
    orchestrator = Orchestrator([(candidate(),)])
    controller = TuiController(orchestrator, Index())
    with pytest.raises(TuiControllerError, match="^candidate snapshot is invalid$"):
        controller.start_defect([], "D-1")
    view = controller.query_defects("P", "I", "A", ())
    with pytest.raises(TuiControllerError, match="^candidate snapshot is invalid$"):
        controller.start_defect(view.session_id, [])


def test_sessions_are_bounded_and_one_shot_under_concurrency():
    orchestrator = Orchestrator([(candidate(),), (candidate(),), (candidate(),)])
    controller = TuiController(orchestrator, Index(), max_candidate_sessions=2)
    first = controller.query_defects("P", "I", "A", ())
    second = controller.query_defects("P", "I", "A", ())
    third = controller.query_defects("P", "I", "A", ())
    with pytest.raises(TuiControllerError):
        controller.start_defect(first.session_id, "D-1")

    barrier = threading.Barrier(3)
    outcomes = []
    def invoke():
        barrier.wait()
        try:
            controller.start_defect(second.session_id, "D-1")
            outcomes.append("ok")
        except TuiControllerError:
            outcomes.append("error")
    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join()
    assert sorted(outcomes) == ["error", "ok"]
    assert third.session_id != second.session_id


def test_sync_adapters_return_only_views_and_forward_expected_versions():
    orchestrator = Orchestrator()
    controller = TuiController(orchestrator, Index())
    filters = RunFilter()
    assert controller.list_runs(filters, {"r": "busy"}) == (filters, {"r": "busy"})
    assert isinstance(controller.show("r"), RunDetail)
    assert isinstance(controller.start_requirement("REQ-1"), RunDetail)
    assert isinstance(controller.confirm_repository("r", "repo", 3), RunDetail)
    assert isinstance(controller.resume("r", 4), RunDetail)
    assert ("confirm", "r", "repo", 3) in orchestrator.calls
    assert ("resume", "r", 4) in orchestrator.calls


def test_cancel_asserts_authoritative_facts_then_forwards_bound_version():
    orchestrator = Orchestrator()
    controller = TuiController(orchestrator, Index())
    request = controller.prepare_action(orchestrator.run.run_id, "cancel")
    result = controller.cancel(request, "operator")
    assert isinstance(result, RunDetail)
    assert ("cancel", orchestrator.run.run_id, "operator", request.version) in orchestrator.calls

    drifted = orchestrator.run.validated_update(version=orchestrator.run.version + 1)
    orchestrator.run = drifted
    with pytest.raises(StaleTuiActionError, match="^workflow changed; review again$"):
        controller.cancel(request, "SECRET-ACTOR")


def test_constructor_requires_strict_positive_capacity():
    for value in (0, -1, True, 1.5):
        with pytest.raises(TuiControllerError):
            TuiController(Orchestrator(), Index(), max_candidate_sessions=value)


def test_controller_precheck_cannot_race_orchestrator_version_gate(tmp_path):
    store = FileRunStore(tmp_path / "runs")
    run = store.create(WorkflowRun.new(WorkflowType.REQUIREMENT, "REQ-1"))
    side_effects = []
    flow = SimpleNamespace(execute=lambda current: side_effects.append(("flow", current)))
    publisher = SimpleNamespace(
        publish=lambda current: side_effects.append(("publish", current)),
        retry_comment=lambda current: side_effects.append(("retry", current)),
    )
    orchestrator = DeveloperWorkflowOrchestrator(
        store=store,
        requirement_flow=flow,
        defect_flow=flow,
        publisher=publisher,
        config=None,
        defect_candidates=None,
    )
    controller = TuiController(orchestrator, RunIndex(store))
    request = controller.prepare_action(run.run_id, "cancel")
    entered = threading.Event()
    release = threading.Event()
    original_lock = store.operation_lock

    @contextmanager
    def delayed_lock(run_id, purpose):
        entered.set()
        assert release.wait(5)
        with original_lock(run_id, purpose):
            yield

    store.operation_lock = delayed_lock
    errors = []
    worker = threading.Thread(
        target=lambda: _capture_stale(controller, request, errors)
    )
    worker.start()
    assert entered.wait(5)
    store.transition(
        run.run_id,
        run.version,
        target=WorkflowState.READING_ONES,
        reason="competing authoritative update",
    )
    release.set()
    worker.join(5)

    assert errors and isinstance(errors[0], StaleTuiActionError)
    assert str(errors[0]) == "workflow changed; review again"
    assert store.load(run.run_id).state.value == "READING_ONES"
    assert side_effects == []


def test_read_adapters_sanitize_lower_layer_errors():
    orchestrator = Orchestrator()
    controller = TuiController(orchestrator, SimpleNamespace(
        list=lambda *args: (_ for _ in ()).throw(OSError("TOKEN-INNER"))
    ))
    with pytest.raises(TuiControllerError) as listed:
        controller.list_runs(RunFilter())
    assert "TOKEN-INNER" not in str(listed.value)

    orchestrator.show = lambda run_id: (_ for _ in ()).throw(OSError("TOKEN-INNER"))
    with pytest.raises(TuiControllerError) as shown:
        controller.show("run")
    assert "TOKEN-INNER" not in str(shown.value)


def test_generic_resume_cannot_bypass_publication_confirmation():
    orchestrator = Orchestrator()
    orchestrator.run = orchestrator.run.validated_update(
        state=WorkflowState.PUBLISHING, version=4
    )
    controller = TuiController(orchestrator, Index())
    with pytest.raises(TuiControllerError, match="workflow action is unavailable"):
        controller.resume(orchestrator.run.run_id, 4)
    assert not any(call[0] == "resume" for call in orchestrator.calls)


def test_atomic_stale_from_regular_versioned_commands_has_stale_type():
    orchestrator = Orchestrator()
    orchestrator.confirm_repository = lambda *args, **kwargs: (
        (_ for _ in ()).throw(InvalidWorkflowAction("workflow changed; review again"))
    )
    controller = TuiController(orchestrator, Index())
    with pytest.raises(StaleTuiActionError, match="^workflow changed; review again$"):
        controller.confirm_repository("run", "repo", 3)


def _capture_stale(controller, request, errors):
    try:
        controller.cancel(request, "operator")
    except Exception as exc:
        errors.append(exc)
