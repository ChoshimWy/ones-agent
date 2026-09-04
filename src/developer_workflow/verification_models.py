"""Platform-neutral verification contracts; model suggestions never carry commands."""
from __future__ import annotations

MISSING_BASELINE_DESCRIPTION = "缺少修复前失败复现记录：当前测试通过不能单独证明修复有效。"

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator


class VerificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class VerificationNeed(VerificationModel):
    description: str = Field(min_length=1, max_length=4096)
    capabilities: tuple[str, ...] = Field(default=(), max_length=32)
    acceptance: str = Field(default="", max_length=4096)

    @field_validator("capabilities")
    @classmethod
    def tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,95}", value) for value in values):
            raise ValueError("invalid capability tag")
        return tuple(sorted(set(values)))


class VerificationRecipe(VerificationModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    capabilities: tuple[str, ...] = Field(min_length=1, max_length=32)
    repository_key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    # Trusted configuration, not LLM output. Commands run in an isolated copy.
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    timeout_seconds: StrictInt = Field(default=300, ge=1, le=3600)

    _tags = field_validator("capabilities")(VerificationNeed.tags.__func__)

    @field_validator("argv")
    @classmethod
    def args(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not v or len(v) > 2048 or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("invalid verifier argv")
        return values


class VerificationNode(VerificationModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    enabled: StrictBool = False
    transport: Literal["local", "ssh"] = "ssh"
    capabilities: tuple[str, ...] = Field(default=(), max_length=32)
    ssh_alias: str = Field(default="", pattern=r"^(?:[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127})?$")
    # Use an SSH config alias for host/user/port/identity; no passwords in JSON.
    worker_argv: tuple[str, ...] = Field(default=(), max_length=8)
    recipes: tuple[VerificationRecipe, ...] = Field(default=(), max_length=64)

    _tags = field_validator("capabilities")(VerificationNeed.tags.__func__)

    @field_validator("key")
    @classmethod
    def reserved_key(cls, value: str) -> str:
        if value.casefold() == "manual":
            raise ValueError("manual is reserved for human verification records")
        return value

    @field_validator("worker_argv")
    @classmethod
    def worker(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        # Safe in both OpenSSH POSIX and Windows default remote shells.
        if any(not re.fullmatch(r"[a-zA-Z0-9_./:\\-]+", v) or v.startswith("-") for v in values):
            raise ValueError("worker argv requires simple paths without spaces or shell syntax")
        return values


class VerificationTask(VerificationModel):
    key: str
    need: VerificationNeed
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["waiting_environment", "ready", "running", "passed", "failed", "error", "manual", "stale"]
    node_key: str = ""
    recipe_key: str = ""
    recipe_digest: str = ""


class VerificationRecord(VerificationModel):
    task_key: str
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_digest: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    node_key: str
    recipe_key: str = ""
    recipe_digest: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    status: Literal["passed", "failed", "error"]
    exit_code: StrictInt | None = None
    actor: str = Field(min_length=1, max_length=128)
    evidence: str = Field(max_length=16384)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: str

    @model_validator(mode="after")
    def evidence_consistency(self) -> VerificationRecord:
        if not self.actor.strip() or not self.evidence.strip():
            raise ValueError("verification attribution and evidence are required")
        when = datetime.fromisoformat(self.occurred_at)
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("verification time must be timezone-aware")
        if self.status == "passed" and self.node_key != "manual":
            if self.exit_code != 0 or not self.bundle_digest or not self.recipe_digest:
                raise ValueError("automatic verification requires exit zero and input/recipe digests")
        return self
