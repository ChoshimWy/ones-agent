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
from .run_index import RunIndex
from .supervisor import (
    RunTaskSupervisor,
    SupervisorClosedError,
    SupervisorLoopError,
    TaskEvent,
)


def run_tui(controller: TuiController, max_concurrency: int) -> None:
    """Run the full-screen terminal application for an assembled controller."""

    DeveloperWorkflowTuiApp(controller, max_concurrency).run()

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
    "RunIndex",
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
    "run_tui",
    "safe_tui_text",
]
