"""Canonical backend contracts for defect analysis workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class IdentityRef:
    id: str = ""
    name: str = ""
    avatar: str = ""


@dataclass(slots=True)
class ProjectRef:
    id: str = ""
    name: str = ""


@dataclass(slots=True)
class StatusRef:
    id: str = ""
    name: str = ""
    category: str = ""


@dataclass(slots=True)
class IssueTypeRef:
    id: str = ""
    name: str = ""


@dataclass(slots=True)
class PriorityRef:
    id: str = ""
    value: str = ""
    position: int | None = None


@dataclass(slots=True)
class RepoTarget:
    repo_url: str = ""
    repo_name: str = ""
    default_branch: str = "main"


@dataclass(slots=True)
class RepoCandidate:
    repo: RepoTarget = field(default_factory=RepoTarget)
    branch: str = "main"
    source: str = ""
    confidence: float = 0.0
    rationale: str = ""


@dataclass(slots=True)
class DefectRecord:
    defect_id: str = ""
    title: str = ""
    number: str = ""
    project: ProjectRef = field(default_factory=ProjectRef)
    status: StatusRef = field(default_factory=StatusRef)
    issue_type: IssueTypeRef = field(default_factory=IssueTypeRef)
    priority: PriorityRef = field(default_factory=PriorityRef)
    assignee: IdentityRef | None = None
    owner: IdentityRef | None = None
    parent: IdentityRef | None = None
    path: str = ""
    description: str = ""
    deadline: str = ""
    created_at: str = ""
    updated_at: str = ""
    source: str = "ones"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RepoResolution:
    defect_id: str = ""
    project: ProjectRef = field(default_factory=ProjectRef)
    selected_repo: RepoTarget = field(default_factory=RepoTarget)
    selected_branch: str = "main"
    confidence: float = 0.0
    source: str = ""
    rationale: str = ""
    candidates: list[RepoCandidate] = field(default_factory=list)


@dataclass(slots=True)
class EvidenceReference:
    kind: str = "file"
    file_path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    snippet: str = ""
    description: str = ""
    source: str = ""


@dataclass(slots=True)
class FixSuggestion:
    title: str = ""
    description: str = ""
    impacted_files: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    risk_level: str = "medium"


@dataclass(slots=True)
class AnalysisResult:
    defect_id: str = ""
    project: ProjectRef = field(default_factory=ProjectRef)
    repo_resolution: RepoResolution | None = None
    analysis_summary: str = ""
    root_cause: str = ""
    evidence: list[EvidenceReference] = field(default_factory=list)
    confidence: float = 0.0
    impacted_files: list[str] = field(default_factory=list)
    fix_suggestions: list[FixSuggestion] = field(default_factory=list)
    insufficient_evidence: bool = False
    rendered_markdown: str = ""


@dataclass(slots=True)
class ExecutionRequest:
    defect_id: str = ""
    project: ProjectRef = field(default_factory=ProjectRef)
    repo_resolution: RepoResolution | None = None
    request_type: Literal["bugfix", "requirement_development"] = "bugfix"
    proposed_branch_name: str = ""
    target_branch: str = "main"
    requested_by: str = ""
    reason: str = ""
    confidence: float = 0.0
    source: str = "analysis"
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "AnalysisResult",
    "DefectRecord",
    "EvidenceReference",
    "ExecutionRequest",
    "FixSuggestion",
    "IdentityRef",
    "IssueTypeRef",
    "PriorityRef",
    "ProjectRef",
    "RepoCandidate",
    "RepoResolution",
    "RepoTarget",
    "StatusRef",
]
