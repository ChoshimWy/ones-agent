"""analyzer.py 测试"""

from unittest.mock import MagicMock, patch

import pytest


MOCK_DEFECT = {
    "uuid": "d1", "name": "登录崩溃", "number": 101,
    "status": {"uuid": "s1", "name": "待处理", "category": "todo"},
    "priority": {"uuid": "p1", "value": "高", "position": 1},
    "assign": {"uuid": "u1", "name": "张三", "avatar": ""},
    "owner": {"uuid": "u2", "name": "李四", "avatar": ""},
    "issueType": {"uuid": "it1", "name": "缺陷"},
    "project": {"uuid": "proj1", "name": "主项目"},
    "createTime": 1700000000, "deadline": "2026-05-01",
    "estimatedHours": 4.0, "subTaskCount": 2, "subTaskDoneCount": 1,
}


def _mock_openai_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


class TestFormatDefect:
    def test_basic_format(self):
        from src.llm.analyzer import _format_defect
        result = _format_defect(MOCK_DEFECT)
        assert "登录崩溃" in result
        assert "#101" in result
        assert "待处理" in result

    def test_minimal_defect(self):
        from src.llm.analyzer import _format_defect
        result = _format_defect({"name": "空缺陷"})
        assert "空缺陷" in result


class TestNameHelper:
    def test_name_from_name_field(self):
        from src.llm.analyzer import _name
        assert _name({"name": "张三"}) == "张三"

    def test_name_from_value_field(self):
        from src.llm.analyzer import _name
        assert _name({"value": "高"}) == "高"

    def test_name_none(self):
        from src.llm.analyzer import _name
        assert _name(None) == ""


class TestAnalyzerBrief:
    """无代码上下文时的简要分析"""

    @patch("src.llm.analyzer.OpenAI")
    def test_brief_analysis(self, MockOpenAI):
        from src.llm.analyzer import Analyzer
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _mock_openai_response("建议修复X")
        MockOpenAI.return_value = mock_client

        result = Analyzer().analyze(MOCK_DEFECT)
        assert result == "建议修复X"


class TestAnalyzerRootCause:
    """有代码上下文时的根因分析"""

    @patch("src.llm.analyzer.OpenAI")
    def test_root_cause_with_codebase(self, MockOpenAI):
        from src.llm.analyzer import Analyzer
        mock_client = MagicMock()
        # 第一次调用: 定位文件; 第二次调用: 根因分析
        mock_client.chat.completions.create.side_effect = [
            _mock_openai_response("src/login.py\nsrc/auth.py"),
            _mock_openai_response("### 根因分析\n空指针导致崩溃"),
        ]
        MockOpenAI.return_value = mock_client

        mock_codebase = MagicMock()
        mock_codebase.tree.return_value = "src/\n  login.py\n  auth.py"
        mock_codebase.get_context_for_defect.return_value = {"src/login.py": "def login():\n    pass"}
        mock_codebase.read_file.return_value = "def login():\n    pass"

        result = Analyzer().analyze(MOCK_DEFECT, codebase=mock_codebase)
        assert "根因分析" in result
        assert mock_client.chat.completions.create.call_count == 2

    @patch("src.llm.analyzer.OpenAI")
    def test_batch_analyze_with_codebase(self, MockOpenAI):
        from src.llm.analyzer import Analyzer
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            _mock_openai_response("src/main.py"),
            _mock_openai_response("根因: X"),
        ]
        MockOpenAI.return_value = mock_client

        mock_codebase = MagicMock()
        mock_codebase.tree.return_value = "src/main.py"
        mock_codebase.get_context_for_defect.return_value = {}

        results = Analyzer().batch_analyze([MOCK_DEFECT], codebase=mock_codebase)
        assert len(results) == 1
        assert results[0]["title"] == "登录崩溃"
