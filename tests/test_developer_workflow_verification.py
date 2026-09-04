from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.developer_workflow import verification as v
from src.developer_workflow import verification_worker as worker
from src.developer_workflow.contracts import CodexResult, WorkflowState
from src.developer_workflow.orchestrator import DeveloperWorkflowOrchestrator, InvalidWorkflowAction
from src.developer_workflow.verification_models import VerificationNeed, VerificationNode, VerificationRecipe, VerificationRecord
from src.developer_workflow.tui.models import RunDetail
from test_developer_workflow_defect import _flow


def need(os_name: str = "windows") -> VerificationNeed:
    return VerificationNeed(description="Validate camera output", capabilities=(f"os:{os_name}", "device:camera"),
                            acceptance="Camera frames remain valid after GPU failure")


def node(os_name: str = "windows", **kwargs) -> VerificationNode:
    return VerificationNode(key=os_name, enabled=True, transport="local", capabilities=need(os_name).capabilities,
        recipes=(VerificationRecipe(key="camera", capabilities=need(os_name).capabilities,
                                    repository_key="app", argv=(sys.executable, "check.py")),), **kwargs)


def reviewed(tmp_path: Path, requested: VerificationNeed | None = None):
    flow, store, repo, codex, _ = _flow(tmp_path)
    # Exercise the opt-in strict pre-PR verification path in this test module.
    flow.config = flow.config.validated_update(publishing=flow.config.publishing.validated_update(
        defer_external_verification_to_pr=False))
    requested = requested or need()
    original = codex.run_stage

    def stage(stage, **kwargs):
        result = original(stage, **kwargs)
        if stage == "review":
            result = result.validated_update(verification_needs=(requested,), review_external_validation=(requested.description,))
        return result

    codex.run_stage = stage
    run = flow.execute(store.run)
    assert run.state is WorkflowState.BLOCKED, run.blocked_reason
    assert run.blocked_reason == v.VERIFICATION_WAITING
    return flow, store, repo, codex, run


@pytest.mark.parametrize("os_name", ["windows", "macos", "linux", "freebsd"])
def test_capability_routing_is_platform_neutral(tmp_path, os_name):
    _, _, _, _, run = reviewed(tmp_path, need(os_name))
    nodes = (node("other"), node(os_name))
    task, = v.plan(run, nodes)
    assert task.node_key == os_name and task.status == "ready"
    assert v.pending_reason((task,)) == v.VERIFICATION_READY


def test_missing_gpu_capability_does_not_match_a_platform_only_node(tmp_path):
    _, _, _, _, run = reviewed(tmp_path)
    bare = node().model_copy(update={"capabilities": ("os:windows",)})
    assert v.plan(run, (bare,))[0].status == "waiting_environment"


def test_legacy_text_never_silently_disappears(tmp_path):
    _, _, _, _, run = reviewed(tmp_path)
    run = run.validated_update(review=run.review.validated_update(verification_needs=()))
    assert v.plan(run, (node(),))[0].status == "manual"


def record(task, status="passed"):
    return VerificationRecord(task_key=task.key, snapshot_digest=task.snapshot_digest, node_key=task.node_key, bundle_digest="b" * 64,
        recipe_key=task.recipe_key, recipe_digest=task.recipe_digest, status=status, exit_code=0 if status == "passed" else 1,
        actor="tester", evidence="Actual camera check log", output_sha256="a" * 64, occurred_at="2026-09-03T00:00:00Z")


def test_results_are_invalidated_by_snapshot_recipe_and_acceptance_changes(tmp_path):
    _, _, _, _, run = reviewed(tmp_path)
    task, = v.plan(run, (node(),))
    run = run.validated_update(verification_records=(record(task),))
    assert v.plan(run, (node(),))[0].status == "passed"
    changed = run.validated_update(tested_snapshot=run.tested_snapshot.validated_update(diff_sha256="b" * 64))
    assert v.plan(changed, (node(),))[0].status == "ready"
    changed_node = node().model_copy(update={"recipes": (node().recipes[0].model_copy(update={"argv": ("other",)}),)})
    assert v.plan(run, (changed_node,))[0].status == "ready"
    changed = run.validated_update(review=run.review.validated_update(verification_needs=(need().model_copy(update={"acceptance": "different"}),)))
    assert v.plan(changed, (node(),))[0].status == "ready"


