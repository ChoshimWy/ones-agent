"""凭证加载与脱敏"""

from __future__ import annotations

import re

_SENSITIVE_KEYS = {"password", "secret", "token", "key", "pat", "api_key", "webhook_key"}


def mask_value(v: str, visible: int = 4) -> str:
    if len(v) <= visible:
        return "***"
    return v[:visible] + "***"


def mask_dict(d: dict) -> dict:
    return {k: mask_value(str(v)) if k.lower() in _SENSITIVE_KEYS else v for k, v in d.items()}


def mask_string(s: str) -> str:
    for pattern in [r"(Bearer\s+)\S+", r"(token[=:]\s*)\S+", r"(key[=:]\s*)\S+"]:
        s = re.sub(pattern, r"\1***", s, flags=re.IGNORECASE)
    return s
