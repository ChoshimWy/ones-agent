"""Phase 3 测试 - Git 操作"""

import os
import tempfile
from pathlib import Path

import git
import pytest

from config.settings import GitSettings
from src.integrations.git_ops import GitOps, _build_auth_url, _slugify, _guess_prefix, build_branch_name


class TestSlugify:
    def test_basic(self):
        assert _slugify("Add Login Feature") == "add-login-feature"

    def test_special_chars(self):
        assert _slugify("Fix: bug #123!!!") == "fix-bug-123"

    def test_truncate(self):
        assert len(_slugify("a" * 50, max_len=10)) <= 10

    def test_chinese(self):
        assert _slugify("修复登录问题") == ""


class TestGuessPrefix:
    def test_fix(self):
        assert _guess_prefix("fix null pointer") == "fix"

    def test_feat(self):
        assert _guess_prefix("add new feature") == "feat"

    def test_refactor(self):
        assert _guess_prefix("refactor module") == "refactor"

    def test_test(self):
        assert _guess_prefix("add test case") == "test"

    def test_docs(self):
        assert _guess_prefix("update readme") == "docs"


class TestBuildAuthUrl:
    def test_https_with_pat(self):
        settings = GitSettings(auth_type="https_pat", pat="my-token", _env_file=None)
        url = _build_auth_url("https://github.com/org/repo.git", settings)
        assert "oauth2:my-token@" in url

    def test_ssh_transform(self):
        settings = GitSettings(auth_type="ssh", _env_file=None)
        url = _build_auth_url("https://github.com/org/repo.git", settings)
        assert url.startswith("git@github.com:")
        assert url.endswith("org/repo")

    def test_no_pat(self):
        settings = GitSettings(auth_type="https_pat", pat="", _env_file=None)
        url = _build_auth_url("https://github.com/org/repo.git", settings)
        assert url == "https://github.com/org/repo.git"


class TestBuildBranchName:
    def test_requirement_branch_name(self):
        assert build_branch_name("ONES-REQ-123", "requirement", "Add login page") == "feat/ONES-REQ-123-add-login-page"

    def test_defect_branch_name(self):
        assert build_branch_name("ONES-BUG-456", "defect", "Fix null pointer") == "fix/ONES-BUG-456-fix-null-pointer"


class TestGitOpsIntegration:
    """使用本地临时 Git 仓库测试真实 Git 操作"""

    @pytest.fixture
    def remote_repo(self, tmp_path):
        bare = tmp_path / "remote.git"
        bare.mkdir()
        git.Repo.init(str(bare), bare=True)

        clone_dir = tmp_path / "initial"
        cloned = git.Repo.clone_from(str(bare), str(clone_dir))
        cloned.config_writer().set_value("user", "name", "Test").release()
        cloned.config_writer().set_value("user", "email", "test@test.com").release()
        (clone_dir / "README.md").write_text("# Test")
        cloned.git.add("-A")
        cloned.git.commit("-m", "init")
        cloned.git.branch("-M", "main")
        cloned.git.push("-u", "origin", "main")
        return str(bare), str(clone_dir)

    @pytest.fixture
    def ops(self, remote_repo, tmp_path):
        bare_url, _ = remote_repo
        settings = GitSettings(
            repo_url=bare_url,
            auth_type="https_pat",
            pat="",
            default_branch="main",
            _env_file=None,
        )
        return GitOps(settings, work_dir=str(tmp_path / "work"))

    def test_clone_repo(self, ops, remote_repo):
        repo_dir = ops.clone_repo()
        assert repo_dir.exists()
        repo = git.Repo(str(repo_dir))
        assert repo.active_branch.name == "main"

    def test_clone_repo_idempotent(self, ops, remote_repo):
        dir1 = ops.clone_repo()
        dir2 = ops.clone_repo()
        assert dir1 == dir2

    def test_checkout_branch(self, ops, remote_repo):
        repo_dir = ops.clone_repo()
        branch = ops.checkout_branch(repo_dir, "ONES-REQ-123", "requirement", "Add login page")
        assert branch == "feat/ONES-REQ-123-add-login-page"

        repo = git.Repo(str(repo_dir))
        assert repo.active_branch.name == branch

    def test_checkout_branch_defect(self, ops, remote_repo):
        repo_dir = ops.clone_repo()
        branch = ops.checkout_branch(repo_dir, "ONES-BUG-456", "defect", "Fix null pointer")
        assert branch.startswith("fix/ONES-BUG-456-")

    def test_checkout_branch_idempotent_reuses_existing_branch(self, ops, remote_repo):
        repo_dir = ops.clone_repo()
        branch1 = ops.checkout_branch(repo_dir, "ONES-REQ-123", "requirement", "Add login page")

        repo = git.Repo(str(repo_dir))
        repo.git.checkout("main")

        branch2 = ops.checkout_branch(repo_dir, "ONES-REQ-123", "requirement", "Add login page")

        assert branch2 == branch1
        assert repo.active_branch.name == branch1
        assert [head.name for head in repo.heads].count(branch1) == 1

    def test_commit_changes(self, ops, remote_repo):
        repo_dir = ops.clone_repo()
        ops.checkout_branch(repo_dir, "ONES-REQ-123", "requirement", "Add feature")

        (repo_dir / "new_file.py").write_text("print('hello')")
        commit_hash = ops.commit_changes(repo_dir, "ONES-REQ-123", "add new feature")

        assert commit_hash
        repo = git.Repo(str(repo_dir))
        assert "ONES-REQ-123" in repo.head.commit.message
        assert "feat" in repo.head.commit.message

    def test_commit_nothing(self, ops, remote_repo):
        repo_dir = ops.clone_repo()
        ops.checkout_branch(repo_dir, "ONES-REQ-123", "requirement", "Add feature")

        commit_hash = ops.commit_changes(repo_dir, "ONES-REQ-123", "no change")
        assert commit_hash == ""

    def test_push_branch(self, ops, remote_repo):
        repo_dir = ops.clone_repo()
        ops.checkout_branch(repo_dir, "ONES-REQ-123", "requirement", "Add feature")
        (repo_dir / "file.py").write_text("x = 1")
        ops.commit_changes(repo_dir, "ONES-REQ-123", "add feature")
        ops.push_branch(repo_dir)

        bare_repo = git.Repo(remote_repo[0])
        refs = [ref.name for ref in bare_repo.refs]
        assert any("ONES-REQ-123" in r for r in refs)

    def test_full_workflow(self, ops, remote_repo):
        repo_dir = ops.clone_repo()
        branch = ops.checkout_branch(repo_dir, "ONES-REQ-789", "requirement", "Implement auth")
        (repo_dir / "auth.py").write_text("def login(): pass")
        ops.commit_changes(repo_dir, "ONES-REQ-789", "implement auth module")
        ops.push_branch(repo_dir)

        bare_repo = git.Repo(remote_repo[0])
        refs = [ref.name for ref in bare_repo.refs]
        assert any("ONES-REQ-789" in r for r in refs)
