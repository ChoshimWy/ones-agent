"""Phase 5 测试 - CI/CD 触发与轮询"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from config.settings import GitSettings
from src.integrations.ci_cd import CICDClient, PipelineStatus, PipelineResult


class TestPipelineResult:
    def test_defaults(self):
        r = PipelineResult()
        assert r.status is None or r.status == ""

    def test_to_dict(self):
        r = PipelineResult(status=PipelineStatus.SUCCESS, pipeline_id="p1", duration=100)
        d = r.to_dict()
        assert d["status"] == PipelineStatus.SUCCESS
        assert d["duration"] == 100


class TestPlatformDetection:
    def test_github(self):
        s = GitSettings(repo_url="https://github.com/org/repo.git", _env_file=None)
        client = CICDClient(s)
        assert client._platform == "github"

    def test_gitlab(self):
        s = GitSettings(repo_url="https://gitlab.com/org/repo.git", _env_file=None)
        client = CICDClient(s)
        assert client._platform == "gitlab"

    def test_generic(self):
        s = GitSettings(repo_url="https://gitea.local/org/repo.git", _env_file=None)
        client = CICDClient(s)
        assert client._platform == "generic"


class TestExtractRepo:
    def test_github_https(self):
        s = GitSettings(repo_url="https://github.com/myorg/myrepo.git", _env_file=None)
        client = CICDClient(s)
        assert client._extract_repo() == "myorg/myrepo"

    def test_no_github(self):
        s = GitSettings(repo_url="https://gitlab.com/x/y.git", _env_file=None)
        client = CICDClient(s)
        assert client._extract_repo() == ""


class TestGitHubTrigger:
    @respx.mock
    @pytest.mark.asyncio
    async def test_trigger_success(self):
        s = GitSettings(
            repo_url="https://github.com/org/repo.git",
            pat="ghp_test123",
            _env_file=None,
        )
        client = CICDClient(s)

        respx.post("https://api.github.com/repos/org/repo/actions/workflows/ci.yml/dispatches").mock(
            return_value=httpx.Response(204)
        )

        pipeline_id = await client.trigger_pipeline("feat/ONES-1-test")
        assert pipeline_id == "github:org/repo:feat/ONES-1-test"
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_trigger_failure(self):
        s = GitSettings(
            repo_url="https://github.com/org/repo.git",
            pat="ghp_test123",
            _env_file=None,
        )
        client = CICDClient(s)

        respx.post("https://api.github.com/repos/org/repo/actions/workflows/ci.yml/dispatches").mock(
            return_value=httpx.Response(401, json={"message": "Bad credentials"})
        )

        pipeline_id = await client.trigger_pipeline("feat/ONES-1-test")
        assert pipeline_id == ""
        await client.close()


class TestGitHubPoll:
    @respx.mock
    @pytest.mark.asyncio
    async def test_poll_running(self):
        s = GitSettings(
            repo_url="https://github.com/org/repo.git",
            pat="ghp_test123",
            _env_file=None,
        )
        client = CICDClient(s)

        respx.get("https://api.github.com/repos/org/repo/actions/runs").mock(
            return_value=httpx.Response(200, json={
                "workflow_runs": [{"id": 123, "status": "in_progress", "html_url": "https://github.com/..."}]
            })
        )

        result = await client.poll_status("github:org/repo:feat/ONES-1-test")
        assert result.status == PipelineStatus.RUNNING
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_poll_success(self):
        s = GitSettings(
            repo_url="https://github.com/org/repo.git",
            pat="ghp_test123",
            _env_file=None,
        )
        client = CICDClient(s)

        respx.get("https://api.github.com/repos/org/repo/actions/runs").mock(
            return_value=httpx.Response(200, json={
                "workflow_runs": [{"id": 123, "status": "completed", "conclusion": "success", "html_url": "https://github.com/..."}]
            })
        )

        result = await client.poll_status("github:org/repo:feat/ONES-1-test")
        assert result.status == PipelineStatus.SUCCESS
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_poll_failed(self):
        s = GitSettings(
            repo_url="https://github.com/org/repo.git",
            pat="ghp_test123",
            _env_file=None,
        )
        client = CICDClient(s)

        respx.get("https://api.github.com/repos/org/repo/actions/runs").mock(
            return_value=httpx.Response(200, json={
                "workflow_runs": [{"id": 123, "status": "completed", "conclusion": "failure", "html_url": "https://github.com/..."}]
            })
        )

        result = await client.poll_status("github:org/repo:feat/ONES-1-test")
        assert result.status == PipelineStatus.FAILED
        await client.close()


class TestTriggerAndWait:
    @pytest.mark.asyncio
    async def test_timeout(self):
        s = GitSettings(repo_url="https://github.com/org/repo.git", _env_file=None)
        client = CICDClient(s)

        with patch.object(client, "trigger_pipeline", new_callable=AsyncMock, return_value="github:org/repo:br"):
            with patch.object(client, "poll_status", new_callable=AsyncMock, return_value=PipelineResult(status=PipelineStatus.RUNNING)):
                result = await client.trigger_and_wait("br", timeout=2, interval=1)

        assert result.status == PipelineStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_no_pipeline_id(self):
        s = GitSettings(repo_url="https://github.com/org/repo.git", _env_file=None)
        client = CICDClient(s)

        with patch.object(client, "trigger_pipeline", new_callable=AsyncMock, return_value=""):
            result = await client.trigger_and_wait("br")

        assert result.status == PipelineStatus.FAILED

    @pytest.mark.asyncio
    async def test_success_immediately(self):
        s = GitSettings(repo_url="https://github.com/org/repo.git", _env_file=None)
        client = CICDClient(s)

        with patch.object(client, "trigger_pipeline", new_callable=AsyncMock, return_value="github:org/repo:br"):
            with patch.object(client, "poll_status", new_callable=AsyncMock, return_value=PipelineResult(status=PipelineStatus.SUCCESS)):
                result = await client.trigger_and_wait("br")

        assert result.status == PipelineStatus.SUCCESS


class TestUnsupportedPlatform:
    @pytest.mark.asyncio
    async def test_generic_trigger(self):
        s = GitSettings(repo_url="https://gitea.local/x/y.git", _env_file=None)
        client = CICDClient(s)

        pipeline_id = await client.trigger_pipeline("main")
        assert pipeline_id == ""

    @pytest.mark.asyncio
    async def test_generic_poll(self):
        s = GitSettings(repo_url="https://gitea.local/x/y.git", _env_file=None)
        client = CICDClient(s)

        result = await client.poll_status("some-id")
        assert result.status == PipelineStatus.PENDING
