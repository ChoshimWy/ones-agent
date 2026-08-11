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

        from config.settings import LLMSettings, OnesSettings, WechatSettings

        ones = OnesSettings(_env_file=None)
        llm = LLMSettings(_env_file=None)
        wechat = WechatSettings(_env_file=None)

        assert ones.base_url == "http://aputureones.com:8088"
        assert llm.base_url == "https://api.openai.com/v1"
        assert llm.model == "gpt-4o"
        assert isinstance(ones.email, str)
        assert isinstance(wechat.webhook_key, str)

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

    def test_ones_defect_status_ids_are_fixed(self):
        from config.settings import OnesSettings

        settings = OnesSettings()

        assert settings.defect_status_id_list() == ["JAZYLueG", "VMxom1Jo", "WwhszYN8", "CKA6U955"]

    def test_ones_defect_status_ids_supports_env_override(self, monkeypatch):
        monkeypatch.setenv("ONES_DEFECT_STATUS_IDS", " status-a , status-b,, status-c , ")

        from config.settings import OnesSettings

        settings = OnesSettings()

        assert settings.defect_status_id_list() == ["status-a", "status-b", "status-c"]

    def test_ones_defect_status_ids_migrates_python_list_text(self):
        from config.settings import OnesSettings

        settings = OnesSettings(
            defect_status_ids="['JAZYLueG', 'VMxom1Jo']",
            _env_file=None,
        )

        assert settings.defect_status_id_list() == ["JAZYLueG", "VMxom1Jo"]

    def test_ones_defect_status_ids_migrates_nested_legacy_text(self):
        from config.settings import OnesSettings

        settings = OnesSettings(
            defect_status_ids='[\'["JAZYLueG"\', \'"VMxom1Jo"\', \'"WwhszYN8" ]\']',
            _env_file=None,
        )

        assert settings.defect_status_id_list() == ["JAZYLueG", "VMxom1Jo", "WwhszYN8"]
