"""企业微信 Webhook 推送"""

from __future__ import annotations

import requests

from config import WECHAT_WEBHOOK_KEY

WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


class WeChatBot:
    def __init__(self, key: str = WECHAT_WEBHOOK_KEY):
        self.key = key

    @property
    def url(self) -> str:
        return f"{WEBHOOK_URL}?key={self.key}"

    def send_markdown(self, content: str) -> dict:
        resp = requests.post(
            self.url,
            json={"msgtype": "markdown", "markdown": {"content": content}},
        )
        resp.raise_for_status()
        return resp.json()

    def send_text(self, content: str, mentioned: list[str] | None = None) -> dict:
        body: dict = {"msgtype": "text", "text": {"content": content}}
        if mentioned:
            body["text"]["mentioned_list"] = mentioned
        resp = requests.post(self.url, json=body)
        resp.raise_for_status()
        return resp.json()

    def send_defect_report(self, results: list[dict]) -> dict:
        if not results:
            return self.send_text("✅ 当前无新缺陷")
        parts = ["## 🐛 缺陷分析报告\n"]
        for r in results:
            parts.append(
                f"**{r['title']}** `{r.get('priority', '')}` `{r.get('status', '')}`\n"
                f"> 负责人: {r.get('assignee', '未分配')}\n\n"
                f"{r['analysis']}\n---\n"
            )
        return self.send_markdown("\n".join(parts))
