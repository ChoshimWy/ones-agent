"""Codebase evidence access boundary built from repo resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from src.contracts import RepoResolution
from src.integrations.codebase import Codebase


EvidenceAvailability = Literal["available", "weak_resolution", "unresolved"]
EvidenceReadStatus = Literal["ok", "weak_resolution", "unresolved", "file_not_found"]


@dataclass(frozen=True, slots=True)
class EvidenceAccessState:
    status: EvidenceAvailability
    source: str
    rationale: str
    repo_url: str
    branch: str

    @property
    def available(self) -> bool:
        return self.status == "available"


@dataclass(frozen=True, slots=True)
class RepositoryTreeSummary:
    status: EvidenceReadStatus
    tree: str = ""
    max_depth: int = 3
    source: str = ""
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class KeywordFileCandidate:
    file_path: str
    preview: str


@dataclass(frozen=True, slots=True)
class KeywordFileCandidates:
    status: EvidenceReadStatus
    keywords: tuple[str, ...] = field(default_factory=tuple)
    candidates: tuple[KeywordFileCandidate, ...] = field(default_factory=tuple)
    source: str = ""
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class FileExcerpt:
    status: EvidenceReadStatus
    file_path: str
    start_line: int | None = None
    end_line: int | None = None
    content: str = ""
    source: str = ""
    rationale: str = ""


@dataclass(slots=True)
class ResolvedCodebaseEvidence:
    resolution: RepoResolution
    state: EvidenceAccessState
    _codebase: Codebase | None = field(default=None, repr=False)

    def repository_tree_summary(self, *, max_depth: int = 3) -> RepositoryTreeSummary:
        blocked = self._blocked_tree_summary(max_depth=max_depth)
        if blocked is not None:
            return blocked

        return RepositoryTreeSummary(
            status="ok",
            tree=self._codebase.tree(max_depth=max_depth),
            max_depth=max_depth,
            source=self.state.source,
            rationale=self.state.rationale,
        )

    def keyword_file_candidates(self, keywords: list[str], *, max_files: int = 10) -> KeywordFileCandidates:
        blocked = self._blocked_candidates(keywords=keywords)
        if blocked is not None:
            return blocked

        results = self._codebase.search_keywords(keywords, max_files=max_files)
        candidates = tuple(
            KeywordFileCandidate(file_path=file_path, preview=content[:800])
            for file_path, content in sorted(results.items())
        )
        return KeywordFileCandidates(
            status="ok",
            keywords=tuple(keywords),
            candidates=candidates,
            source=self.state.source,
            rationale=self.state.rationale,
        )

    def file_excerpt(self, file_path: str, *, start_line: int = 1, end_line: int = 40) -> FileExcerpt:
        blocked = self._blocked_excerpt(file_path=file_path, start_line=start_line, end_line=end_line)
        if blocked is not None:
            return blocked

        content = self._codebase.read_file(file_path)
        if content is None:
            return FileExcerpt(
                status="file_not_found",
                file_path=file_path,
                source=self.state.source,
                rationale=f"File '{file_path}' could not be read from the resolved repository.",
            )

        lines = content.splitlines()
        normalized_start = max(start_line, 1)
        normalized_end = max(end_line, normalized_start)
        selected = lines[normalized_start - 1:normalized_end]
        excerpt = "\n".join(selected)
        if selected and content.endswith("\n"):
            excerpt += "\n"
        return FileExcerpt(
            status="ok",
            file_path=file_path,
            start_line=normalized_start,
            end_line=min(normalized_end, len(lines)) if lines else normalized_start,
            content=excerpt,
            source=self.state.source,
            rationale=self.state.rationale,
        )

    def _blocked_tree_summary(self, *, max_depth: int) -> RepositoryTreeSummary | None:
        if self.state.available:
            return None
        return RepositoryTreeSummary(
            status=self.state.status,
            max_depth=max_depth,
            source=self.state.source,
            rationale=self.state.rationale,
        )

    def _blocked_candidates(self, *, keywords: list[str]) -> KeywordFileCandidates | None:
        if self.state.available:
            return None
        return KeywordFileCandidates(
            status=self.state.status,
            keywords=tuple(keywords),
            source=self.state.source,
            rationale=self.state.rationale,
        )

    def _blocked_excerpt(self, *, file_path: str, start_line: int, end_line: int) -> FileExcerpt | None:
        if self.state.available:
            return None
        normalized_start = max(start_line, 1)
        normalized_end = max(end_line, normalized_start)
        return FileExcerpt(
            status=self.state.status,
            file_path=file_path,
            start_line=normalized_start,
            end_line=normalized_end,
            source=self.state.source,
            rationale=self.state.rationale,
        )


@dataclass(slots=True)
class CodebaseEvidenceService:
    """Build a single-repo evidence boundary from canonical repo resolution."""

    minimum_confidence: float = 1.0

    def from_resolution(self, resolution: RepoResolution) -> ResolvedCodebaseEvidence:
        state = self._state_for_resolution(resolution)
        codebase = self._codebase_for_resolution(resolution) if state.available else None
        return ResolvedCodebaseEvidence(resolution=resolution, state=state, _codebase=codebase)

    def _state_for_resolution(self, resolution: RepoResolution) -> EvidenceAccessState:
        repo_url = resolution.selected_repo.repo_url.strip()
        branch = (resolution.selected_branch or resolution.selected_repo.default_branch or "main").strip() or "main"

        if resolution.confidence < self.minimum_confidence:
            return EvidenceAccessState(
                status="weak_resolution" if repo_url else "unresolved",
                source=resolution.source,
                rationale=(
                    resolution.rationale
                    or "Repository resolution confidence is below the minimum required for evidence access."
                ),
                repo_url=repo_url,
                branch=branch,
            )

        if not repo_url:
            return EvidenceAccessState(
                status="unresolved",
                source=resolution.source,
                rationale=resolution.rationale or "Repository resolution did not include a repository target.",
                repo_url=repo_url,
                branch=branch,
            )

        return EvidenceAccessState(
            status="available",
            source=resolution.source,
            rationale=resolution.rationale,
            repo_url=repo_url,
            branch=branch,
        )

    def _codebase_for_resolution(self, resolution: RepoResolution) -> Codebase:
        repo_url = resolution.selected_repo.repo_url.strip()
        branch = (resolution.selected_branch or resolution.selected_repo.default_branch or "main").strip() or "main"
        local_path = Path(repo_url)
        if local_path.exists():
            return Codebase(path=str(local_path))
        return Codebase(repo_url=repo_url, branch=branch)


__all__ = [
    "CodebaseEvidenceService",
    "EvidenceAccessState",
    "FileExcerpt",
    "KeywordFileCandidate",
    "KeywordFileCandidates",
    "RepositoryTreeSummary",
    "ResolvedCodebaseEvidence",
]
