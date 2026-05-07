"""Phase 6 测试 - 安全加固与可观测性"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.utils.audit import AuditLog
from src.utils.retry import aretry
from src.utils.metrics import tasks_total, failures_total, metrics_output


class TestAuditLog:
    def test_record_creates_file(self, tmp_path):
        audit = AuditLog(log_dir=str(tmp_path / "audit"))
        audit.record(actor="agent", action="commit", target="item-1", result="ok")

        files = list((tmp_path / "audit").glob("*.jsonl"))
        assert len(files) == 1

    def test_record_content(self, tmp_path):
        audit = AuditLog(log_dir=str(tmp_path / "audit"))
        audit.record(actor="agent", action="push", target="branch-1", result="success")

        files = list((tmp_path / "audit").glob("*.jsonl"))
        content = files[0].read_text()
        entry = json.loads(content.strip())
        assert entry["actor"] == "agent"
        assert entry["action"] == "push"
        assert entry["target"] == "branch-1"
        assert entry["result"] == "success"
        assert "timestamp" in entry

    def test_record_with_extra(self, tmp_path):
        audit = AuditLog(log_dir=str(tmp_path / "audit"))
        audit.record(actor="agent", action="analyze", target="item-1", extra={"risk": "high"})

        files = list((tmp_path / "audit").glob("*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["risk"] == "high"

    def test_multiple_records_append(self, tmp_path):
        audit = AuditLog(log_dir=str(tmp_path / "audit"))
        audit.record(actor="a1", action="x", target="t1")
        audit.record(actor="a2", action="y", target="t2")

        files = list((tmp_path / "audit").glob("*.jsonl"))
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 2


class TestAsyncRetry:
    @pytest.mark.asyncio
    async def test_aretry_success(self):
        call_count = 0

        @aretry(max_retries=3, backoff_factor=0)
        async def ok():
            nonlocal call_count
            call_count += 1
            return "done"

        result = await ok()
        assert result == "done"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_aretry_recovers(self):
        call_count = 0

        @aretry(max_retries=3, backoff_factor=0, retry_on=(ValueError,))
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "ok"

        result = await flaky()
        assert result == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_aretry_exhausted(self):
        @aretry(max_retries=2, backoff_factor=0, retry_on=(ValueError,))
        async def always_fail():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            await always_fail()


class TestMetrics:
    def test_metrics_output(self):
        from src.utils.metrics import metrics_output
        data, content_type = metrics_output()
        assert isinstance(data, bytes)
        assert "agent_tasks_total" in data.decode()

    def test_tasks_total_counter(self):
        before = tasks_total._value.get() if hasattr(tasks_total, '_value') else 0
        tasks_total.labels(type="defect", status="queued").inc()
        data, _ = metrics_output()
        assert "agent_tasks_total" in data.decode()

    def test_failures_counter(self):
        failures_total.labels(stage="webhook").inc()
        data, _ = metrics_output()
        assert "agent_failures" in data.decode()


class TestMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_metrics_endpoint(self):
        from httpx import ASGITransport, AsyncClient
        from main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/metrics")
        assert resp.status_code == 200
        assert "agent_tasks_total" in resp.text


class TestAuditMiddleware:
    @pytest.mark.asyncio
    async def test_post_audited(self, tmp_path):
        from httpx import ASGITransport, AsyncClient
        from main import app, audit, settings

        original_dir = audit._log_dir
        original_secret = settings.agent.webhook_secret
        audit._log_dir = tmp_path / "audit"
        settings.agent.webhook_secret = ""

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/webhook/ones", json={"work_item_id": "item-1", "type": "defect"})

        assert resp.status_code == 200

        files = list((tmp_path / "audit").glob("*.jsonl"))
        assert len(files) >= 1
        content = files[0].read_text()
        entry = json.loads(content.strip().split("\n")[-1])
        assert entry["action"] in ("POST", "enqueue")

        audit._log_dir = original_dir
        settings.agent.webhook_secret = original_secret

    @pytest.mark.asyncio
    async def test_get_not_audited(self, tmp_path):
        from httpx import ASGITransport, AsyncClient
        from main import app, audit

        original_dir = audit._log_dir
        audit._log_dir = tmp_path / "audit"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            await ac.get("/health")

        files = list((tmp_path / "audit").glob("*.jsonl"))
        if files:
            for line in files[0].read_text().strip().split("\n"):
                entry = json.loads(line)
                assert entry["action"] != "GET"

        audit._log_dir = original_dir
