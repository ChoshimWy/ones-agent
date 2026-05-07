from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.contracts import AnalysisResult, DefectRecord, EvidenceReference, FixSuggestion, IssueTypeRef, PriorityRef, ProjectRef, RepoResolution, RepoTarget
from src.integrations.codebase import Codebase
from src.services.analysis_result_shaper import AnalysisResultShaper
from src.services.codebase_evidence import (
    EvidenceAccessState,
    FileExcerpt,
    KeywordFileCandidate,
    KeywordFileCandidates,
    RepositoryTreeSummary,
)
from src.services.defect_analysis_workflow import (
    DefectAnalysisWorkflowResult,
    DefectAnalysisWorkflowService,
    DefectUnderstandingStage,
    EvidenceCollectionStage,
    FixSuggestionDraft,
    FixSuggestionStage,
    RootCauseDraft,
    RootCauseHypothesisStage,
)


def _make_defect() -> DefectRecord:
    return DefectRecord(
        defect_id="defect-123",
        title="Login callback drops tenant context",
        description="Users are returned to the dashboard without the expected tenant scope after SSO login.",
        project=ProjectRef(id="proj-1", name="Auth Platform"),
        issue_type=IssueTypeRef(id="it-1", name="缺陷"),
        priority=PriorityRef(id="p1", value="high"),
    )


def _make_resolution(*, confidence: float = 1.0, repo_url: str = "E:/workspace/fake/auth-platform") -> RepoResolution:
    return RepoResolution(
        defect_id="defect-123",
        project=ProjectRef(id="proj-1", name="Auth Platform"),
        selected_repo=RepoTarget(
            repo_url=repo_url,
            repo_name="auth-platform",
            default_branch="main",
        ),
        selected_branch="main",
        confidence=confidence,
        source="project_repo_mapping",
        rationale="Resolved from the configured project-to-repo mapping.",
    )


class FakeResolvedEvidence:
    def __init__(self):
        self.state = EvidenceAccessState(
            status="available",
            source="project_repo_mapping",
            rationale="Repository resolution is stable and ready for evidence reads.",
            repo_url="E:/workspace/fake/auth-platform",
            branch="main",
        )

    def repository_tree_summary(self, *, max_depth: int = 3) -> RepositoryTreeSummary:
        return RepositoryTreeSummary(
            status="ok",
            tree="src/\n  auth/\n    callback.py\n  session.py",
            max_depth=max_depth,
            source="tree",
            rationale="Repository tree was read successfully.",
        )

    def keyword_file_candidates(self, keywords: list[str], *, max_files: int = 10) -> KeywordFileCandidates:
        return KeywordFileCandidates(
            status="ok",
            keywords=tuple(keywords),
            candidates=(
                KeywordFileCandidate(
                    file_path="src/auth/callback.py",
                    preview="def finish_login(request):\n    tenant_id = request.session.get('tenant_id')\n",
                ),
                KeywordFileCandidate(
                    file_path="src/session.py",
                    preview="def ensure_tenant_scope(request):\n    return request.state.tenant\n",
                ),
            ),
            source="keyword_search",
            rationale=f"Keyword search returned up to {max_files} candidate file(s).",
        )

    def file_excerpt(self, file_path: str, *, start_line: int = 1, end_line: int = 40) -> FileExcerpt:
        excerpt_by_file = {
            "src/auth/callback.py": (
                "def finish_login(request):\n"
                "    tenant_id = request.session.get('tenant_id')\n"
                "    request.state.tenant = tenant_id\n"
                "    return redirect('/dashboard')\n"
            ),
            "src/session.py": (
                "def ensure_tenant_scope(request):\n"
                "    if not getattr(request.state, 'tenant', None):\n"
                "        raise MissingTenantScope()\n"
                "    return request.state.tenant\n"
            ),
        }
        return FileExcerpt(
            status="ok",
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            content=excerpt_by_file[file_path],
            source="keyword_search",
            rationale="Collected the candidate login callback excerpt for workflow analysis.",
        )


