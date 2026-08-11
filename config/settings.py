"""ONES Agent 配置 - pydantic-settings"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_DEFECT_STATUS_IDS = [
    "JAZYLueG",
    "VMxom1Jo",
    "WwhszYN8",
    "CKA6U955",
]


def _parse_status_ids(raw: str) -> list[str]:
    status_ids: list[str] = []
    for part in raw.split(","):
        status_id = part.strip(" []'\"")
        if status_id and status_id not in status_ids:
            status_ids.append(status_id)
    return status_ids


class OnesSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ONES_", env_file=".env", extra="ignore")

    base_url: str = "http://aputureones.com:8088"
    email: str = ""
    password: str = ""
    team_id: str = ""
    project_id: str = ""
    issue_type_id: str = ""
    api_token: str = ""
    defect_status_ids: str = ""
    comment_list_path_template: str | None = None
    comment_timeout_seconds: float = Field(default=30.0, gt=0)
    comment_max_pages: int = Field(default=50, gt=0)
    comment_max_comments: int = Field(default=10_000, gt=0)
    comment_max_payload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)

    def defect_status_id_list(self) -> list[str]:
        if self.defect_status_ids:
            return _parse_status_ids(self.defect_status_ids)
        return list(DEFAULT_DEFECT_STATUS_IDS)


class GitSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GIT_", env_file=".env", extra="ignore")

    repo_url: str = ""
    auth_type: str = "https_pat"
    pat: str = ""
    ssh_key_path: str = "~/.ssh/id_rsa"
    default_branch: str = "main"


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env", extra="ignore")

    provider: str = "openai"
    model: str = "gpt-4o"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"


class EmailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMAIL_", env_file=".env", extra="ignore")

    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    sender: str = ""
    use_tls: bool = True


class WechatSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WECHAT_", env_file=".env", extra="ignore")

    webhook_key: str = ""


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    webhook_secret: str = ""
    log_level: str = "INFO"
    state_db_path: str = "data/agent.db"
    check_interval: int = 1800
    codebase_path: str = ""
    repo_url: str = ""
    repo_branch: str = "main"


class Settings:
    """聚合配置，各子模块独立加载环境变量"""

    def __init__(self, env_file: str = ".env"):
        self.ones = OnesSettings(_env_file=env_file)
        self.git = GitSettings(_env_file=env_file)
        self.llm = LLMSettings(_env_file=env_file)
        self.email = EmailSettings(_env_file=env_file)
        self.wechat = WechatSettings(_env_file=env_file)
        self.agent = AgentSettings(_env_file=env_file)

    def summary(self) -> dict:
        return {
            "ones": {
                "base_url": self.ones.base_url,
                "team_id": self.ones.team_id,
                "project_id": self.ones.project_id,
                "defect_status_ids": self.ones.defect_status_id_list(),
                "has_credentials": bool(self.ones.email and self.ones.password),
            },
            "git": {
                "repo_url": self.git.repo_url,
                "auth_type": self.git.auth_type,
                "has_pat": bool(self.git.pat),
            },
            "llm": {
                "provider": self.llm.provider,
                "model": self.llm.model,
                "has_key": bool(self.llm.api_key),
            },
            "wechat": {"has_webhook": bool(self.wechat.webhook_key)},
            "email": {"configured": bool(self.email.smtp_host and self.email.smtp_user)},
            "agent": {
                "log_level": self.agent.log_level,
                "state_db": self.agent.state_db_path,
                "check_interval": self.agent.check_interval,
            },
        }
