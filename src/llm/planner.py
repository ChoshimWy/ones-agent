"""LLM 规划器 - 需求解析 & 步骤生成 (litellm)"""

from __future__ import annotations

import json
from pathlib import Path

import litellm
import structlog
from pydantic import BaseModel

from config.settings import LLMSettings

log = structlog.get_logger()

PROMPT_TEMPLATE = (Path(__file__).parent / "prompts" / "planner.md").read_text(encoding="utf-8")


class DevPlan(BaseModel):
    branch_name: str = ""
    steps: list[str] = []
    risk_level: str = "low"
    requires_human_approval: bool = False
    summary: str = ""


class Planner:
    """LLM 规划器

    用法:
        planner = Planner(settings)
        plan = await planner.plan(work_item)
        print(plan.branch_name, plan.steps)
    """

    def __init__(self, settings: LLMSettings | None = None):
        self._settings = settings or LLMSettings()

    async def plan(self, work_item: dict) -> DevPlan:
        prompt = self._build_prompt(work_item)
        log.info("planner_call", work_item=work_item.get("uuid", ""), model=self._settings.model)

        try:
            resp = await litellm.acompletion(
                model=self._settings.model,
                messages=[
                    {"role": "system", "content": PROMPT_TEMPLATE},
                    {"role": "user", "content": prompt},
                ],
                api_key=self._settings.api_key or None,
                api_base=self._settings.base_url,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or ""
            return self._parse(content)
        except Exception as e:
            log.error("planner_failed", error=str(e))
            return self._fallback_plan(work_item, str(e))

    def plan_sync(self, work_item: dict) -> DevPlan:
        prompt = self._build_prompt(work_item)
        try:
            resp = litellm.completion(
                model=self._settings.model,
                messages=[
                    {"role": "system", "content": PROMPT_TEMPLATE},
                    {"role": "user", "content": prompt},
                ],
                api_key=self._settings.api_key or None,
                api_base=self._settings.base_url,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or ""
            return self._parse(content)
        except Exception as e:
            log.error("planner_sync_failed", error=str(e))
            return self._fallback_plan(work_item, str(e))

    def _build_prompt(self, item: dict) -> str:
        parts = [f"## 工作项: {item.get('name', '未知')}"]
        parts.append(f"- UUID: {item.get('uuid', '')}")
        if v := item.get("issueType", {}).get("name"):
            parts.append(f"- 类型: {v}")
        if v := item.get("status", {}).get("name"):
            parts.append(f"- 状态: {v}")
        if v := item.get("priority", {}).get("value") or item.get("priority", {}).get("name"):
            parts.append(f"- 优先级: {v}")
        if v := item.get("assign", {}).get("name"):
            parts.append(f"- 负责人: {v}")
        if v := item.get("project", {}).get("name"):
            parts.append(f"- 项目: {v}")
        if v := item.get("description"):
            parts.append(f"\n### 描述\n{v}")
        return "\n".join(parts)

    def _parse(self, content: str) -> DevPlan:
        try:
            data = json.loads(content)
            return DevPlan(
                branch_name=data.get("branch_name", ""),
                steps=data.get("steps", []),
                risk_level=data.get("risk_level", "low"),
                requires_human_approval=data.get("requires_human_approval", False),
                summary=data.get("summary", ""),
            )
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("planner_parse_failed", error=str(e), content=content[:200])
            return DevPlan(summary=content[:200])

    def _fallback_plan(self, item: dict, error: str) -> DevPlan:
        name = item.get("name", "unknown")
        item_type = item.get("issueType", {}).get("name", "")
        uuid_short = item.get("uuid", "xxx")[:8]
        prefix = "fix" if "缺陷" in item_type or "bug" in item_type.lower() else "feat"
        slug = name.lower().replace(" ", "-")[:20]
        return DevPlan(
            branch_name=f"{prefix}/{uuid_short}-{slug}",
            steps=[f"分析并实现: {name}"],
            risk_level="medium",
            requires_human_approval=True,
            summary=f"LLM 调用失败({error})，使用降级计划",
        )
