from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.contracts import ProjectRef, RepoResolution, RepoTarget
from src.services import CodebaseEvidenceService


def _make_repo_fixture(root: Path) -> Path:
    repo = root / "payments-service"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "payments.py").write_text(
        "def capture_payment(amount):\n"
        "    if amount <= 0:\n"
        "        raise ValueError('amount must be positive')\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (repo / "src" / "refunds.py").write_text(
        "class RefundService:\n"
        "    def refund(self, payment_id):\n"
        "        return payment_id\n",
        encoding="utf-8",
    )
    return repo


class TestCodebaseEvidenceService(unittest.TestCase):
    def test_mapped_resolution_produces_codebase_access_and_stable_evidence_outputs(self):
        with TemporaryDirectory() as temp_dir:
            repo_path = _make_repo_fixture(Path(temp_dir))
            resolution = RepoResolution(
                defect_id="defect-1",
                project=ProjectRef(id="proj-1", name="Payments"),
                selected_repo=RepoTarget(
                    repo_url=str(repo_path),
                    repo_name="payments-service",
                    default_branch="main",
                ),
                selected_branch="main",
                confidence=1.0,
                source="project_repo_mapping",
                rationale="Resolved from configured project-to-repo mapping.",
            )

            access = CodebaseEvidenceService().from_resolution(resolution)

            self.assertTrue(access.state.available)
            self.assertEqual(access.state.status, "available")

            tree = access.repository_tree_summary(max_depth=3)
            self.assertEqual(tree.status, "ok")
            self.assertIn("src/", tree.tree)
            self.assertIn("payments.py", tree.tree)

            candidates = access.keyword_file_candidates(["refund", "payment"], max_files=5)
            self.assertEqual(candidates.status, "ok")
            self.assertEqual([candidate.file_path for candidate in candidates.candidates], ["src/payments.py", "src/refunds.py"])
            self.assertTrue(any("capture_payment" in candidate.preview for candidate in candidates.candidates))

            excerpt = access.file_excerpt("src/payments.py", start_line=2, end_line=3)
            self.assertEqual(excerpt.status, "ok")
            self.assertEqual(excerpt.start_line, 2)
            self.assertEqual(excerpt.end_line, 3)
            self.assertIn("amount <= 0", excerpt.content)
            self.assertIn("raise ValueError", excerpt.content)

            missing = access.file_excerpt("src/missing.py")
            self.assertEqual(missing.status, "file_not_found")
            self.assertEqual(missing.file_path, "src/missing.py")

    def test_unresolved_resolution_is_surfaced_explicitly_in_access_state_and_reads(self):
        resolution = RepoResolution(
            defect_id="defect-2",
            project=ProjectRef(id="proj-missing", name="Unknown"),
            confidence=0.0,
            source="project_repo_mapping_missing",
            rationale="No repository mapping exists for project proj-missing.",
        )

        access = CodebaseEvidenceService().from_resolution(resolution)

        self.assertFalse(access.state.available)
        self.assertEqual(access.state.status, "unresolved")
        self.assertEqual(access.state.source, "project_repo_mapping_missing")

        tree = access.repository_tree_summary()
        self.assertEqual(tree.status, "unresolved")
        self.assertEqual(tree.tree, "")
        self.assertIn("No repository mapping exists", tree.rationale)

        candidates = access.keyword_file_candidates(["login"])
        self.assertEqual(candidates.status, "unresolved")
        self.assertEqual(candidates.candidates, ())

        excerpt = access.file_excerpt("src/app.py", start_line=3, end_line=5)
        self.assertEqual(excerpt.status, "unresolved")
        self.assertEqual(excerpt.file_path, "src/app.py")
        self.assertEqual(excerpt.start_line, 3)
        self.assertEqual(excerpt.end_line, 5)

    def test_weak_resolution_is_explicitly_blocked_before_codebase_reads(self):
        with TemporaryDirectory() as temp_dir:
            repo_path = _make_repo_fixture(Path(temp_dir))
            resolution = RepoResolution(
                defect_id="defect-3",
                project=ProjectRef(id="proj-weak", name="Payments"),
                selected_repo=RepoTarget(repo_url=str(repo_path), repo_name="payments-service", default_branch="main"),
                selected_branch="main",
                confidence=0.6,
                source="heuristic_guess",
                rationale="Heuristic repo match needs confirmation before evidence collection.",
            )

            access = CodebaseEvidenceService().from_resolution(resolution)

            self.assertFalse(access.state.available)
            self.assertEqual(access.state.status, "weak_resolution")
            self.assertEqual(access.repository_tree_summary().status, "weak_resolution")
            self.assertEqual(access.keyword_file_candidates(["payment"]).status, "weak_resolution")
