from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Input, Static, TabbedContent

from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
from src.developer_workflow.tui.ones_settings import OnesSettingsPane
from src.developer_workflow.setup_models import SecretKind
from src.developer_workflow.setup_controller import SetupActionError
from test_developer_workflow_configuration_tabs import ConfigurationController
from test_developer_workflow_setup_controller import _controller, _candidate_for_store


FIELDS = {"ones_base_url": "https://ones.example.test", "ones_team_id": "team-1", "ones_issue_type_id": "issue-1"}


@pytest.mark.parametrize("size", [(140, 42), (80, 24)])
async def test_inline_editor_loads_saves_without_another_screen(size):
    app = DeveloperWorkflowTuiApp(ConfigurationController(), 3)
    app.read_inline_ones = AsyncMock(return_value=FIELDS)
    saved = []

    async def save(fields, credentials):
        saved.append((dict(fields), dict(credentials)))

    app.save_inline_ones = save
    async with app.run_test(size=size) as pilot:
        dashboard = app.screen
        dashboard.action_show_settings()
        await pilot.pause()
        assert app.screen is dashboard
        assert not dashboard.query("#settings-ones #nav-runtime-setup")
        assert dashboard.query_one("#inline-ones-base_url", Input).value == FIELDS["ones_base_url"]
        for name in ("email", "password"):
            widget = dashboard.query_one(f"#inline-ones-{name}", Input)
            assert widget.password and widget.value == ""
        dashboard.query_one("#inline-ones-team_id", Input).value = "new-team"
        dashboard.query_one("#inline-ones-password", Input).value = "transient-secret"
        tabs = dashboard.query_one("#configuration-tabs", TabbedContent)
        tabs.active = "settings-runtime"
        tabs.active = "settings-ones"
        await pilot.pause()
        assert dashboard.query_one("#inline-ones-team_id", Input).value == "new-team"
        dashboard.query_one(OnesSettingsPane).save()
        await pilot.pause()
        assert saved[0][0]["ones_team_id"] == "new-team"
        assert saved[0][1][SecretKind.ONES_PASSWORD] == "transient-secret"
        assert dashboard.query_one("#inline-ones-password", Input).value == ""
        assert app.screen is dashboard


async def test_inline_load_failure_disables_save_and_sanitizes():
    app = DeveloperWorkflowTuiApp(ConfigurationController(), 3)
    app.read_inline_ones = AsyncMock(side_effect=RuntimeError("PRIVATE"))
    async with app.run_test() as pilot:
        app.screen.action_show_settings()
        await pilot.pause()
        assert app.screen.query_one("#inline-ones-save", Button).disabled
        assert "PRIVATE" not in str(app.screen.query_one("#inline-ones-notice", Static).render())


async def test_inline_preparation_preserves_other_modules_and_secrets(tmp_path):
    controller, store, _, _ = _controller(tmp_path)
    active = _candidate_for_store(tmp_path, "a" * 32)
    store.document = store.document.validated_update(active=active)
    try:
        await controller.prepare_inline_ones(FIELDS, {SecretKind.ONES_EMAIL: "editor@example.test"})
        assert controller.draft.workflow.model_dump() == active.workflow.model_dump()
        candidate, secrets = controller._build_candidate()
        assert secrets.require(SecretKind.PROVIDER_TOKEN) == "persisted-value"
        assert secrets.require(SecretKind.ONES_PASSWORD) == "persisted-value"
        assert candidate.runtime.provider_host == active.runtime.provider_host
        assert store.commits == 0  # Validation is not persistence or activation.
    finally:
        await controller.aclose()


async def test_changed_host_cannot_receive_retained_password(tmp_path):
    controller, store, _, _ = _controller(tmp_path)
    store.document = store.document.validated_update(active=_candidate_for_store(tmp_path, "a" * 32))
    try:
        with pytest.raises(SetupActionError, match="explicit credentials"):
            await controller.prepare_inline_ones({**FIELDS, "ones_base_url": "https://other.example.test"}, {})
        assert store.commits == 0
    finally:
        await controller.aclose()


@pytest.mark.parametrize("failed", [False, True])
async def test_inline_save_reuses_runtime_transition_without_setup_navigation(failed):
    app = DeveloperWorkflowTuiApp(ConfigurationController(), 3)
    session = SimpleNamespace(close=AsyncMock(), close_complete=True)
    app.runtime_session = session
    app._dashboard = None
    app._remove_dashboard = AsyncMock()
    editor = SimpleNamespace(load_active_public_draft=Mock(),
        prepare_inline_ones=AsyncMock(side_effect=RuntimeError("private") if failed else None),
        save_and_activate=AsyncMock(return_value="new-handle"),
        activate_existing=AsyncMock(return_value="old-handle"))
    app._new_setup_controller = Mock(return_value=editor)
    app._finish_setup = AsyncMock()
    app._show_setup = AsyncMock()
    app.notify = Mock()
    await app.save_inline_ones(FIELDS, {})
    session.close.assert_awaited_once()
    app._finish_setup.assert_awaited_once_with("old-handle" if failed else "new-handle")
    app._show_setup.assert_not_awaited()
    assert "private" not in str(app.notify.call_args)
    if failed:
        editor.save_and_activate.assert_not_awaited()
