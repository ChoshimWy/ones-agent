"""Git 操作 - 克隆/分支/提交/推送/PR"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import git
import structlog

from config.settings import GitSettings

log = structlog.get_logger()


def _build_auth_url(repo_url: str, settings: GitSettings) -> str:
    """根据 GIT_AUTH_TYPE 拼接带认证的 URL"""
    if settings.auth_type == "ssh":
        url = repo_url
        if url.startswith("https://"):
            host = url.replace("https://", "").replace("http://", "")
            path = host.split("/", 1)[1] if "/" in host else host
            url = f"git@{host.split('/')[0]}:{path}"
        if url.endswith(".git"):
            url = url[:-4]
        return url

    if settings.pat and repo_url.startswith("https://"):
        return repo_url.replace("https://", f"https://oauth2:{settings.pat}@")
    return repo_url


def _slugify(text: str, max_len: int = 30) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"[^\x00-\x7f]+", "", text)
    text = re.sub(r"-+", "-", text)
    return text[:max_len].strip("-")


def build_branch_name(work_item_id: str, work_type: str, title: str) -> str:
    prefix = "feat" if work_type == "requirement" else "fix"
    slug = _slugify(title)
    if not slug:
        raise ValueError("Branch title must produce a non-empty ASCII-safe slug.")
    return f"{prefix}/{work_item_id.strip()}-{slug}"


class GitOps:
    """Git 操作封装

    用法:
        ops = GitOps(settings)
        repo_dir = ops.clone_repo()
        ops.checkout_branch(repo_dir, "ONES-REQ-123", "requirement", "添加登录功能")
        # ... 修改文件 ...
        ops.commit_changes(repo_dir, "ONES-REQ-123", "implement login")
        ops.push_branch(repo_dir)
        ops.create_pr(repo_dir, "feat: 添加登录", "实现登录功能")
    """

    def __init__(self, settings: GitSettings | None = None, work_dir: str = "data/repos"):
        self._settings = settings or GitSettings()
        self._work_dir = Path(work_dir)
        self._work_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _repo_name(self) -> str:
        url = self._settings.repo_url.rstrip("/")
        name = url.split("/")[-1].removesuffix(".git")
        return name or "repo"

    @property
    def _repo_dir(self) -> Path:
        return self._work_dir / self._repo_name

    def clone_repo(self) -> Path:
        auth_url = _build_auth_url(self._settings.repo_url, self._settings)
        if self._repo_dir.exists():
            log.info("git_fetch", dir=str(self._repo_dir))
            repo = git.Repo(str(self._repo_dir))
            repo.remotes.origin.fetch()
            repo.git.checkout(self._settings.default_branch)
            repo.git.pull()
            return self._repo_dir

        log.info("git_clone", url=self._settings.repo_url, dir=str(self._repo_dir))
        git.Repo.clone_from(auth_url, str(self._repo_dir), branch=self._settings.default_branch)
        return self._repo_dir

    def checkout_branch(self, repo_dir: Path | str, work_item_id: str, work_type: str, title: str) -> str:
        branch_name = build_branch_name(work_item_id, work_type, title)

        repo = git.Repo(str(repo_dir))
        repo.remotes.origin.fetch()
        repo.git.checkout(self._settings.default_branch)
        repo.git.pull()

        local_branches = {head.name for head in repo.heads}
        remote_branch_names = {ref.remote_head for ref in repo.remotes.origin.refs if ref.remote_head != "HEAD"}
        reused = False
        if branch_name in local_branches:
            repo.git.checkout(branch_name)
            reused = True
        elif branch_name in remote_branch_names:
            repo.git.checkout("-B", branch_name, f"origin/{branch_name}")
            reused = True
        else:
            repo.git.checkout("-b", branch_name)

        log.info("git_branch_ready", branch=branch_name, reused=reused)
        return branch_name

    def commit_changes(self, repo_dir: Path | str, work_item_id: str, summary: str) -> str:
        repo = git.Repo(str(repo_dir))
        repo.git.add("-A")

        status = repo.git.status("--porcelain")
        if not status:
            log.warning("git_nothing_to_commit", work_item_id=work_item_id)
            return ""

        try:
            repo.config_reader().get_value("user", "name")
        except Exception:
            repo.config_writer().set_value("user", "name", "ONES Agent").release()
            repo.config_writer().set_value("user", "email", "agent@ones.local").release()

        commit_msg = f"{_guess_prefix(summary)}({work_item_id}): {summary}"
        repo.git.commit("-m", commit_msg)
        commit_hash = repo.head.commit.hexsha
        log.info("git_committed", work_item_id=work_item_id, hash=commit_hash, msg=commit_msg)
        return commit_hash

    def push_branch(self, repo_dir: Path | str) -> None:
        repo = git.Repo(str(repo_dir))
        branch = repo.active_branch.name
        auth_url = _build_auth_url(self._settings.repo_url, self._settings)
        repo.remotes.origin.set_url(auth_url)
        repo.git.push("--set-upstream", "origin", branch)
        log.info("git_pushed", branch=branch)

    def create_pr(self, repo_dir: Path | str, title: str, body: str, target_branch: str = "") -> str:
        repo = git.Repo(str(repo_dir))
        branch = repo.active_branch.name
        target = target_branch or self._settings.default_branch
        auth_url = _build_auth_url(self._settings.repo_url, self._settings)

        remote_url = repo.remotes.origin.url
        if "github.com" in remote_url or "github.com" in auth_url:
            return self._github_pr(auth_url, repo_dir, branch, title, body, target)
        if "gitlab" in remote_url or "gitlab" in auth_url:
            return self._gitlab_pr(auth_url, branch, title, body, target)

        log.warning("git_pr_unsupported_host", url=remote_url)
        return ""

    def _github_pr(self, auth_url: str, repo_dir: Path | str, branch: str, title: str, body: str, target: str) -> str:
        owner_repo = auth_url.split("github.com")[-1].lstrip(":/").removesuffix(".git")
        if "@" in owner_repo:
            owner_repo = owner_repo.split(":", 1)[1] if ":" in owner_repo else owner_repo

        cmd = [
            "gh", "pr", "create",
            "--repo", owner_repo,
            "--head", branch,
            "--base", target,
            "--title", title,
            "--body", body,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(repo_dir))
        if result.returncode != 0:
            log.error("gh_pr_failed", stderr=result.stderr)
            return ""
        pr_url = result.stdout.strip()
        log.info("github_pr_created", url=pr_url)
        return pr_url

    def _gitlab_pr(self, auth_url: str, branch: str, title: str, body: str, target: str) -> str:
        project_id = auth_url.split("/")[-1].removesuffix(".git")
        api_url = auth_url.split("/-/")[0] if "/-/" in auth_url else auth_url.rsplit("/", 2)[0]
        log.warning("gitlab_pr_not_implemented", project=project_id)
        return ""

    def get_repo(self, repo_dir: Path | str) -> git.Repo:
        return git.Repo(str(repo_dir))


def _guess_prefix(summary: str) -> str:
    s = summary.lower()
    if any(w in s for w in ("fix", "bug", "patch", "hotfix")):
        return "fix"
    if any(w in s for w in ("test", "spec")):
        return "test"
    if any(w in s for w in ("refactor", "clean", "rename")):
        return "refactor"
    if any(w in s for w in ("doc", "readme", "comment")):
        return "docs"
    if any(w in s for w in ("add", "new", "create", "implement")):
        return "feat"
    return "feat"
