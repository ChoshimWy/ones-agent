from __future__ import annotations

import pytest

from types import MappingProxyType

from src.developer_workflow.setup_controller import SetupController
from src.developer_workflow.setup_validation import (
    OnesProbeInput,
    SetupStep,
    SetupValidator,
    ValidationStatus,
)


class _AuthOnlyGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def authenticate(self) -> None:
        self.calls.append("authenticate")

    async def get_team(self, team_id: str) -> None:
        self.calls.append(f"team:{team_id}")

    async def list_projects(self, *, include_archived: bool) -> list[dict[str, str]]:
        self.calls.append(f"projects:{include_archived}")
        return []


@pytest.mark.asyncio
async def test_ones_probe_allows_auth_and_team_without_work_item_id() -> None:
    gateway = _AuthOnlyGateway()
    validator = SetupValidator._testing(ones_gateway=gateway)

    result = await validator.probe_ones(OnesProbeInput(team_id="TEAM"))

    assert result.step is SetupStep.ONES
    assert result.status is ValidationStatus.PASSED
    assert gateway.calls == ["authenticate", "team:TEAM", "projects:False"]


def test_empty_tui_ones_probe_fields_become_optional_values() -> None:
    controller = object.__new__(SetupController)

    probe = controller._normalize_ui_probe(
        SetupStep.ONES,
        MappingProxyType(
            {
                "ones-team-id": "TEAM",
                "ones-project-id": "",
                "ones-status-id": "",
                "ones-item-id": "",
                "ones-issue-type-id": "DEFECT",
            }
        ),
    )

    assert isinstance(probe, OnesProbeInput)
    assert probe.team_id == "TEAM"
    assert probe.project_id is None
    assert probe.status_id is None
    assert probe.item_id is None
    assert probe.issue_type_id == "DEFECT"
