"""ONES Defect Agent - 配置 (backward compat)"""

# All config is now managed via config.settings (pydantic-settings).
# This module only re-exports for legacy imports.

from config.settings import Settings as _Settings

_settings = _Settings()

ONES_BASE_URL: str = _settings.ones.base_url
ONES_EMAIL: str = _settings.ones.email
ONES_PASSWORD: str = _settings.ones.password
ONES_TEAM_ID: str = _settings.ones.team_id
ONES_PROJECT_ID: str = _settings.ones.project_id
ONES_ISSUE_TYPE_ID: str = _settings.ones.issue_type_id

LLM_BASE_URL: str = _settings.llm.base_url
LLM_API_KEY: str = _settings.llm.api_key
LLM_MODEL: str = _settings.llm.model

WECHAT_WEBHOOK_KEY: str = _settings.wechat.webhook_key

EMAIL_SMTP_HOST: str = _settings.email.smtp_host
EMAIL_SMTP_PORT: int = _settings.email.smtp_port
EMAIL_SMTP_USER: str = _settings.email.smtp_user
EMAIL_SMTP_PASSWORD: str = _settings.email.smtp_password
EMAIL_SENDER: str = _settings.email.sender

CODEBASE_PATH: str = _settings.agent.codebase_path
REPO_URL: str = _settings.agent.repo_url
REPO_BRANCH: str = _settings.agent.repo_branch
