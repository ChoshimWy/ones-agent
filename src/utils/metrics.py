"""Prometheus 指标"""

from __future__ import annotations

from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

tasks_total = Counter("agent_tasks_total", "Total tasks processed", ["type", "status"])
failures_total = Counter("agent_failures", "Total failures", ["stage"])
duration_seconds = Histogram("agent_duration_seconds", "Task duration", ["stage"])


def metrics_output() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
