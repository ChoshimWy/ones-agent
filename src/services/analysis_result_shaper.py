"""Canonical AnalysisResult shaping for staged defect analysis."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

from src.contracts import AnalysisResult, EvidenceReference, FixSuggestion

if TYPE_CHECKING:
    from src.services.defect_analysis_workflow import DefectAnalysisWorkflowResult


@dataclass(slots=True)
class AnalysisResultShaper:
    """Translate staged workflow output into the canonical analysis contract."""

    actionable_confidence_floor: float = 0.75
    blocked_confidence_ceiling: float = 0.49
    minimum_summary_length: int = 20
    minimum_root_cause_length: int = 20
    minimum_root_cause_token_matches: int = 2

    def from_workflow_result(self, workflow_result: DefectAnalysisWorkflowResult) -> AnalysisResult:
        evidence = self._normalized_evidence(workflow_result.evidence)
        summary = (workflow_result.analysis_summary or workflow_result.defect_understanding.summary).strip()
        root_cause = workflow_result.root_cause.strip()
        confidence = self._normalized_confidence(workflow_result.confidence)
        evidence_file_paths = {item.file_path for item in evidence if item.file_path}
        impacted_files = self._evidence_backed_paths(workflow_result.impacted_files, evidence_file_paths)
        fix_suggestions = self._normalized_fix_suggestions(workflow_result.fix_suggestions, evidence_file_paths)
        blocked_reasons = self._blocking_reasons(
            workflow_result=workflow_result,
            summary=summary,
            root_cause=root_cause,
            evidence=evidence,
            impacted_files=impacted_files,
            fix_suggestions=fix_suggestions,
            confidence=confidence,
        )

        if blocked_reasons:
            blocked_summary = self._blocked_summary(evidence=evidence, blocked_reasons=blocked_reasons)
            result = AnalysisResult(
                defect_id=workflow_result.defect_id,
                project=workflow_result.project,
                repo_resolution=workflow_result.repo_resolution,
                analysis_summary=blocked_summary,
                root_cause="",
                evidence=evidence,
                confidence=min(confidence, self.blocked_confidence_ceiling),
                impacted_files=impacted_files,
                fix_suggestions=[],
                insufficient_evidence=True,
            )
            result.rendered_markdown = self.render_markdown(result, blocked_reasons=blocked_reasons)
            return result

        result = AnalysisResult(
            defect_id=workflow_result.defect_id,
            project=workflow_result.project,
            repo_resolution=workflow_result.repo_resolution,
            analysis_summary=summary,
            root_cause=root_cause,
            evidence=evidence,
            confidence=confidence,
            impacted_files=impacted_files,
            fix_suggestions=fix_suggestions,
            insufficient_evidence=False,
        )
        result.rendered_markdown = self.render_markdown(result)
        return result

    def render_markdown(self, result: AnalysisResult, *, blocked_reasons: list[str] | None = None) -> str:
        lines = ["## Analysis Summary", result.analysis_summary or "No analysis summary available."]

        lines.extend(["", "## Root Cause Hypothesis"])
        if result.root_cause:
            lines.append(result.root_cause)
        elif result.insufficient_evidence:
            lines.append("Root cause is unconfirmed due to insufficient evidence.")
        else:
            lines.append("No root cause was provided.")

        lines.extend(["", "## Evidence"])
        if result.evidence:
            for item in result.evidence:
                lines.append(f"- {self._evidence_markdown_line(item)}")
        else:
            lines.append("- No evidence collected.")

        lines.extend(["", "## Impacted Files"])
        if result.impacted_files:
            lines.extend(f"- {path}" for path in result.impacted_files)
        else:
            lines.append("- No impacted files confirmed.")

        lines.extend(["", "## Fix Suggestions"])
        if result.fix_suggestions:
            for suggestion in result.fix_suggestions:
                lines.append(f"- {suggestion.title}: {suggestion.description}")
                lines.extend(f"  - {path}" for path in suggestion.impacted_files)
                lines.extend(f"  - {step}" for step in suggestion.steps)
        elif result.insufficient_evidence:
            lines.append("- No executable fix suggestions because the analysis is blocked.")
        else:
            lines.append("- No fix suggestions were generated.")

        lines.extend(["", "## Confidence", f"- Score: {result.confidence:.2f}"])
        lines.append(f"- Insufficient evidence: {'yes' if result.insufficient_evidence else 'no'}")

        if blocked_reasons:
            lines.extend(["", "## Blocked Reason"])
            lines.extend(f"- {reason}" for reason in blocked_reasons)

        return "\n".join(lines)

    def _blocking_reasons(
        self,
        *,
        workflow_result: DefectAnalysisWorkflowResult,
        summary: str,
        root_cause: str,
        evidence: list[EvidenceReference],
        impacted_files: list[str],
        fix_suggestions: list[FixSuggestion],
        confidence: float,
    ) -> list[str]:
        reasons: list[str] = []

        repo_resolution = workflow_result.repo_resolution
        if not workflow_result.defect_id.strip():
            reasons.append("Defect identifier is missing.")
        if not (workflow_result.project.id.strip() or workflow_result.project.name.strip()):
            reasons.append("Project context is missing.")
        if repo_resolution is None:
            reasons.append("Repository resolution is missing.")
        else:
            if not (repo_resolution.selected_repo.repo_url.strip() or repo_resolution.selected_repo.repo_name.strip()):
                reasons.append("Resolved repository target is incomplete.")
            if not repo_resolution.selected_branch.strip():
                reasons.append("Resolved repository branch is missing.")

        acceptable_evidence = [item for item in evidence if self._is_acceptable_evidence(item)]
        evidence_file_paths = {item.file_path for item in acceptable_evidence if item.file_path}
        support_points = self._support_points(acceptable_evidence)
        has_root_cause_support = self._has_root_cause_support(root_cause, acceptable_evidence)
        has_fix_overlap = any(set(suggestion.impacted_files) & set(impacted_files) for suggestion in fix_suggestions)

        if workflow_result.insufficient_evidence:
            reasons.append(workflow_result.blocked_reason or "The staged workflow reported insufficient evidence.")
        if len(summary.strip()) < self.minimum_summary_length:
            reasons.append("Analysis summary does not meet the minimum detail requirement.")
        if len(acceptable_evidence) < 2:
            reasons.append("Fewer than two acceptable evidence items were collected.")
        if not evidence_file_paths:
            reasons.append("No repository-backed file evidence was collected.")
        if len(support_points) < 2:
            reasons.append("Evidence does not provide enough distinct support points.")
        if len(root_cause) < self.minimum_root_cause_length:
            reasons.append("Root cause is unconfirmed or too short to support execution.")
        if root_cause and not has_root_cause_support:
            reasons.append("No evidence item directly supports the claimed root cause.")
        if not impacted_files:
            reasons.append("No evidence-backed impacted files were confirmed.")
        if not fix_suggestions:
            reasons.append("No actionable fix suggestions were confirmed.")
        elif not has_fix_overlap:
            reasons.append("Fix suggestions do not overlap with the confirmed impacted files.")
        if confidence < self.actionable_confidence_floor:
            reasons.append("Confidence is below the actionable threshold.")

        unique_reasons: list[str] = []
        for reason in reasons:
            cleaned = reason.strip()
            if cleaned and cleaned not in unique_reasons:
                unique_reasons.append(cleaned)
        return unique_reasons

    @staticmethod
    def _normalized_confidence(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @staticmethod
    def _normalized_evidence(evidence: tuple[EvidenceReference, ...]) -> list[EvidenceReference]:
        normalized: list[EvidenceReference] = []
        for item in evidence:
            normalized.append(
                EvidenceReference(
                    kind=item.kind.strip() or "context",
                    file_path=item.file_path.strip(),
                    start_line=item.start_line,
                    end_line=item.end_line,
                    snippet=item.snippet.strip(),
                    description=item.description.strip() or "Evidence collected during staged defect analysis.",
                    source=item.source.strip() or "workflow",
                )
            )
        return normalized

    @staticmethod
    def _evidence_backed_paths(paths: tuple[str, ...], evidence_file_paths: set[str]) -> list[str]:
        confirmed: list[str] = []
        for path in paths:
            cleaned = path.strip()
            if cleaned and cleaned in evidence_file_paths and cleaned not in confirmed:
                confirmed.append(cleaned)
        return confirmed

    @staticmethod
    def _normalized_fix_suggestions(
        suggestions: tuple[FixSuggestion, ...],
        evidence_file_paths: set[str],
    ) -> list[FixSuggestion]:
        normalized: list[FixSuggestion] = []
        for item in suggestions:
            impacted_files: list[str] = []
            for path in item.impacted_files:
                cleaned = path.strip()
                if cleaned and cleaned in evidence_file_paths and cleaned not in impacted_files:
                    impacted_files.append(cleaned)

            steps = [step.strip() for step in item.steps if step.strip()]
            suggestion = FixSuggestion(
                title=item.title.strip(),
                description=item.description.strip(),
                impacted_files=impacted_files,
                steps=steps,
                risk_level=item.risk_level,
            )
            if (
                suggestion.title
                and suggestion.description
                and suggestion.impacted_files
                and suggestion.steps
                and suggestion.risk_level in {"low", "medium", "high"}
            ):
                normalized.append(suggestion)
        return normalized

    @staticmethod
    def _is_acceptable_evidence(item: EvidenceReference) -> bool:
        if item.kind == "file":
            return bool(item.file_path and (item.snippet or (item.start_line is not None and item.end_line is not None)))
        if item.kind in {"defect", "repo_resolution", "tree_summary"}:
            return bool(item.description or item.source or item.snippet)
        return bool(item.file_path or item.description or item.source)

    @staticmethod
    def _support_points(evidence: list[EvidenceReference]) -> set[str]:
        support_points: set[str] = set()
        file_paths = {item.file_path for item in evidence if item.kind == "file" and item.file_path}

        if any(item.kind == "defect" for item in evidence):
            support_points.add("defect_metadata")
        if any(item.kind == "repo_resolution" for item in evidence):
            support_points.add("repo_resolution")
        if any(item.kind == "file" and item.file_path for item in evidence):
            support_points.add("code_observation")
        if len(file_paths) >= 2:
            support_points.add("cross_file_consistency")

        return support_points

    def _has_root_cause_support(self, root_cause: str, evidence: list[EvidenceReference]) -> bool:
        if not root_cause.strip():
            return False

        lowered_root_cause = root_cause.lower()
        root_tokens = self._meaningful_tokens(root_cause)
        for item in evidence:
            if item.kind != "file" or not item.file_path:
                continue
            evidence_text = " ".join(
                part.lower()
                for part in [item.file_path, item.description, item.snippet, item.source]
                if part
            )
            shared_tokens = {token for token in root_tokens if token in evidence_text}
            file_name = item.file_path.rsplit("/", 1)[-1].lower()
            path_referenced = item.file_path.lower() in lowered_root_cause or (file_name and file_name in lowered_root_cause)
            if path_referenced and len(shared_tokens) >= self.minimum_root_cause_token_matches:
                return True
        return False

    @staticmethod
    def _blocked_summary(*, evidence: list[EvidenceReference], blocked_reasons: list[str]) -> str:
        file_count = len({item.file_path for item in evidence if item.file_path})
        return (
            "Reviewed the staged defect context "
            f"and {len(evidence)} evidence item(s) across {file_count} repository-backed file(s), "
            "but could not confirm an actionable root cause. "
            f"Missing or blocked evidence: {'; '.join(blocked_reasons)}"
        )

    @staticmethod
    def _meaningful_tokens(text: str) -> set[str]:
        stop_words = {
            "the",
            "and",
            "for",
            "with",
            "that",
            "this",
            "after",
            "before",
            "from",
            "into",
            "without",
            "because",
            "still",
            "present",
            "missing",
            "root",
            "cause",
            "scope",
            "file",
            "path",
            "src",
            "def",
        }
        return {
            token
            for token in re.findall(r"[a-z0-9_./'-]+", text.lower())
            if len(token) >= 4 and token not in stop_words
        }

    @staticmethod
    def _evidence_markdown_line(item: EvidenceReference) -> str:
        location = item.file_path or item.kind
        line_range = ""
        if item.start_line is not None and item.end_line is not None:
            line_range = f":{item.start_line}-{item.end_line}"
        detail_parts = [item.description]
        if item.source:
            detail_parts.append(f"source={item.source}")
        if item.snippet:
            detail_parts.append(f"snippet={item.snippet[:120].replace(chr(10), ' ')}")
        return f"{location}{line_range} - {'; '.join(part for part in detail_parts if part)}"


__all__ = ["AnalysisResultShaper"]
