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
from .runtime_session import TuiRuntimeCloseError, TuiRuntimeSession
from .setup_screens import SetupRootScreen
from .supervisor import (
    RunTaskSupervisor,
    SupervisorClosedError,
    SupervisorLoopError,
    TaskEvent,
)


def run_tui(setup_controller: object, runtime_bootstrapper: object) -> None:
    """Run the two-stage terminal host without pre-constructing workflow services."""

    if type(runtime_bootstrapper) is int:
        DeveloperWorkflowTuiApp(
            setup_controller, runtime_bootstrapper  # type: ignore[arg-type]
        ).run()
        return
    DeveloperWorkflowTuiApp(
        setup_controller=setup_controller,
        runtime_bootstrapper=runtime_bootstrapper,
    ).run()

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
    "SetupRootScreen",
    "SupervisorClosedError",
    "SupervisorLoopError",
    "TestView",
    "StaleTuiActionError",
    "TuiController",
    "TuiControllerError",
    "TuiDisplayError",
    "TaskEvent",
    "TuiRuntimeCloseError",
    "TuiRuntimeSession",
    "run_detail_from_run",
    "run_tui",
    "safe_tui_text",
]