class FakeEvidenceService:
    def __init__(self, resolved_evidence: FakeResolvedEvidence):
        self._resolved_evidence = resolved_evidence
        self.calls: list[RepoResolution] = []

    def from_resolution(self, resolution: RepoResolution) -> FakeResolvedEvidence:
        self.calls.append(resolution)
        return self._resolved_evidence


class FakeAnalyzer:
    def __init__(self):
        self.root_cause_calls: list[dict[str, object]] = []
        self.fix_calls: list[dict[str, object]] = []

    def generate_root_cause(self, **kwargs) -> RootCauseDraft:
        self.root_cause_calls.append(kwargs)
        return RootCauseDraft(
            summary="The login callback preserves a tenant id but never validates it before redirecting.",
            root_cause="`src/auth/callback.py` redirects after SSO login without validating that the tenant context is still present, so the dashboard opens without tenant scope.",
            impacted_files=("src/auth/callback.py", "src/session.py"),
            confidence=0.82,
            rationale="File excerpt shows the redirect path and tenant handoff without a guard.",
        )

    def generate_fix_suggestions(self, **kwargs) -> FixSuggestionDraft:
        self.fix_calls.append(kwargs)
        return FixSuggestionDraft(
            suggestions=(
                FixSuggestion(
                    title="Validate tenant context before redirect",
                    description="Add a guard that blocks the dashboard redirect when tenant state is missing after login.",
                    impacted_files=["src/auth/callback.py", "src/session.py"],
                    steps=[
                        "Check tenant_id before setting request.state.tenant.",
                        "Return the login flow to tenant selection when tenant context is missing.",
                    ],
                    risk_level="medium",
                ),
            ),
            rationale="The same callback and session handoff files need the fix.",
        )


