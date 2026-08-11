from __future__ import annotations

from datetime import UTC, datetime
import io

import pytest
from rich.console import Console
from textual.widgets import Input

from src.developer_workflow.contracts import WorkflowState, WorkflowType
from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
from src.developer_workflow.tui.models import (
    DangerousActionRequest,
    PublicationView,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
)


NOW = datetime(2026, 8, 11, tzinfo=UTC)


class _SecurityController:
    def __init__(self) -> None:
        self.summary = RunSummary(
            run_id="security-run",
            workflow_type=WorkflowType.DEFECT,
            work_item_id="BUG-7",
            state=WorkflowState.WAITING_APPROVAL,
            version=7,
            updated_at=NOW,
            activity=RunActivity.IDLE,
        )

    def list_runs(self, filters: RunFilter, activities=None):
        del filters, activities
        return (self.summary,)

    def show(self, run_id: str) -> RunDetail:
        assert run_id == self.summary.run_id
        return RunDetail(
            summary=self.summary,
            repositories=(),
            tests=(),
            review=("reviewed safely",),
            publication=PublicationView(repositories=(), comment_id="", error=""),
            history=(),
            blocked_reason="",
            fingerprint="f" * 64,
            risk_count=0,
            unresolved_count=0,
        )

    def prepare_action(self, run_id: str, action: str) -> DangerousActionRequest:
        assert run_id == self.summary.run_id
        return DangerousActionRequest(
            run_id=run_id,
            version=self.summary.version,
            action=action,  # type: ignore[arg-type]
            fingerprint="f" * 64,
            work_item_id=self.summary.work_item_id,
            repositories=(),
            changed_file_count=0,
            test_count=0,
            risk_count=0,
            unresolved_count=0,
            state=self.summary.state,
        )


def _rendered_ui(app: DeveloperWorkflowTuiApp) -> str:
    """Serialize every mounted render surface without sanitizing its output."""

    rendered: list[str] = []
    for widget in app.query("*"):
        surface = widget.render()
        output = io.StringIO()
        console = Console(
            file=output,
            width=160,
            color_system=None,
            force_terminal=False,
        )
        console.print(surface)
        rendered.append(output.getvalue())
        label = getattr(widget, "label", None)
        if label is not None:
            rendered.append(str(label))
        renderable = getattr(widget, "renderable", None)
        if renderable is not None:
            rendered.append(str(renderable))
    notifications = getattr(app, "_notifications", ())
    for notification in notifications:
        rendered.extend(
            str(getattr(notification, field, ""))
            for field in ("title", "message", "severity")
        )
    return "\n".join(rendered)


@pytest.mark.asyncio
async def test_parent_credentials_paths_controls_and_rich_markup_never_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = {
        "CODEX_API_KEY": "CODEX-SECRET-[bold]do-not-render[/bold]",
        "CODEX_AUTH_TOKEN": "CODEX-AUTH-SECRET-\x1b[31m",
        "OPENAI_API_KEY": "OPENAI-SECRET-VALUE",
        "ONES_PASSWORD": "ONES-PASSWORD-SECRET",
        "ONES_API_TOKEN": "ONES-TOKEN-SECRET",
        "ONES_EMAIL": "private-person-secret@example.invalid",
        "ONES_BASE_URL": "https://ones.invalid/PRIVATE-ONES-PATH-SECRET",
        "ONES_DEV_PROVIDER_TOKEN": "PROVIDER-TOKEN-SECRET",
        "GIT_ASKPASS": "E:/PRIVATE-GIT-PATH-SECRET/[italic]askpass[/italic].exe",
        "GIT_SSH_COMMAND": "ssh -i E:/PRIVATE-SSH-KEY-SECRET",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    app = DeveloperWorkflowTuiApp(
        _SecurityController(),  # type: ignore[arg-type]
        3,
        provider_type="github",
        sandbox_configured=True,
        poll_interval=10,
    )
    observed: list[str] = []
    async with app.run_test(size=(120, 32)) as pilot:
        observed.append(_rendered_ui(app))
        await pilot.press("?")
        observed.append(_rendered_ui(app))
        await pilot.press("escape", "n")
        observed.append(_rendered_ui(app))
        await pilot.press("escape", "a")
        observed.append(_rendered_ui(app))
        app.screen.query_one("#actor", Input).value = "operator"

    complete_render = "\n".join(observed)
    for secret in secrets.values():
        assert secret not in complete_render
    for unmistakable_secret_fragment in (
        "CODEX-SECRET",
        "PRIVATE-ONES-PATH-SECRET",
        "PRIVATE-GIT-PATH-SECRET",
        "PRIVATE-SSH-KEY-SECRET",
        "private-person-secret@example.invalid",
    ):
        assert unmistakable_secret_fragment not in complete_render
