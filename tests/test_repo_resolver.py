from __future__ import annotations

import gc
import tempfile
import unittest
from pathlib import Path

from src.contracts import DefectRecord, ProjectRef
from src.core.engine import Engine
from src.services import RepoResolver


class TestRepoResolver(unittest.TestCase):
    def test_resolve_returns_deterministic_repo_resolution_for_mapped_project(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            engine = Engine(db_path=str(db_path))
            engine.add_project_repo(
                project_id="proj-1",
                project_name="Payments",
                repo_url="https://example.com/acme/payments-service.git",
                branch="release/2026.05",
            )
            resolver = RepoResolver(engine=engine)
            defect = DefectRecord(
                defect_id="defect-123",
                title="Refund job fails",
                project=ProjectRef(id="proj-1", name="Payments"),
            )

            resolution = resolver.resolve(defect=defect)

            self.assertEqual(resolution.defect_id, "defect-123")
            self.assertEqual(resolution.project.id, "proj-1")
            self.assertEqual(
                resolution.selected_repo.repo_url,
                "https://example.com/acme/payments-service.git",
            )
            self.assertEqual(resolution.selected_repo.repo_name, "payments-service")
            self.assertEqual(resolution.selected_repo.default_branch, "release/2026.05")
            self.assertEqual(resolution.selected_branch, "release/2026.05")
            self.assertEqual(resolution.source, "project_repo_mapping")
            self.assertEqual(resolution.confidence, 1.0)
            self.assertEqual(len(resolution.candidates), 1)
            self.assertEqual(resolution.candidates[0].branch, "release/2026.05")
            self.assertEqual(resolution.candidates[0].source, "project_repo_mapping")
            self.assertEqual(resolution.candidates[0].confidence, 1.0)
            del resolution
            del defect
            del resolver
            del engine
            gc.collect()

    def test_resolve_falls_back_to_configured_default_branch_when_mapping_branch_is_blank(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            engine = Engine(db_path=str(db_path))
            engine.add_project_repo(
                project_id="proj-2",
                project_name="Checkout",
                repo_url="https://example.com/acme/checkout.git",
                branch="",
            )
            resolver = RepoResolver(engine=engine, default_branch="develop")

            resolution = resolver.resolve(project=ProjectRef(id="proj-2", name="Checkout"))

            self.assertEqual(resolution.selected_repo.repo_url, "https://example.com/acme/checkout.git")
            self.assertEqual(resolution.selected_repo.default_branch, "develop")
            self.assertEqual(resolution.selected_branch, "develop")
            self.assertEqual(resolution.source, "project_repo_mapping")
            self.assertEqual(resolution.confidence, 1.0)
            del resolution
            del resolver
            del engine
            gc.collect()

    def test_resolve_returns_explicit_unresolved_result_for_unmapped_project(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            engine = Engine(db_path=str(db_path))
            resolver = RepoResolver(engine=engine)

            resolution = resolver.resolve(project=ProjectRef(id="proj-missing", name="Unknown"))

            self.assertEqual(resolution.project.id, "proj-missing")
            self.assertEqual(resolution.selected_repo.repo_url, "")
            self.assertEqual(resolution.selected_branch, "")
            self.assertEqual(resolution.source, "project_repo_mapping_missing")
            self.assertEqual(resolution.confidence, 0.0)
            self.assertEqual(resolution.candidates, [])
            self.assertIn("No repository mapping exists", resolution.rationale)
            del resolution
            del resolver
            del engine
            gc.collect()
