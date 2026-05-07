"""Project-to-repository resolution service."""

from __future__ import annotations

from dataclasses import dataclass

from src.contracts import DefectRecord, ProjectRef, RepoCandidate, RepoResolution, RepoTarget


@dataclass(slots=True)
class RepoResolver:
    """Resolve a normalized defect/project context to a repository target."""

    engine: object
    default_branch: str = "main"

    def resolve(
        self,
        *,
        defect: DefectRecord | None = None,
        project: ProjectRef | None = None,
    ) -> RepoResolution:
        target_project = defect.project if defect is not None else project or ProjectRef()
        defect_id = defect.defect_id if defect is not None else ""

        if not target_project.id:
            return RepoResolution(
                defect_id=defect_id,
                project=target_project,
                selected_repo=RepoTarget(default_branch=self.default_branch),
                selected_branch="",
                confidence=0.0,
                source="project_context_missing",
                rationale="Cannot resolve repository without a normalized project id.",
                candidates=[],
            )

        mapping = self.engine.get_repo_for_project(target_project.id)
        if not mapping:
            return RepoResolution(
                defect_id=defect_id,
                project=target_project,
                selected_repo=RepoTarget(default_branch=self.default_branch),
                selected_branch="",
                confidence=0.0,
                source="project_repo_mapping_missing",
                rationale=(
                    f"No repository mapping exists for project {target_project.id}; "
                    "analysis should stop until a project_repos mapping is configured."
                ),
                candidates=[],
            )

        repo_url = str(mapping.get("repoUrl", "") or "")
        branch = str(mapping.get("branch", "") or "").strip() or self.default_branch
        repo = RepoTarget(
            repo_url=repo_url,
            repo_name=self._repo_name_from_url(repo_url),
            default_branch=branch,
        )
        candidate = RepoCandidate(
            repo=repo,
            branch=branch,
            source="project_repo_mapping",
            confidence=1.0,
            rationale=(
                f"Resolved project {target_project.id} via Engine project_repos mapping"
                f" to {repo_url or 'an empty repo URL'} on branch {branch}."
            ),
        )
        return RepoResolution(
            defect_id=defect_id,
            project=target_project,
            selected_repo=repo,
            selected_branch=branch,
            confidence=1.0,
            source="project_repo_mapping",
            rationale=candidate.rationale,
            candidates=[candidate],
        )

    @staticmethod
    def _repo_name_from_url(repo_url: str) -> str:
        normalized = repo_url.rstrip("/")
        if not normalized:
            return ""
        return normalized.split("/")[-1].removesuffix(".git")
