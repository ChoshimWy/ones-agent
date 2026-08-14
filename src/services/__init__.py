"""Backend service boundaries for the ONES agent."""

from importlib import import_module
from threading import RLock
from typing import Any

__all__ = [
    "AnalysisResultShaper",
    "CodebaseEvidenceService",
    "DefectAnalysisWorkflowService",
    "ExecutionService",
    "OnesGateway",
    "RepoResolver",
]

_EXPORTS = {
    "AnalysisResultShaper": (
        "src.services.analysis_result_shaper",
        "AnalysisResultShaper",
    ),
    "CodebaseEvidenceService": (
        "src.services.codebase_evidence",
        "CodebaseEvidenceService",
    ),
    "DefectAnalysisWorkflowService": (
        "src.services.defect_analysis_workflow",
        "DefectAnalysisWorkflowService",
    ),
    "ExecutionService": ("src.services.execution_service", "ExecutionService"),
    "OnesGateway": ("src.services.ones_gateway", "OnesGateway"),
    "RepoResolver": ("src.services.repo_resolver", "RepoResolver"),
}
_EXPORT_LOCK = RLock()


def __getattr__(name: str) -> Any:
    export = _EXPORTS.get(name)
    if export is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None

    with _EXPORT_LOCK:
        if name in globals():
            return globals()[name]
        module_name, attribute_name = export
        value = getattr(import_module(module_name), attribute_name)
        globals()[name] = value
        return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
