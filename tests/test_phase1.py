"""Phase 1 测试 - 配置、日志、凭证脱敏、重试"""

import os

import pytest

from config.settings import Settings, OnesSettings, GitSettings, LLMSettings, AgentSettings
from src.utils.secrets import mask_value, mask_dict, mask_string
from src.utils.retry import retry


class TestSettings:
    def test_ones_settings_defaults(self):
        s = OnesSettings(_env_file=None)
        assert s.base_url == "http://aputureones.com:8088"

    def test_git_settings_defaults(self):
        s = GitSettings(_env_file=None)
        assert s.auth_type == "https_pat"
        assert s.default_branch == "main"

    def test_llm_settings_defaults(self):
        s = LLMSettings(_env_file=None)
        assert s.provider == "openai"
        assert s.model == "gpt-4o"

    def test_agent_settings_defaults(self):
        s = AgentSettings(_env_file=None)
        assert s.log_level == "INFO"
        assert s.state_db_path == "data/agent.db"

    def test_settings_summary_masks_secrets(self, monkeypatch):
        monkeypatch.setenv("ONES_EMAIL", "test@test.com")
        monkeypatch.setenv("ONES_PASSWORD", "secret123")
        monkeypatch.setenv("LLM_API_KEY", "sk-abc123")
        s = Settings(env_file=None)
        summary = s.summary()
        assert summary["ones"]["has_credentials"] is True
        assert "secret123" not in str(summary)
        assert "sk-abc123" not in str(summary)

    def test_settings_env_override(self, monkeypatch):
        monkeypatch.setenv("ONES_BASE_URL", "http://custom:9999")
        monkeypatch.setenv("LLM_MODEL", "deepseek-v3")
        s = Settings(env_file=None)
        assert s.ones.base_url == "http://custom:9999"
        assert s.llm.model == "deepseek-v3"


class TestSecrets:
    def test_mask_value_short(self):
        assert mask_value("ab") == "***"

    def test_mask_value_long(self):
        assert mask_value("sk-1234567890") == "sk-1***"

    def test_mask_dict(self):
        d = {"api_key": "sk-secret", "name": "visible"}
        masked = mask_dict(d)
        assert masked["name"] == "visible"
        assert "secret" not in masked["api_key"]

    def test_mask_string_bearer(self):
        s = "Authorization: Bearer tok_abc123"
        assert "tok_abc123" not in mask_string(s)
        assert "Bearer" in mask_string(s)


class TestRetry:
    def test_retry_success_first_try(self):
        call_count = 0

        @retry(max_retries=3, backoff_factor=0)
        def ok():
            nonlocal call_count
            call_count += 1
            return "done"

        assert ok() == "done"
        assert call_count == 1

    def test_retry_succeeds_after_failures(self):
        call_count = 0

        @retry(max_retries=3, backoff_factor=0, retry_on=(ValueError,))
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "ok"

        assert flaky() == "ok"
        assert call_count == 3

    def test_retry_exhausted(self):
        @retry(max_retries=2, backoff_factor=0, retry_on=(ValueError,))
        def always_fail():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            always_fail()