class TestDefectAnalysisWorkflowService(unittest.TestCase):
    def test_analyze_runs_staged_workflow_with_mocked_evidence_and_analyzer(self):
        defect = _make_defect()
        resolution = _make_resolution()
        fake_evidence = FakeEvidenceService(FakeResolvedEvidence())
        fake_analyzer = FakeAnalyzer()
        workflow = DefectAnalysisWorkflowService(
            evidence_service=fake_evidence,
            analyzer=fake_analyzer,
        )

        result = workflow.analyze(defect, resolution)

        self.assertEqual(fake_evidence.calls, [resolution])
        self.assertEqual(result.project.id, "proj-1")
        self.assertEqual(result.defect_understanding.status, "completed")
        self.assertEqual(result.evidence_collection.status, "completed")
        self.assertEqual(result.root_cause_hypothesis.status, "completed")
        self.assertEqual(result.fix_suggestion_generation.status, "completed")
        self.assertFalse(result.insufficient_evidence)
        self.assertGreaterEqual(result.confidence, 0.75)
        self.assertEqual(result.impacted_files, ("src/auth/callback.py", "src/session.py"))
        self.assertEqual(result.fix_suggestions[0].title, "Validate tenant context before redirect")
        self.assertIn("tenant scope", result.root_cause)
        self.assertTrue(any(item.kind == "file" and item.file_path == "src/auth/callback.py" for item in result.evidence))
        self.assertEqual(len(fake_analyzer.root_cause_calls), 1)
        self.assertEqual(len(fake_analyzer.fix_calls), 1)

    def test_analyze_returns_blocked_semantics_for_weak_resolution(self):
        defect = _make_defect()
        resolution = _make_resolution(confidence=0.4)
        analyzer = MagicMock()
        analyzer.generate_root_cause = MagicMock()
        analyzer.generate_fix_suggestions = MagicMock()
        workflow = DefectAnalysisWorkflowService(analyzer=analyzer)

        result = workflow.analyze(defect, resolution)

        self.assertTrue(result.insufficient_evidence)
        self.assertEqual(result.evidence_collection.status, "blocked")
        self.assertEqual(result.root_cause_hypothesis.status, "blocked")
        self.assertEqual(result.fix_suggestion_generation.status, "blocked")
        self.assertLessEqual(result.confidence, 0.49)
        self.assertIn("insufficient evidence", result.analysis_summary)
        self.assertIn("Resolved from the configured project-to-repo mapping.", result.blocked_reason)
        self.assertEqual(result.fix_suggestions, ())
        analyzer.generate_root_cause.assert_not_called()
        analyzer.generate_fix_suggestions.assert_not_called()

    def test_analyze_does_not_invoke_clone_or_branch_execution_code(self):
        defect = _make_defect()
        resolution = _make_resolution()
        workflow = DefectAnalysisWorkflowService(
            evidence_service=FakeEvidenceService(FakeResolvedEvidence()),
            analyzer=FakeAnalyzer(),
        )

        with patch.object(Codebase, "_clone", side_effect=AssertionError("clone should not run during analysis")) as clone_mock, \
             patch("src.integrations.codebase.subprocess.run", side_effect=AssertionError("git subprocess should not run during analysis")) as subprocess_mock, \
             patch("src.integrations.git_ops.GitOps.checkout_branch", side_effect=AssertionError("branch creation should not run during analysis")) as checkout_mock, \
             patch("src.integrations.git_ops.GitOps.clone_repo", side_effect=AssertionError("repo clone should not run during analysis")) as clone_repo_mock, \
             patch("src.integrations.git_ops.GitOps.commit_changes", side_effect=AssertionError("commit should not run during analysis")) as commit_mock, \
             patch("src.integrations.git_ops.GitOps.push_branch", side_effect=AssertionError("push should not run during analysis")) as push_mock, \
             patch("src.integrations.git_ops.GitOps.create_pr", side_effect=AssertionError("pr creation should not run during analysis")) as pr_mock:
            result = workflow.analyze(defect, resolution)

        self.assertEqual(result.evidence_collection.status, "completed")
        self.assertEqual(result.fix_suggestion_generation.status, "completed")
        clone_mock.assert_not_called()
        subprocess_mock.assert_not_called()
        checkout_mock.assert_not_called()
        clone_repo_mock.assert_not_called()
        commit_mock.assert_not_called()
        push_mock.assert_not_called()
        pr_mock.assert_not_called()

    def test_analyze_result_returns_canonical_actionable_analysis_result(self):
        defect = _make_defect()
        resolution = _make_resolution()
        workflow = DefectAnalysisWorkflowService(
            evidence_service=FakeEvidenceService(FakeResolvedEvidence()),
            analyzer=FakeAnalyzer(),
        )

        result = workflow.analyze_result(defect, resolution)

        self.assertIsInstance(result, AnalysisResult)
        self.assertFalse(result.insufficient_evidence)
        self.assertGreaterEqual(result.confidence, 0.75)
        self.assertEqual(result.root_cause, "`src/auth/callback.py` redirects after SSO login without validating that the tenant context is still present, so the dashboard opens without tenant scope.")
        self.assertEqual(result.impacted_files, ["src/auth/callback.py", "src/session.py"])
        self.assertEqual(result.fix_suggestions[0].impacted_files, ["src/auth/callback.py", "src/session.py"])
        self.assertGreaterEqual(len(result.evidence), 4)
        self.assertTrue(any(item.file_path == "src/auth/callback.py" and item.snippet for item in result.evidence))
        self.assertTrue(any(item.file_path == "src/session.py" and item.snippet for item in result.evidence))

    def test_analyze_result_returns_low_confidence_blocked_canonical_output(self):
        defect = _make_defect()
        resolution = _make_resolution(confidence=0.4)
        workflow = DefectAnalysisWorkflowService(analyzer=FakeAnalyzer())

        result = workflow.analyze_result(defect, resolution)

        self.assertIsInstance(result, AnalysisResult)
        self.assertTrue(result.insufficient_evidence)
        self.assertLessEqual(result.confidence, 0.49)
        self.assertEqual(result.root_cause, "")
        self.assertEqual(result.fix_suggestions, [])
        self.assertIn("could not confirm an actionable root cause", result.analysis_summary)
        self.assertTrue(any(item.kind == "repo_resolution" and item.source == "project_repo_mapping" for item in result.evidence))
        self.assertIn("## Blocked Reason", result.rendered_markdown)

    def test_analyze_result_rendered_markdown_matches_structured_result(self):
        defect = _make_defect()
        resolution = _make_resolution()
        workflow = DefectAnalysisWorkflowService(
            evidence_service=FakeEvidenceService(FakeResolvedEvidence()),
            analyzer=FakeAnalyzer(),
        )

        result = workflow.analyze_result(defect, resolution)

        self.assertIn(result.analysis_summary, result.rendered_markdown)
        self.assertIn(result.root_cause, result.rendered_markdown)
        self.assertIn("src/auth/callback.py", result.rendered_markdown)
        self.assertIn("src/session.py", result.rendered_markdown)
        self.assertIn(result.fix_suggestions[0].title, result.rendered_markdown)
        self.assertIn(f"Score: {result.confidence:.2f}", result.rendered_markdown)


