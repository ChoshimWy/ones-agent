"""LLM 缺陷分析器 - 代码根因分析"""

from __future__ import annotations

from openai import OpenAI

from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
from src.integrations.codebase import Codebase

SYSTEM_PROMPT_ROOT_CAUSE = """你是一个资深代码缺陷根因分析师。你会收到缺陷信息和相关项目源代码。

分析步骤:
1. 根据缺陷描述定位问题所在代码位置
2. 分析根因（不要只看表面，追到真正原因）
3. 给出具体修改建议，包含代码片段
4. 评估影响范围和修复优先级

输出格式:
### 根因分析
[根因描述]

### 涉及代码
[文件路径 + 行号/范围]

### 修复建议
```代码语言
// 修改前
...

// 修改后
...
```

### 影响范围
[评估]

用简洁中文，Markdown 格式。"""

SYSTEM_PROMPT_BRIEF = """你是一个专业的代码缺陷分析师。分析项目缺陷信息后：
1. 判断缺陷严重程度和可能原因
2. 给出具体的修复建议或代码修改方向
3. 如果信息不足，说明需要哪些额外信息
用简洁的中文回答，使用 Markdown 格式。"""


class Analyzer:
    def __init__(
        self,
        base_url: str = LLM_BASE_URL,
        api_key: str = LLM_API_KEY,
        model: str = LLM_MODEL,
    ):
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model

    def analyze(self, defect: dict, codebase: Codebase | None = None) -> str:
        if codebase:
            return self._root_cause_analysis(defect, codebase)
        return self._brief_analysis(defect)

    def _root_cause_analysis(self, defect: dict, codebase: Codebase) -> str:
        tree = codebase.tree()
        locate_prompt = _format_defect(defect) + f"\n\n### 项目目录结构\n```\n{tree}\n```\n\n请列出最可能与该缺陷相关的 5 个文件路径（每行一个，只写路径）。"
        file_list_resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是代码定位专家。根据缺陷描述和项目目录，列出最可能相关的文件路径。"},
                {"role": "user", "content": locate_prompt},
            ],
            temperature=0.1,
        )
        file_paths = [l.strip().lstrip("-0123456789. ") for l in (file_list_resp.choices[0].message.content or "").splitlines() if l.strip() and not l.startswith("#")]

        code_context = codebase.get_context_for_defect(defect)
        for p in file_paths[:5]:
            if p not in code_context:
                content = codebase.read_file(p)
                if content:
                    code_context[p] = content[:8000]

        code_section = ""
        for path, content in list(code_context.items())[:5]:
            code_section += f"\n#### {path}\n```{_guess_lang(path)}\n{content}\n```\n"

        full_prompt = _format_defect(defect) + f"\n\n### 相关代码\n{code_section}" if code_section else _format_defect(defect)

        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_ROOT_CAUSE},
                {"role": "user", "content": full_prompt},
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content or ""

    def _brief_analysis(self, defect: dict) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_BRIEF},
                {"role": "user", "content": _format_defect(defect)},
            ],
            temperature=0.3,
        )
        return resp.choices[0].message.content or ""

    def batch_analyze(self, defects: list[dict], codebase: Codebase | None = None) -> list[dict]:
        results = []
        for d in defects:
            results.append({
                "id": d.get("uuid", ""),
                "title": d.get("name", ""),
                "status": _name(d.get("status")),
                "priority": _name(d.get("priority")),
                "assignee": _name(d.get("assign")),
                "analysis": self.analyze(d, codebase=codebase),
            })
        return results


def _name(field: dict | None) -> str:
    if not field:
        return ""
    return field.get("name") or field.get("value") or ""


def _guess_lang(path: str) -> str:
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    return {".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
            ".java": "java", ".go": "go", ".rs": "rust", ".vue": "vue", ".html": "html",
            ".css": "css", ".sql": "sql", ".sh": "bash", ".yaml": "yaml", ".xml": "xml"}.get(f".{ext}", ext)


def _format_defect(d: dict) -> str:
    parts = [f"## 缺陷: {d.get('name', '未知')} (#{d.get('number', '')})"]
    if v := _name(d.get("status")):
        parts.append(f"- 状态: {v}")
    if v := _name(d.get("priority")):
        parts.append(f"- 优先级: {v}")
    if v := _name(d.get("assign")):
        parts.append(f"- 负责人: {v}")
    if v := _name(d.get("owner")):
        parts.append(f"- 创建者: {v}")
    if v := _name(d.get("issueType")):
        parts.append(f"- 类型: {v}")
    if v := _name(d.get("project")):
        parts.append(f"- 项目: {v}")
    if v := d.get("deadline"):
        parts.append(f"- 截止日期: {v}")
    if v := d.get("createTime"):
        parts.append(f"- 创建时间: {v}")
    if v := d.get("estimatedHours"):
        parts.append(f"- 预估工时: {v}h")
    if d.get("subTaskCount"):
        parts.append(f"- 子任务: {d['subTaskDoneCount']}/{d['subTaskCount']}")
    return "\n".join(parts)
