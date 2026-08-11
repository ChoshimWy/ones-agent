from __future__ import annotations

import httpx
import pytest

from src.developer_workflow.pr_provider import (
    HttpPullRequestClient,
    PullRequestProviderError,
)


def _client(provider: str, handler) -> HttpPullRequestClient:
    transport = httpx.MockTransport(handler)
    return HttpPullRequestClient(
        provider=provider,
        provider_host=f"{provider}.example",
        api_base_url=f"https://{provider}.example",
        token_provider=lambda: "private-token",
        client=httpx.Client(transport=transport),
        retry_backoff=lambda attempt: 0,
    )


def test_github_find_uses_exact_head_base_and_marker() -> None:
    requests: list[httpx.Request] = []
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[
            {"head":{"ref":"feature"},"base":{"ref":"main"},"body":"wrong","html_url":"https://github.example/x/y/pull/1"},
            {"head":{"ref":"feature"},"base":{"ref":"main"},"body":"body\nones-dev-run:abc","html_url":"https://github.example/x/y/pull/2"},
        ])
    client = _client("github", handler)
    url = client.find(repo_url="https://github.example/x/y.git",head="feature",base="main",marker="ones-dev-run:abc")
    assert url.endswith("/2")
    assert requests[0].method == "GET"
    assert "private-token" not in str(requests[0].url)


def test_gitlab_create_uses_expected_fields_and_marker() -> None:
    captured: dict = {}
    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(201, json={"web_url":"https://gitlab.example/x/y/-/merge_requests/1"})
    client = _client("gitlab", handler)
    url = client.create(repo_url="https://gitlab.example/x/y.git",head="feature",base="main",title="Title",body="Body",marker="ones-dev-run:abc")
    assert url.endswith("/1")
    assert captured == {"source_branch":"feature","target_branch":"main","title":"Title","description":"Body\n\nones-dev-run:abc"}


def test_find_retries_429_but_auth_failure_does_not_retry() -> None:
    calls = 0
    def retry_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls; calls += 1
        return httpx.Response(429 if calls < 3 else 200, json=[])
    assert _client("github", retry_handler).find(repo_url="https://github.example/x/y.git",head="f",base="main",marker="m") is None
    assert calls == 3

    auth_calls = 0
    def auth_handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls; auth_calls += 1
        return httpx.Response(401, text="private-token")
    with pytest.raises(PullRequestProviderError) as caught:
        _client("github", auth_handler).find(repo_url="https://github.example/x/y.git",head="f",base="main",marker="m")
    assert auth_calls == 1 and "private-token" not in str(caught.value)


@pytest.mark.parametrize("provider", ["unknown", "local_fake"])
def test_unknown_provider_fails_before_http(provider: str) -> None:
    with pytest.raises(ValueError):
        _client(provider, lambda request: pytest.fail("HTTP must not be called"))


def test_repo_host_mismatch_and_empty_url_fail_closed() -> None:
    client = _client("github", lambda request: httpx.Response(201, json={"html_url":""}))
    with pytest.raises(PullRequestProviderError):
        client.find(repo_url="https://evil.example/x/y.git",head="f",base="main",marker="m")
    with pytest.raises(PullRequestProviderError):
        client.create(repo_url="https://github.example/x/y.git",head="f",base="main",title="t",body="b",marker="m")


@pytest.mark.parametrize(
    ("provider", "repo_url", "expected_path"),
    [
        ("github", "git@github.example:Team/CaseRepo.git", "/repos/Team/CaseRepo/pulls"),
        ("gitlab", "git@gitlab.example:Group/CaseRepo.git", "/api/v4/projects/Group%2FCaseRepo/merge_requests"),
    ],
)
def test_scp_repository_urls_preserve_path_case(provider, repo_url, expected_path) -> None:
    requests = []
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])
    client = _client(provider, handler)
    assert client.find(repo_url=repo_url, head="f", base="main", marker="m") is None
    assert requests[0].url.raw_path.decode().split("?", 1)[0] == expected_path


def test_http_repository_userinfo_is_rejected_before_transport() -> None:
    client = _client("github", lambda request: pytest.fail("HTTP must not be called"))
    with pytest.raises(PullRequestProviderError):
        client.find(
            repo_url="https://user@github.example/Team/Repo.git",
            head="f", base="main", marker="m",
        )