class TestAnalysisResultShaper(unittest.TestCase):
    @staticmethod
    def _make_workflow_result(
        *,
        evidence: tuple[EvidenceReference, ...],
        summary: str,
        root_cause: str,
        impacted_files: tuple[str, ...],
        fix_suggestions: tuple[FixSuggestion, ...],
        confidence: float,
        insufficient_evidence: bool,
        blocked_reason: str = "",
        evidence_status: str = "completed",
        root_status: str = "completed",
        fix_status: str = "completed",
        resolution: RepoResolution | None = None,
    ) -> DefectAnalysisWorkflowResult:
        return DefectAnalysisWorkflowResult(
            defect_id="defect-123",
            project=ProjectRef(id="proj-1", name="Auth Platform"),
            repo_resolution=resolution or _make_resolution(),
            defect_understanding=DefectUnderstandingStage(status="completed", summary="Tenant context is lost after SSO login."),
            evidence_collection=EvidenceCollectionStage(
                status=evidence_status,
                evidence=evidence,
                blocked_reason=blocked_reason,
            ),
            root_cause_hypothesis=RootCauseHypothesisStage(
                status=root_status,
                draft=RootCauseDraft(
                    summary=summary,
                    root_cause=root_cause,
                    impacted_files=impacted_files,
                    confidence=confidence,
                ),
                blocked_reason=blocked_reason,
            ),
            fix_suggestion_generation=FixSuggestionStage(
                status=fix_status,
                draft=FixSuggestionDraft(suggestions=fix_suggestions),
                blocked_reason=blocked_reason,
            ),
            analysis_summary=summary,
            confidence=confidence,
            insufficient_evidence=insufficient_evidence,
            blocked_reason=blocked_reason,
        )

    def test_from_workflow_result_blocks_unsupported_root_cause_claim(self):
        shaper = AnalysisResultShaper()
        workflow_result = self._make_workflow_result(
            evidence=(
                EvidenceReference(
                    kind="defect",
                    description="Defect reports missing tenant scope after login.",
                    snippet="Users lose tenant scope after SSO login.",
                    source="ones",
                ),
                EvidenceReference(
                    kind="repo_resolution",
                    description="Repository mapping resolved auth-platform.",
                    snippet="E:/workspace/fake/auth-platform",
                    source="project_repo_mapping",
                ),
                EvidenceReference(
                    kind="file",
                    file_path="src/auth/callback.py",
                    start_line=1,
                    end_line=4,
                    snippet="def finish_login(request):\n    request.state.tenant = tenant_id\n    return redirect('/dashboard')\n",
                    description="Callback excerpt shows tenant handoff and redirect.",
                    source="keyword_search",
                ),
            ),
            summary="The callback appears related to the missing tenant context.",
            root_cause="`src/auth/callback.py` definitely fails because retry cache poisoning bypasses validation.",
            impacted_files=("src/auth/callback.py",),
            fix_suggestions=(
                FixSuggestion(
                    title="Tighten callback validation",
                    description="Add explicit validation before redirecting.",
                    impacted_files=["src/auth/callback.py"],
                    steps=["Add a validation guard before redirect."],
                    risk_level="medium",
                ),
            ),
            confidence=0.83,
            insufficient_evidence=False,
        )

        result = shaper.from_workflow_result(workflow_result)

        self.assertTrue(result.insufficient_evidence)
        self.assertLessEqual(result.confidence, 0.49)
        self.assertEqual(result.root_cause, "")
        self.assertIn("No evidence item directly supports the claimed root cause.", result.analysis_summary)

    def test_from_workflow_result_rewrites_blocked_summary_to_non_conclusive_text(self):
        shaper = AnalysisResultShaper()
        workflow_result = self._make_workflow_result(
            evidence=(
                EvidenceReference(
                    kind="defect",
                    description="Defect reports missing tenant scope after login.",
                    snippet="Users lose tenant scope after SSO login.",
                    source="ones",
                ),
                EvidenceReference(
                    kind="repo_resolution",
                    description="Repository mapping exists but confidence is weak.",
                    snippet="E:/workspace/fake/auth-platform",
                    source="project_repo_mapping",
                ),
            ),
            summary="The callback definitely loses tenant scope after login.",
            root_cause="",
            impacted_files=(),
            fix_suggestions=(),
            confidence=0.4,
            insufficient_evidence=True,
            blocked_reason="Repository resolution confidence is below the minimum required for evidence access.",
            evidence_status="blocked",
            root_status="blocked",
            fix_status="blocked",
            resolution=_make_resolution(confidence=0.4),
        )

        result = shaper.from_workflow_result(workflow_result)

        self.assertTrue(result.insufficient_evidence)
        self.assertIn("could not confirm an actionable root cause", result.analysis_summary)
        self.assertNotIn("definitely loses tenant scope", result.analysis_summary)
        self.assertIn("## Blocked Reason", result.rendered_markdown)

    def test_from_workflow_result_does_not_upgrade_blocked_staged_result(self):
        shaper = AnalysisResultShaper()
        workflow_result = self._make_workflow_result(
            evidence=(
                EvidenceReference(kind="defect", description="Defect metadata.", snippet="Tenant scope missing.", source="ones"),
                EvidenceReference(kind="repo_resolution", description="Resolved repo.", snippet="E:/workspace/fake/auth-platform", source="project_repo_mapping"),
                EvidenceReference(
                    kind="file",
                    file_path="src/auth/callback.py",
                    start_line=1,
                    end_line=4,
                    snippet="tenant_id = request.session.get('tenant_id')\nreturn redirect('/dashboard')\n",
                    description="Redirect path excerpt.",
                    source="keyword_search",
                ),
            ),
            summary="The callback appears to skip tenant validation.",
            root_cause="`src/auth/callback.py` redirects before the tenant context is validated.",
            impacted_files=("src/auth/callback.py",),
            fix_suggestions=(
                FixSuggestion(
                    title="Validate tenant state",
                    description="Guard the redirect when tenant state is missing.",
                    impacted_files=["src/auth/callback.py"],
                    steps=["Check tenant state before redirecting."],
                    risk_level="medium",
                ),
            ),
            confidence=0.88,
            insufficient_evidence=True,
            blocked_reason="The staged workflow reported insufficient evidence.",
        )

        result = shaper.from_workflow_result(workflow_result)

        self.assertTrue(result.insufficient_evidence)
        self.assertLessEqual(result.confidence, 0.49)
        self.assertEqual(result.fix_suggestions, [])
