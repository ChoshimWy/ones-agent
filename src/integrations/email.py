"""邮件通知 - aiosmtplib async"""

from __future__ import annotations

import asyncio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Sequence

import aiosmtplib
import structlog

from config.settings import EmailSettings

log = structlog.get_logger()


class EmailSender:
    def __init__(self, settings: EmailSettings | None = None):
        self._s = settings or EmailSettings()

    @property
    def configured(self) -> bool:
        return bool(self._s.smtp_host and self._s.smtp_user and self._s.sender)

    async def send(
        self,
        to: Sequence[str],
        subject: str,
        html: str,
        text: str = "",
    ) -> None:
        if not self.configured:
            log.warning("email_not_configured")
            return
        msg = MIMEMultipart("alternative")
        msg["From"] = self._s.sender
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if text:
            msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        try:
            await aiosmtplib.send(
                msg,
                hostname=self._s.smtp_host,
                port=self._s.smtp_port,
                username=self._s.smtp_user,
                password=self._s.smtp_password,
                use_tls=self._s.use_tls,
            )
            log.info("email_sent", to=to, subject=subject)
        except Exception as e:
            log.error("email_send_failed", to=to, error=str(e))
            raise
