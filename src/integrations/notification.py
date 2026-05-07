"""统一通知服务 - 邮件 + 企业微信"""

from __future__ import annotations

from typing import Sequence

import structlog

from config.settings import EmailSettings, WechatSettings
from src.integrations.email import EmailSender
from src.integrations.wechat import WeChatBot

log = structlog.get_logger()


class NotifyTarget:
    """通知目标配置"""
    def __init__(self, emails: Sequence[str] = (), wechat: bool = False):
        self.emails = list(emails)
        self.wechat = wechat


class NotificationService:
    """统一通知 - 一次调用，多渠道分发"""

    def __init__(
        self,
        email_settings: EmailSettings | None = None,
        wechat_settings: WechatSettings | None = None,
    ):
        self._email = EmailSender(email_settings)
        self._wechat = WeChatBot(wechat_settings.webhook_key if wechat_settings else "")

    async def notify(
        self,
        target: NotifyTarget,
        subject: str,
        markdown: str,
    ) -> dict[str, bool]:
        """发送通知到所有已配置渠道，返回各渠道成功状态"""
        results: dict[str, bool] = {}

        if target.emails and self._email.configured:
            try:
                html = _md_to_html(markdown)
                await self._email.send(to=target.emails, subject=subject, html=html, text=markdown)
                results["email"] = True
            except Exception as e:
                log.error("notify_email_failed", error=str(e))
                results["email"] = False

        if target.wechat and self._wechat.key:
            try:
                self._wechat.send_markdown(markdown)
                results["wechat"] = True
            except Exception as e:
                log.error("notify_wechat_failed", error=str(e))
                results["wechat"] = False

        return results


def _md_to_html(md: str) -> str:
    """简易 Markdown → HTML 转换（用于邮件正文）"""
    import re
    html = md
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html, flags=re.MULTILINE)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"`(.+?)`", r"<code>\1</code>", html)
    html = re.sub(r"^---$", "<hr>", html, flags=re.MULTILINE)
    html = re.sub(r"\n", "<br>\n", html)
    return f"<html><body style='font-family:sans-serif'>{html}</body></html>"
