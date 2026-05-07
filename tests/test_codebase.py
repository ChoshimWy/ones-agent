"""codebase.py 测试"""

import os
from pathlib import Path

from src.integrations.codebase import Codebase


def _make_project(tmp_path: Path) -> Path:
    """创建模拟项目目录"""
    proj = tmp_path / "myproject"
    proj.mkdir()
    (proj / "src").mkdir()
    (proj / "src" / "main.py").write_text("def login(email, password):\n    pass\n")
    (proj / "src" / "auth.py").write_text("class AuthService:\n    def check(self):\n        pass\n")
    (proj / "README.md").write_text("# My Project\n")
    (proj / "__pycache__").mkdir()
    (proj / "__pycache__" / "cache.pyc").write_text("binary")
    return proj


class TestCodebaseTree:
    def test_tree_returns_structure(self, tmp_path):
        proj = _make_project(tmp_path)
        cb = Codebase(path=str(proj))
        tree = cb.tree()
        assert "main.py" in tree
        assert "auth.py" in tree
        assert "README.md" in tree

    def test_tree_ignores_pycache(self, tmp_path):
        proj = _make_project(tmp_path)
        cb = Codebase(path=str(proj))
        tree = cb.tree()
        assert "__pycache__" not in tree
        assert "cache.pyc" not in tree

    def test_tree_empty_path(self, tmp_path):
        cb = Codebase(path=str(tmp_path / "nonexist"))
        assert cb.tree() == ""


class TestCodebaseReadFile:
    def test_read_existing_file(self, tmp_path):
        proj = _make_project(tmp_path)
        cb = Codebase(path=str(proj))
        content = cb.read_file("src/main.py")
        assert "def login" in content

    def test_read_nonexistent_file(self, tmp_path):
        proj = _make_project(tmp_path)
        cb = Codebase(path=str(proj))
        assert cb.read_file("nope.py") is None

    def test_read_no_path(self, tmp_path):
        cb = Codebase()
        assert cb.read_file("any.py") is None


class TestCodebaseSearch:
    def test_search_by_filename(self, tmp_path):
        proj = _make_project(tmp_path)
        cb = Codebase(path=str(proj))
        results = cb.search_keywords(["auth"])
        assert any("auth.py" in p for p in results)

    def test_search_by_content(self, tmp_path):
        proj = _make_project(tmp_path)
        cb = Codebase(path=str(proj))
        results = cb.search_keywords(["login"])
        assert any("main.py" in p for p in results)

    def test_search_no_match(self, tmp_path):
        proj = _make_project(tmp_path)
        cb = Codebase(path=str(proj))
        results = cb.search_keywords(["zzzznonexistent"])
        assert len(results) == 0


class TestCodebaseGetContext:
    def test_get_context_extracts_keywords(self, tmp_path):
        proj = _make_project(tmp_path)
        cb = Codebase(path=str(proj))
        defect = {"name": "登录页面崩溃 login crash"}
        results = cb.get_context_for_defect(defect)
        assert len(results) > 0