def orchestrator(flow, store):
    return DeveloperWorkflowOrchestrator(store=store, config=flow.config, defect_flow=flow,
        requirement_flow=SimpleNamespace(), publisher=SimpleNamespace(), defect_candidates=SimpleNamespace())


def test_manual_verification_reaches_approval_without_repeating_review(tmp_path):
    flow, store, _, codex, run = reviewed(tmp_path)
    service = orchestrator(flow, store)
    before = tuple(codex.stages)
    # Repeated resume is cheap and never repeats the same review to acquire a device.
    run = flow.execute(run)
    assert tuple(codex.stages) == before
    result = service.verify(run.run_id, run.verification_plan[0].key, "operator",
                            expected_version=run.version, manual_evidence="Mac lab A: frames and failure recovery pass, log /results/001.txt")
    assert result.state is WorkflowState.WAITING_APPROVAL, result.blocked_reason
    assert tuple(codex.stages) == before
    assert result.approval.verification_records[0].node_key == "manual"
    assert result.verification_plan[0].status == "passed"


@pytest.mark.parametrize("status", ["passed", "failed", "error"])
def test_node_result_advances_or_routes_without_false_success(tmp_path, monkeypatch, status):
    flow, store, _, codex, run = reviewed(tmp_path)
    flow.config = flow.config.validated_update(verification_nodes=(node(),))
    service = orchestrator(flow, store)
    monkeypatch.setattr(v, "execute", lambda run, task, node, actor: record(task, status))
    result = service.verify(run.run_id, run.verification_plan[0].key, "tester", expected_version=run.version,
                            expected_recipe_digest=v.plan(run, (node(),))[0].recipe_digest)
    if status == "passed":
        assert result.state is WorkflowState.WAITING_APPROVAL, result.blocked_reason
    else:
        assert result.state is WorkflowState.BLOCKED
        assert result.approval is None
        assert result.blocked_reason == v.VERIFICATION_FAILED
    assert codex.stages.count("review") == (2 if status == "failed" else 1)


def test_stale_version_cannot_execute_node(tmp_path, monkeypatch):
    flow, store, _, _, run = reviewed(tmp_path)
    monkeypatch.setattr(v, "execute", lambda *args: pytest.fail("must not execute"))
    with pytest.raises(InvalidWorkflowAction, match="workflow changed"):
        orchestrator(flow, store).verify(run.run_id, run.verification_plan[0].key, "tester", expected_version=run.version - 1)


def test_node_not_configured_cannot_execute(tmp_path):
    flow, store, _, _, run = reviewed(tmp_path)
    with pytest.raises(InvalidWorkflowAction, match="matching"):
        orchestrator(flow, store).verify(run.run_id, run.verification_plan[0].key, "tester", expected_version=run.version)


def test_reviewer_omission_cannot_erase_a_failed_verification(tmp_path, monkeypatch):
    flow, store, _, codex, run = reviewed(tmp_path)
    flow.config = flow.config.validated_update(verification_nodes=(node(),))
    original = codex.run_stage
    def stage(stage, **kwargs):
        result = original(stage, **kwargs)
        return result.validated_update(verification_needs=(), review_external_validation=()) if stage == "review" else result
    codex.run_stage = stage
    monkeypatch.setattr(v, "execute", lambda run, task, node, actor: record(task, "failed"))
    result = orchestrator(flow, store).verify(run.run_id, run.verification_plan[0].key, "tester", expected_version=run.version,
                                             expected_recipe_digest=v.plan(run, (node(),))[0].recipe_digest)
    assert result.state is WorkflowState.BLOCKED and result.approval is None
    assert result.verification_plan[0].status == "failed"


def request(script: bytes, timeout=5):
    files = {"app/check.py": {"sha256": hashlib.sha256(script).hexdigest(),
                             "data": base64.b64encode(script).decode(), "executable": False}}
    return {"operation": "execute", "files": files, "bundle_digest": worker.digest({k: {"sha256": value["sha256"], "executable": False} for k, value in files.items()}),
            "snapshot_digest": "a" * 64, "recipe": {"repository_key": "app", "argv": [sys.executable, "check.py"], "timeout_seconds": timeout}}


