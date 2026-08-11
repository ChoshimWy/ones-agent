"""Stable display-model API for the developer workflow terminal UI."""

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

__all__ = [
    "CandidateSessionView",
    "DangerousActionRequest",
    "DefectChoice",
    "HistoryView",
    "PublicationView",
    "RepositoryView",
    "RunActivity",
    "RunDetail",
    "RunFilter",
    "RunSummary",
    "TestView",
    "StaleTuiActionError",
    "TuiController",
    "TuiControllerError",
    "TuiDisplayError",
    "run_detail_from_run",
    "safe_tui_text",
]
