"""Backward-compatible lazy exports for legacy configuration constants."""

from __future__ import annotations

from threading import Lock
from typing import Any


globals().pop("_settings", None)


__all__ = [
    "ONES_BASE_URL", "ONES_EMAIL", "ONES_PASSWORD", "ONES_TEAM_ID",
    "ONES_PROJECT_ID", "ONES_ISSUE_TYPE_ID", "LLM_BASE_URL", "LLM_API_KEY",
    "LLM_MODEL", "WECHAT_WEBHOOK_KEY", "EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT",
    "EMAIL_SMTP_USER", "EMAIL_SMTP_PASSWORD", "EMAIL_SENDER", "CODEBASE_PATH",
    "REPO_URL", "REPO_BRANCH",
]

_legacy_settings_lock = Lock()
_LEGACY_FIELDS = {
    "ONES_BASE_URL": ("ones", "base_url"), "ONES_EMAIL": ("ones", "email"),
    "ONES_PASSWORD": ("ones", "password"), "ONES_TEAM_ID": ("ones", "team_id"),
    "ONES_PROJECT_ID": ("ones", "project_id"), "ONES_ISSUE_TYPE_ID": ("ones", "issue_type_id"),
    "LLM_BASE_URL": ("llm", "base_url"), "LLM_API_KEY": ("llm", "api_key"),
    "LLM_MODEL": ("llm", "model"), "WECHAT_WEBHOOK_KEY": ("wechat", "webhook_key"),
    "EMAIL_SMTP_HOST": ("email", "smtp_host"), "EMAIL_SMTP_PORT": ("email", "smtp_port"),
    "EMAIL_SMTP_USER": ("email", "smtp_user"), "EMAIL_SMTP_PASSWORD": ("email", "smtp_password"),
    "EMAIL_SENDER": ("email", "sender"), "CODEBASE_PATH": ("agent", "codebase_path"),
    "REPO_URL": ("agent", "repo_url"), "REPO_BRANCH": ("agent", "repo_branch"),
}


def _legacy_settings() -> Any:
    try:
        return _settings
    except NameError:
        with _legacy_settings_lock:
            try:
                return _settings
            except NameError:
                from config.settings import Settings

                settings = Settings()
                globals()["_settings"] = settings
                return settings


def __getattr__(name: str) -> Any:
    try:
        section, field = _LEGACY_FIELDS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(getattr(_legacy_settings(), section), field)