@pytest.mark.parametrize("script,status", [(b"print('passed')", "passed"), (b"raise SystemExit(1)", "failed"),
    (b"from pathlib import Path; Path('check.py').write_text('print(1)')", "error")])
def test_real_worker_executes_and_protects_tested_sources(tmp_path, monkeypatch, script, status):
    monkeypatch.setattr(worker.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    answer = worker.execute(request(script))
    assert answer["status"] == status
    assert answer["snapshot_digest"] == "a" * 64
    assert Path(answer["artifacts_directory"]).joinpath("verification-output.log").exists()


def test_real_worker_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(worker.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    answer = worker.execute(request(b"import time; time.sleep(20)", timeout=1))
    assert answer["status"] == "error" and answer["exit_code"] is None


def test_worker_checks_platform_before_running_commands():
    data = request(b"raise AssertionError('must not run')")
    data["capabilities"] = ["os:impossible"]
    with pytest.raises(ValueError, match="platform"):
        worker.execute(data)


def test_worker_does_not_inherit_controller_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ONES_TOKEN", "sensitive-example")
    monkeypatch.setenv("OPENAI_API_KEY", "another-example")
    monkeypatch.setattr(worker.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    result = worker.execute(request(b"import os; assert 'ONES_TOKEN' not in os.environ; assert 'OPENAI_API_KEY' not in os.environ"))
    assert result["status"] == "passed"


def test_missing_node_interpreter_has_an_actionable_safe_error():
    data = request(b"print('never executed')")
    data["recipe"]["argv"] = ["nonexistent-verification-runtime-9a31"]
    with pytest.raises(ValueError, match="missing_runtime") as caught:
        v.invoke(node(), data, 20)
    assert "解释器" in v.failure_message(caught.value)


def test_group_snapshot_binding_includes_each_repository(tmp_path):
    from src.developer_workflow.contracts import RepositoryGroupMapping, RepositoryRunEvidence
    _, _, _, _, run = reviewed(tmp_path)
    app = run.repository
    sdk = app.validated_update(key="sdk", repo_name="sdk", repo_url=str(tmp_path / "sdk.git"), role="dependency")
    group = RepositoryGroupMapping(key="product", project_id=app.project_id, iteration_id=app.iteration_id,
                                   primary_repository=app.key, repositories=(app, sdk))
    evidence = tuple(RepositoryRunEvidence(repository_key=mapping.key, mapping=mapping,
        prepared_worktree=run.prepared_worktree, tested_snapshot=run.tested_snapshot) for mapping in (app, sdk))
    run = run.validated_update(repository_group=group, repository=None, repository_evidence=evidence)
    before = v.snapshot_digest(run)
    changed = evidence[1].validated_update(tested_snapshot=run.tested_snapshot.validated_update(diff_sha256="b" * 64))
    assert v.snapshot_digest(run.validated_update(repository_evidence=(evidence[0], changed))) != before
    with pytest.raises(ValueError, match="missing tested"):
        v.snapshot_digest(run.validated_update(repository_evidence=(evidence[0], evidence[1].validated_update(tested_snapshot=None))))


@pytest.mark.parametrize("path", ["../a", "/a", "a/../b", "a/.git/config", "a\\b", "app/NUL.txt", "app/a.", "app/a:stream", "a//b"])
def test_bundle_paths_cannot_escape_or_alias(path):
    with pytest.raises(ValueError):
        worker.safe_path(path)


def test_worker_rejects_forged_manifest():
    data = request(b"print(1)")
    data["bundle_digest"] = "b" * 64
    with pytest.raises(ValueError, match="manifest"):
        worker.execute(data)


def test_local_worker_protocol_probe():
    answer = v.invoke(node(), {"operation": "probe"}, 20)
    assert answer["protocol"] == 1 and answer["system"] == platform.system().lower()


def test_ssh_requires_known_host_and_rejects_shell_injection():
    remote = VerificationNode(key="mac", ssh_alias="lab-mac", worker_argv=("python3", "/opt/verification_worker.py"))
    assert "StrictHostKeyChecking=yes" in v.worker_command(remote)
    assert "BatchMode=yes" in v.worker_command(remote)
    with pytest.raises(ValidationError):
        remote.model_validate({**remote.model_dump(), "worker_argv": ["python3;shutdown"]})
    with pytest.raises(ValidationError):
        VerificationNode(key="manual", transport="local")


def test_ui_shows_environment_action_instead_of_continue_review(tmp_path):
    _, _, _, _, run = reviewed(tmp_path)
    detail = RunDetail.from_run(run)
    assert detail.can_verify and detail.verification_tasks[0].status == "waiting_environment"


def test_public_logs_redact_common_credentials():
    output = v.public_text("password=abc token=xyz Bearer aabb\x1b[31m\nnext")
    assert "abc" not in output and "xyz" not in output and "aabb" not in output and "\x1b" not in output


def test_actual_local_node_bundle_execution_reaches_approval(tmp_path):
    flow, store, repo, _, run = reviewed(tmp_path, need(platform.system().lower()))
    local = node(platform.system().lower())
    flow.config = flow.config.validated_update(verification_nodes=(local,))
    subprocess.run(["git", "init", "--quiet"], cwd=repo.root, check=True, capture_output=True)
    (repo.root / "check.py").write_text("print('verification executed on a copied snapshot')", encoding="utf-8")
    service = orchestrator(flow, store)
    task, = v.plan(run, (local,))
    result = service.verify(run.run_id, task.key, "tester", expected_version=run.version,
                            expected_recipe_digest=task.recipe_digest)
    assert result.state is WorkflowState.WAITING_APPROVAL, result.blocked_reason
    evidence, = result.approval.verification_records
    assert evidence.exit_code == 0 and evidence.bundle_digest
    assert "verification executed on a copied snapshot" in evidence.evidence


def test_recipe_config_change_requires_a_new_confirmation(tmp_path, monkeypatch):
    flow, store, _, _, run = reviewed(tmp_path)
    flow.config = flow.config.validated_update(verification_nodes=(node(),))
    monkeypatch.setattr(v, "execute", lambda *args: pytest.fail("unconfirmed execution"))
    with pytest.raises(InvalidWorkflowAction, match="recipe changed"):
        orchestrator(flow, store).verify(run.run_id, run.verification_plan[0].key, "tester",
                                         expected_version=run.version, expected_recipe_digest="f" * 64)


def test_protected_files_are_not_copied_to_nodes(tmp_path):
    _, _, repo, _, run = reviewed(tmp_path)
    subprocess.run(["git", "init", "--quiet"], cwd=repo.root, check=True, capture_output=True)
    (repo.root / ".env").write_text("EXAMPLE=not-a-secret", encoding="utf-8")
    with pytest.raises(ValueError, match="protected"):
        v.export_bundle(run)


def test_node_editor_persists_without_changing_repository_configuration(tmp_path):
    from src.developer_workflow.tui.controller import TuiController, TuiControllerError
    flow, store, _, _, _ = reviewed(tmp_path)
    service = orchestrator(flow, store)
    service.requirement_flow = SimpleNamespace(config=flow.config)
    saved = []
    controller = TuiController(service, SimpleNamespace(), workflow_saver=saved.append)
    before = flow.config.repositories
    try:
        controller.save_verification_nodes(json.dumps([node().model_dump(mode="json")]), v.digest(()))
        assert saved[0].verification_nodes == (node(),)
        assert flow.config.verification_nodes == (node(),)
        assert flow.config.repositories == before
        with pytest.raises(TuiControllerError):
            controller.save_verification_nodes("[]", v.digest(()))
        with pytest.raises(TuiControllerError):
            controller.save_verification_nodes('[{"key":"x","password":"forbidden"}]', v.digest(controller.verification_nodes()))
        assert len(saved) == 1
    finally:
        controller.close()


def test_setup_roundtrip_preserves_nodes(tmp_path):
    from src.developer_workflow.setup_models import WorkflowDraft
    from src.developer_workflow.config import DeveloperWorkflowConfig
    flow, _, _, _, _ = reviewed(tmp_path)
    config = flow.config.validated_update(verification_nodes=(node(),))
    draft = WorkflowDraft.model_validate(config.model_dump())
    assert DeveloperWorkflowConfig.model_validate(draft.model_dump()).verification_nodes == (node(),)


def test_requirement_review_cannot_skip_external_checks(tmp_path):
    from test_developer_workflow_requirement import _flow as requirement_flow
    flow, store = requirement_flow(tmp_path)
    flow.config = flow.config.validated_update(publishing=flow.config.publishing.validated_update(
        defer_external_verification_to_pr=False))
    run = flow.execute(store.run)
    assert run.state is WorkflowState.VALIDATING
    run = run.validated_update(repository=flow.config.repositories[0])
    store.run = run
    original = flow.codex.run_stage
    def stage(stage, **kwargs):
        result = original(stage, **kwargs)
        return result.validated_update(verification_needs=(need(),)) if stage == "review" else result
    flow.codex.run_stage = stage
    run = flow.execute(run)
    assert run.state is WorkflowState.BLOCKED and run.blocked_reason == v.VERIFICATION_WAITING
    task, = run.verification_plan
    manual = VerificationRecord(task_key=task.key, snapshot_digest=task.snapshot_digest, node_key="manual",
        status="passed", actor="operator", evidence="Verified the current snapshot on the target device, results archived.",
        output_sha256="a" * 64, occurred_at="2026-09-03T00:00:00Z")
    run = store.save(run.validated_update(verification_records=(manual,)), run.version)
    before = list(flow.codex.stages)
    run = flow.execute(run)
    assert run.state is WorkflowState.WAITING_APPROVAL, run.blocked_reason
    assert flow.codex.stages == before and run.approval.verification_records == (manual,)


@pytest.mark.asyncio
async def test_verification_modal_requires_consent_and_runs_in_background(tmp_path):
    import asyncio
    from threading import Event
    from textual.widgets import Button, Checkbox, Input
    from src.developer_workflow.tui.verification_modal import VerificationModal
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from test_developer_workflow_tui_app import FakeController
    _, _, _, _, run = reviewed(tmp_path)
    run = run.validated_update(verification_plan=v.plan(run, (node(),)))
    detail = RunDetail.from_run(run)
    entered, release = Event(), Event()
    class Controller(FakeController):
        def __init__(self):
            super().__init__()
            self.runs = (detail.summary,)
        def show(self, _):
            return detail
        def verification_nodes(self):
            return (node().model_dump(mode="json"),)
        def verify(self, *args):
            assert args[-1] == detail.verification_tasks[0].recipe_digest
            entered.set()
            release.wait(5)
            return detail
    app = DeveloperWorkflowTuiApp(Controller(), 3)
    try:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            assert app.screen.query_one("#action-resume", Button).label.plain == "环境验证"
            await pilot.click("#action-resume")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, VerificationModal)
            modal.query_one("#verification-actor", Input).value = "operator"
            modal._confirm()
            assert not entered.is_set() and app.screen is modal
            modal.query_one("#verification-consent", Checkbox).value = True
            modal._confirm()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if entered.is_set():
                    break
            assert entered.is_set()
            assert not isinstance(app.screen, VerificationModal)
            await pilot.press("question_mark")
            await pilot.pause()
            assert not release.is_set()  # The worker is still running while UI events are processed.
            release.set()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_configuration_node_editor_saves_only_on_confirmation():
    from textual.widgets import Input, Button, TabbedContent
    from src.developer_workflow.tui.verification_settings import VerificationNodeDetails
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from test_developer_workflow_tui_app import FakeController
    saved = []
    class Controller(FakeController):
        def verification_nodes(self):
            return ()
        def verification_repositories(self):
            return ("app",)
        def save_verification_nodes(self, raw, expected_digest):
            assert expected_digest == v.digest(())
            saved.append(json.loads(raw))
    app = DeveloperWorkflowTuiApp(Controller(), 3)
    async with app.run_test(size=(120, 42)) as pilot:
        await pilot.click("#nav-settings")
        app.screen.query_one("#configuration-tabs", TabbedContent).active = "settings-nodes"
        await pilot.pause()
        assert not saved
        await pilot.click("#configuration-node-add")
        await pilot.pause()
        assert isinstance(app.screen, VerificationNodeDetails)
        assert app.screen.repositories == ("app",)
        app.screen.query_one("#node-key", Input).value = "local-test"
        app.screen.query_one("#node-save", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert saved == [[VerificationNode(key="local-test", transport="local").model_dump(mode="json")]]
        assert not isinstance(app.screen, VerificationNodeDetails)
