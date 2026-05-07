"""config.py 测试"""

import os


class TestConfig:
    def test_default_values(self, monkeypatch):
        """未设置环境变量时有默认值"""
        for key in ["ONES_BASE_URL", "ONES_EMAIL", "ONES_PASSWORD",
                     "ONES_TEAM_ID", "ONES_PROJECT_ID", "ONES_ISSUE_TYPE_ID",
                     "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
                     "WECHAT_WEBHOOK_KEY"]:
            monkeypatch.delenv(key, raising=False)

        import importlib
        import config
        importlib.reload(config)

        assert config.ONES_BASE_URL == "http://aputureones.com:8088"
        assert config.LLM_BASE_URL == "https://api.openai.com/v1"
        assert config.LLM_MODEL == "gpt-4o"
        assert isinstance(config.ONES_EMAIL, str)
        assert isinstance(config.WECHAT_WEBHOOK_KEY, str)

    def test_env_override(self, monkeypatch):
        """环境变量覆盖默认值"""
        monkeypatch.setenv("ONES_EMAIL", "test@example.com")
        monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
        monkeypatch.setenv("ONES_TEAM_ID", "ABC123")

        import importlib
        import config
        importlib.reload(config)

        assert config.ONES_EMAIL == "test@example.com"
        assert config.LLM_MODEL == "deepseek-chat"
        assert config.ONES_TEAM_ID == "ABC123"
