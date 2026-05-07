"""wechat.py 测试 - mock HTTP"""

from unittest.mock import MagicMock, patch

import pytest


class TestWeChatBot:
    def test_url_contains_key(self):
        from src.integrations.wechat import WeChatBot
        bot = WeChatBot(key="my-key-123")
        assert "my-key-123" in bot.url

    def test_send_markdown(self):
        from src.integrations.wechat import WeChatBot
        with patch("src.integrations.wechat.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"errcode": 0}
            mock_post.return_value.raise_for_status = MagicMock()

            result = WeChatBot(key="k").send_markdown("# Hello")

            mock_post.assert_called_once()
            body = mock_post.call_args.kwargs["json"]
            assert body["msgtype"] == "markdown"
            assert "# Hello" in body["markdown"]["content"]

    def test_send_text(self):
        from src.integrations.wechat import WeChatBot
        with patch("src.integrations.wechat.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"errcode": 0}
            mock_post.return_value.raise_for_status = MagicMock()

            WeChatBot(key="k").send_text("hello", mentioned=["user1"])

            body = mock_post.call_args.kwargs["json"]
            assert body["msgtype"] == "text"
            assert body["text"]["content"] == "hello"
            assert body["text"]["mentioned_list"] == ["user1"]

    def test_send_text_without_mention(self):
        from src.integrations.wechat import WeChatBot
        with patch("src.integrations.wechat.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"errcode": 0}
            mock_post.return_value.raise_for_status = MagicMock()

            WeChatBot(key="k").send_text("hello")

            body = mock_post.call_args.kwargs["json"]
            assert "mentioned_list" not in body["text"]

    def test_send_defect_report_empty(self):
        """无缺陷时发送文本消息"""
        from src.integrations.wechat import WeChatBot
        with patch("src.integrations.wechat.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"errcode": 0}
            mock_post.return_value.raise_for_status = MagicMock()

            WeChatBot(key="k").send_defect_report([])

            body = mock_post.call_args.kwargs["json"]
            assert body["msgtype"] == "text"
            assert "无新缺陷" in body["text"]["content"]

    def test_send_defect_report_with_data(self):
        """有缺陷时发送 Markdown 报告"""
        from src.integrations.wechat import WeChatBot
        with patch("src.integrations.wechat.requests.post") as mock_post:
            mock_post.return_value.json.return_value = {"errcode": 0}
            mock_post.return_value.raise_for_status = MagicMock()

            results = [{
                "title": "登录崩溃",
                "priority": "高",
                "status": "待处理",
                "assignee": "张三",
                "analysis": "建议检查空指针",
            }]
            WeChatBot(key="k").send_defect_report(results)

            body = mock_post.call_args.kwargs["json"]
            assert body["msgtype"] == "markdown"
            assert "登录崩溃" in body["markdown"]["content"]
            assert "张三" in body["markdown"]["content"]
            assert "建议检查空指针" in body["markdown"]["content"]
