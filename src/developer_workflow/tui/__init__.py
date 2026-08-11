"""Stable display-model API for the developer workflow terminal UI."""

from .app import DeveloperWorkflowTuiApp
from .controller import (
    CandidateSessionView,
    StaleTuiActionError,
    TuiController,
    TuiControllerError,
)

from .models import (
    DangerousActionRequest,
    DefectChoice,
    HistoryView,
    PublicationView,
    RepositoryView,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
    TestView,
    TuiDisplayError,
    run_detail_from_run,
    safe_tui_text,
)
from .supervisor import (
    RunTaskSupervisor,
    SupervisorClosedError,
    SupervisorLoopError,
    TaskEvent,
)

__all__ = [
    "CandidateSessionView",
    "DangerousActionRequest",
    "DeveloperWorkflowTuiApp",
    "DefectChoice",
    "HistoryView",
    "PublicationView",
    "RepositoryView",
    "RunActivity",
    "RunDetail",
    "RunFilter",
    "RunSummary",
    "RunTaskSupervisor",
    "SupervisorClosedError",
    "SupervisorLoopError",
    "TestView",
    "StaleTuiActionError",
    "TuiController",
    "TuiControllerError",
    "TuiDisplayError",
    "TaskEvent",
    "run_detail_from_run",
    "safe_tui_text",
]
