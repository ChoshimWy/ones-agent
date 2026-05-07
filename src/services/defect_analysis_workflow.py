"""Dedicated staged defect analysis workflow boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from src.contracts import AnalysisResult, DefectRecord, EvidenceReference, FixSuggestion, ProjectRef, RepoResolution
from src.services.codebase_evidence import (
    CodebaseEvidenceService,
    FileExcerpt,
    KeywordFileCandidates,
    RepositoryTreeSummary,
)
from src.services.analysis_result_shaper import AnalysisResultShaper

StageStatus = str

_STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "into",
    "have",
    "has",
    "was",
    "were",
    "when",
    "then",
    "than",
    "bug",
    "defect",
    "issue",
    "异常",
    "缺陷",
    "问题",
}


@dataclass(frozen=True, slots=True)
class DefectUnderstandingStage:
    status: StageStatus
    summary: str = ""
    keywords: tuple[str, ...] = field(default_factory=tuple)
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceCollectionStage:
    status: StageStatus
    summary: str = ""
    tree_summary: RepositoryTreeSummary | None = None
    keyword_candidates: KeywordFileCandidates | None = None
    file_excerpts: tuple[FileExcerpt, ...] = field(default_factory=tuple)
    evidence: tuple[EvidenceReference, ...] = field(default_factory=tuple)
    blocked_reason: str = ""


@dataclass(frozen=True, slots=True)
class RootCauseDraft:
    summary: str = ""
    root_cause: str = ""
    impacted_files: tuple[str, ...] = field(default_factory=tuple)
    confidence: float = 0.0
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class RootCauseHypothesisStage:
    status: StageStatus
    draft: RootCauseDraft = field(default_factory=RootCauseDraft)
    blocked_reason: str = ""


@dataclass(frozen=True, slots=True)
class FixSuggestionDraft:
    suggestions: tuple[FixSuggestion, ...] = field(default_factory=tuple)
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class FixSuggestionStage:
    status: StageStatus
    draft: FixSuggestionDraft = field(default_factory=FixSuggestionDraft)
    blocked_reason: str = ""


@dataclass(frozen=True, slots=True)
class DefectAnalysisWorkflowResult:
    defect_id: str
    project: ProjectRef
    repo_resolution: RepoResolution
    defect_understanding: DefectUnderstandingStage
    evidence_collection: EvidenceCollectionStage
    root_cause_hypothesis: RootCauseHypothesisStage
    fix_suggestion_generation: FixSuggestionStage
    analysis_summary: str = ""
    confidence: float = 0.0
    insufficient_evidence: bool = True
    blocked_reason: str = ""
    rendered_markdown: str = ""

    @property
    def evidence(self) -> tuple[EvidenceReference, ...]:
        return self.evidence_collection.evidence

    @property
    def root_cause(self) -> str:
        return self.root_cause_hypothesis.draft.root_cause

    @property
    def impacted_files(self) -> tuple[str, ...]:
        return self.root_cause_hypothesis.draft.impacted_files

    @property
    def fix_suggestions(self) -> tuple[FixSuggestion, ...]:
        return self.fix_suggestion_generation.draft.suggestions


class AnalysisStageAnalyzer(Protocol):
    """Optional adapter for structured root-cause and fix generation."""

    def generate_root_cause(
        self,
        *,
        defect: DefectRecord,
        repo_resolution: RepoResolution,
        understanding: DefectUnderstandingStage,
        evidence_collection: EvidenceCollectionStage,
    ) -> RootCauseDraft: ...

    def generate_fix_suggestions(
        self,
        *,
        defect: DefectRecord,
        repo_resolution: RepoResolution,
        understanding: DefectUnderstandingStage,
        evidence_collection: EvidenceCollectionStage,
        root_cause: RootCauseDraft,
    ) -> FixSuggestionDraft: ...


@dataclass(slots=True)
class DefectAnalysisWorkflowService:
    """Run explicit analysis stages without coupling to execution flows."""

    evidence_service: CodebaseEvidenceService = field(default_factory=CodebaseEvidenceService)
    analyzer: AnalysisStageAnalyzer | None = None
    result_shaper: AnalysisResultShaper = field(default_factory=AnalysisResultShaper)
    tree_max_depth: int = 3
    max_candidate_files: int = 5
    excerpt_end_line: int = 80

    def analyze(self, defect: DefectRecord, repo_resolution: RepoResolution) -> DefectAnalysisWorkflowResult:
        understanding = self._understand_defect(defect)
        evidence_collection = self._collect_evidence(defect, repo_resolution, understanding)

        if evidence_collection.status == "blocked":
            return self._blocked_result(
                defect=defect,
                repo_resolution=repo_resolution,
                understanding=understanding,
                evidence_collection=evidence_collection,
                blocked_reason=evidence_collection.blocked_reason,
                confidence=min(repo_resolution.confidence, 0.49),
            )

        root_cause_hypothesis = self._generate_root_cause(
            defect=defect,
            repo_resolution=repo_resolution,
            understanding=understanding,
            evidence_collection=evidence_collection,
        )
        if root_cause_hypothesis.status == "blocked":
            return self._blocked_result(
                defect=defect,
                repo_resolution=repo_resolution,
                understanding=understanding,
                evidence_collection=evidence_collection,
                root_cause_hypothesis=root_cause_hypothesis,
                blocked_reason=root_cause_hypothesis.blocked_reason,
                confidence=min(root_cause_hypothesis.draft.confidence, 0.49),
            )

        fix_suggestion_generation = self._generate_fix_suggestions(
            defect=defect,
            repo_resolution=repo_resolution,
            understanding=understanding,
            evidence_collection=evidence_collection,
            root_cause_hypothesis=root_cause_hypothesis,
        )
        if fix_suggestion_generation.status == "blocked":
            return self._blocked_result(
                defect=defect,
                repo_resolution=repo_resolution,
                understanding=understanding,
                evidence_collection=evidence_collection,
                root_cause_hypothesis=root_cause_hypothesis,
                fix_suggestion_generation=fix_suggestion_generation,
                blocked_reason=fix_suggestion_generation.blocked_reason,
                confidence=min(root_cause_hypothesis.draft.confidence, 0.49),
            )

        summary = root_cause_hypothesis.draft.summary or understanding.summary
        confidence = self._normalized_confidence(root_cause_hypothesis.draft.confidence)
        rendered_markdown = self._render_markdown(
            summary=summary,
            root_cause=root_cause_hypothesis.draft.root_cause,
            evidence=evidence_collection.evidence,
            impacted_files=root_cause_hypothesis.draft.impacted_files,
            fix_suggestions=fix_suggestion_generation.draft.suggestions,
            blocked_reason="",
            insufficient_evidence=False,
        )
        return DefectAnalysisWorkflowResult(
            defect_id=defect.defect_id,
            project=defect.project,
            repo_resolution=repo_resolution,
            defect_understanding=understanding,
            evidence_collection=evidence_collection,
            root_cause_hypothesis=root_cause_hypothesis,
            fix_suggestion_generation=fix_suggestion_generation,
            analysis_summary=summary,
            confidence=confidence,
            insufficient_evidence=False,
            blocked_reason="",
            rendered_markdown=rendered_markdown,
        )

    def analyze_result(self, defect: DefectRecord, repo_resolution: RepoResolution) -> AnalysisResult:
        workflow_result = self.analyze(defect, repo_resolution)
        return self.result_shaper.from_workflow_result(workflow_result)

    def _understand_defect(self, defect: DefectRecord) -> DefectUnderstandingStage:
        keywords = self._extract_keywords(defect)
        summary_parts = [part for part in [defect.title.strip(), defect.description.strip()] if part]
        summary = ". ".join(summary_parts) if summary_parts else f"Defect {defect.defect_id} requires further understanding."
        return DefectUnderstandingStage(
            status="completed",
            summary=summary,
            keywords=keywords,
            rationale="Derived summary and keywords from the canonical DefectRecord fields.",
        )

    def _collect_evidence(
        self,
        defect: DefectRecord,
        repo_resolution: RepoResolution,
        understanding: DefectUnderstandingStage,
    ) -> EvidenceCollectionStage:
        resolved_evidence = self.evidence_service.from_resolution(repo_resolution)
        evidence: list[EvidenceReference] = [
            EvidenceReference(
                kind="defect",
                description=self._defect_evidence_description(defect),
                snippet=defect.description.strip(),
                source=defect.source,
            ),
            EvidenceReference(
                kind="repo_resolution",
                description=repo_resolution.rationale or "Repository resolution rationale was not provided.",
                snippet=repo_resolution.selected_repo.repo_url,
                source=repo_resolution.source,
            ),
        ]

        if not resolved_evidence.state.available:
            return EvidenceCollectionStage(
                status="blocked",
                summary="Evidence collection blocked before code inspection.",
                evidence=tuple(evidence),
                blocked_reason=resolved_evidence.state.rationale,
            )

        tree_summary = resolved_evidence.repository_tree_summary(max_depth=self.tree_max_depth)
        keyword_candidates = resolved_evidence.keyword_file_candidates(
            list(understanding.keywords),
            max_files=self.max_candidate_files,
        )
        file_excerpts: list[FileExcerpt] = []
        impacted_files: list[str] = []

        for candidate in keyword_candidates.candidates:
            excerpt = resolved_evidence.file_excerpt(candidate.file_path, start_line=1, end_line=self.excerpt_end_line)
            file_excerpts.append(excerpt)
            if excerpt.status != "ok" or not excerpt.content.strip():
                continue
            impacted_files.append(excerpt.file_path)
            evidence.append(
                EvidenceReference(
                    kind="file",
                    file_path=excerpt.file_path,
                    start_line=excerpt.start_line,
                    end_line=excerpt.end_line,
                    snippet=excerpt.content,
                    description="Keyword-matched repository excerpt collected for staged analysis.",
                    source=excerpt.source or keyword_candidates.source,
                )
            )

        if tree_summary.status == "ok" and tree_summary.tree.strip():
            evidence.append(
                EvidenceReference(
                    kind="tree_summary",
                    description="Repository tree summary used to scope the analysis.",
                    snippet=tree_summary.tree,
                    source=tree_summary.source,
                )
            )

        if not impacted_files:
            return EvidenceCollectionStage(
                status="blocked",
                summary="Evidence collection completed without file-level proof.",
                tree_summary=tree_summary,
                keyword_candidates=keyword_candidates,
                file_excerpts=tuple(file_excerpts),
                evidence=tuple(evidence),
                blocked_reason="The resolved repository did not yield any file excerpts that support a code-level hypothesis.",
            )

        return EvidenceCollectionStage(
            status="completed",
            summary=f"Collected {len(evidence)} evidence item(s) across {len(set(impacted_files))} file(s).",
            tree_summary=tree_summary,
            keyword_candidates=keyword_candidates,
            file_excerpts=tuple(file_excerpts),
            evidence=tuple(evidence),
        )

    def _generate_root_cause(
        self,
        *,
        defect: DefectRecord,
        repo_resolution: RepoResolution,
        understanding: DefectUnderstandingStage,
        evidence_collection: EvidenceCollectionStage,
    ) -> RootCauseHypothesisStage:
        file_paths = tuple(
            ref.file_path
            for ref in evidence_collection.evidence
            if ref.kind == "file" and ref.file_path
        )
        if not file_paths:
            return RootCauseHypothesisStage(
                status="blocked",
                draft=RootCauseDraft(confidence=0.0),
                blocked_reason="Root-cause analysis requires at least one repository-backed file excerpt.",
            )

        if self.analyzer is None:
            return RootCauseHypothesisStage(
                status="blocked",
                draft=RootCauseDraft(
                    summary=understanding.summary,
                    impacted_files=file_paths,
                    confidence=0.35,
                    rationale="No structured analyzer adapter was configured for root-cause generation.",
                ),
                blocked_reason="No structured analyzer adapter was configured for root-cause generation.",
            )

        draft = self.analyzer.generate_root_cause(
            defect=defect,
            repo_resolution=repo_resolution,
            understanding=understanding,
            evidence_collection=evidence_collection,
        )
        normalized = RootCauseDraft(
            summary=draft.summary or understanding.summary,
            root_cause=draft.root_cause.strip(),
            impacted_files=self._dedupe_paths(draft.impacted_files or file_paths),
            confidence=self._normalized_confidence(draft.confidence),
            rationale=draft.rationale,
        )
        if not normalized.root_cause or normalized.confidence < 0.5:
            return RootCauseHypothesisStage(
                status="blocked",
                draft=normalized,
                blocked_reason="Root-cause generation did not produce a confident, repository-backed hypothesis.",
            )
        return RootCauseHypothesisStage(status="completed", draft=normalized)

    def _generate_fix_suggestions(
        self,
        *,
        defect: DefectRecord,
        repo_resolution: RepoResolution,
        understanding: DefectUnderstandingStage,
        evidence_collection: EvidenceCollectionStage,
        root_cause_hypothesis: RootCauseHypothesisStage,
    ) -> FixSuggestionStage:
        if self.analyzer is None:
            return FixSuggestionStage(
                status="blocked",
                blocked_reason="Fix suggestion generation requires the structured analyzer adapter.",
            )

        draft = self.analyzer.generate_fix_suggestions(
            defect=defect,
            repo_resolution=repo_resolution,
            understanding=understanding,
            evidence_collection=evidence_collection,
            root_cause=root_cause_hypothesis.draft,
        )
        suggestions = tuple(self._normalized_fix_suggestion(item) for item in draft.suggestions if self._valid_fix_suggestion(item))
        if not suggestions:
            return FixSuggestionStage(
                status="blocked",
                draft=FixSuggestionDraft(rationale=draft.rationale),
                blocked_reason="Fix suggestion generation did not produce actionable file-backed suggestions.",
            )
        return FixSuggestionStage(status="completed", draft=FixSuggestionDraft(suggestions=suggestions, rationale=draft.rationale))

    def _blocked_result(
        self,
        *,
        defect: DefectRecord,
        repo_resolution: RepoResolution,
        understanding: DefectUnderstandingStage,
        evidence_collection: EvidenceCollectionStage,
        blocked_reason: str,
        confidence: float,
        root_cause_hypothesis: RootCauseHypothesisStage | None = None,
        fix_suggestion_generation: FixSuggestionStage | None = None,
    ) -> DefectAnalysisWorkflowResult:
        root_stage = root_cause_hypothesis or RootCauseHypothesisStage(
            status="blocked",
            blocked_reason=blocked_reason,
        )
        fix_stage = fix_suggestion_generation or FixSuggestionStage(
            status="blocked",
            blocked_reason=blocked_reason,
        )
        summary = self._blocked_summary(understanding.summary, blocked_reason)
        rendered_markdown = self._render_markdown(
            summary=summary,
            root_cause=root_stage.draft.root_cause,
            evidence=evidence_collection.evidence,
            impacted_files=root_stage.draft.impacted_files,
            fix_suggestions=fix_stage.draft.suggestions,
            blocked_reason=blocked_reason,
            insufficient_evidence=True,
        )
        return DefectAnalysisWorkflowResult(
            defect_id=defect.defect_id,
            project=defect.project,
            repo_resolution=repo_resolution,
            defect_understanding=understanding,
            evidence_collection=evidence_collection,
            root_cause_hypothesis=root_stage,
            fix_suggestion_generation=fix_stage,
            analysis_summary=summary,
            confidence=self._normalized_confidence(min(confidence, 0.49)),
            insufficient_evidence=True,
            blocked_reason=blocked_reason,
            rendered_markdown=rendered_markdown,
        )

    @staticmethod
    def _extract_keywords(defect: DefectRecord) -> tuple[str, ...]:
        source_text = " ".join(
            part
            for part in [
                defect.title,
                defect.description,
                defect.issue_type.name,
                defect.priority.value,
                defect.project.name,
            ]
            if part
        )
        normalized = (
            source_text.lower()
            .replace("/", " ")
            .replace("-", " ")
            .replace("_", " ")
            .replace("\n", " ")
        )
        words: list[str] = []
        for token in normalized.split():
            clean = token.strip(".,:;()[]{}'\"`?!")
            if len(clean) < 3 or clean in _STOP_WORDS or clean.isdigit():
                continue
            if clean not in words:
                words.append(clean)
        return tuple(words[:8])

    @staticmethod
    def _defect_evidence_description(defect: DefectRecord) -> str:
        if defect.description.strip():
            return f"Defect record '{defect.title or defect.defect_id}' provides the primary problem statement."
        return f"Defect metadata for '{defect.title or defect.defect_id}' is available, but detailed description text is missing."

    @staticmethod
    def _normalized_confidence(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @staticmethod
    def _dedupe_paths(paths: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        seen: list[str] = []
        for path in paths:
            if path and path not in seen:
                seen.append(path)
        return tuple(seen)

    @staticmethod
    def _valid_fix_suggestion(suggestion: FixSuggestion) -> bool:
        return bool(
            suggestion.title.strip()
            and suggestion.description.strip()
            and suggestion.impacted_files
            and suggestion.steps
            and suggestion.risk_level in {"low", "medium", "high"}
        )

    @staticmethod
    def _normalized_fix_suggestion(suggestion: FixSuggestion) -> FixSuggestion:
        return FixSuggestion(
            title=suggestion.title.strip(),
            description=suggestion.description.strip(),
            impacted_files=[path for path in suggestion.impacted_files if path],
            steps=[step.strip() for step in suggestion.steps if step.strip()],
            risk_level=suggestion.risk_level,
        )

    @staticmethod
    def _blocked_summary(summary: str, reason: str) -> str:
        prefix = summary.strip() or "Defect analysis is blocked."
        return f"{prefix} Analysis is blocked due to insufficient evidence: {reason}".strip()

    @staticmethod
    def _render_markdown(
        *,
        summary: str,
        root_cause: str,
        evidence: tuple[EvidenceReference, ...],
        impacted_files: tuple[str, ...],
        fix_suggestions: tuple[FixSuggestion, ...],
        blocked_reason: str,
        insufficient_evidence: bool,
    ) -> str:
        lines = ["## Analysis Summary", summary or "No analysis summary available."]
        if root_cause:
            lines.extend(["", "## Root Cause Hypothesis", root_cause])
        elif insufficient_evidence:
            lines.extend(["", "## Root Cause Hypothesis", "Root cause is unconfirmed due to insufficient evidence."])

        lines.extend(["", "## Evidence"])
        if evidence:
            for item in evidence:
                location = item.file_path or item.kind
                detail = item.description or item.source or "Evidence collected without extra description."
                lines.append(f"- {location}: {detail}")
        else:
            lines.append("- No evidence collected.")

        lines.extend(["", "## Impacted Files"])
        if impacted_files:
            lines.extend(f"- {path}" for path in impacted_files)
        else:
            lines.append("- No impacted files confirmed.")

        lines.extend(["", "## Fix Suggestions"])
        if fix_suggestions:
            for suggestion in fix_suggestions:
                lines.append(f"- {suggestion.title}: {suggestion.description}")
        elif insufficient_evidence:
            lines.append("- No executable fix suggestions because the analysis is blocked.")
        else:
            lines.append("- No fix suggestions were generated.")

        if blocked_reason:
            lines.extend(["", "## Blocked Reason", blocked_reason])
        return "\n".join(lines)


__all__ = [
    "AnalysisStageAnalyzer",
    "DefectAnalysisWorkflowResult",
    "DefectAnalysisWorkflowService",
    "DefectUnderstandingStage",
    "EvidenceCollectionStage",
    "FixSuggestionDraft",
    "FixSuggestionStage",
    "RootCauseDraft",
    "RootCauseHypothesisStage",
]
