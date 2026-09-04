from __future__ import annotations

from dataclasses import replace
from io import StringIO

import pytest
from rich.cells import cell_len
from rich.console import Console
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Static, TabbedContent

from src.developer_workflow.contracts import WorkflowState
from src.developer_workflow.tui import detail_rendering as rendering
from src.developer_workflow.tui.models import (
    HistoryView, PublicationView, RepositoryView, ReviewView, TestView as CommandView, safe_tui_text,
)
from src.developer_workflow.tui.screens import RunDetailPane
from test_developer_workflow_tui_app import NOW, _detail, _summary


def evidence():
    repo = RepositoryView(key="camera-sdk", role="primary", base_commit="a" * 40,
        head_commit="b" * 40, tree_hash="c" * 64,
        changed_files=("effects/very_long_directory_name/effect_pipeline.cpp", "tests/test_camera.py"),
        changed_file_count=3, commit_hash="d" * 40, pushed=True,
        pr_url="https://git.example/team/camera-sdk/pull/42", error="", pr_target="main")
    return replace(_detail(_summary(1)),
        summary=replace(_summary(1), state=WorkflowState.BLOCKED),
        repositories=(repo,), tests=(CommandView("test command", "passed", 0), CommandView("pytest tests/test_camera.py", "test_failed", 1)),
        history=(HistoryView("TESTING", "AI_REVIEW", NOW), HistoryView("AI_REVIEW", "BLOCKED", NOW)),
        publication=PublicationView((repo,), "", ""), resume_state=WorkflowState.AI_REVIEW,
        status_message="The code review found no further repair to apply, but external or platform validation is still missing. See the Review tab; publication remains blocked.",
        review_report=ReviewView("本地测试通过", (), (), ("macOS GPU 实机验证",), False), can_verify=True)


def output(report, width=120):
    stream = StringIO()
    Console(file=stream, width=width, color_system=None).print(report)
    return stream.getvalue()


def test_overview_distinguishes_environment_verification_and_no_code_blockers():
    detail = evidence()
    text = rendering.overview(detail).plain
    assert "已暂停" in text and "BLOCKED" in text
    assert "暂停说明" in text and "仍缺少环境或平台验证" in text
    assert "代码问题：0" in text and "外部验证：1" in text
    assert "环境验证" in text and "尚未生成" in text
    assert "AI_REVIEW" in text and "当前不能发布" not in text  # Preserve the specific environment reason.
    assert "The code review" not in text


@pytest.mark.parametrize("state", list(WorkflowState))
def test_overview_localizes_every_workflow_state_without_hiding_state_code(state):
    detail = replace(_detail(_summary(1)), summary=replace(_summary(1), state=state))
    text = rendering.overview(detail).plain
    assert rendering.STATES[state.value] in text and state.value in text
    assert "任务信息" in text and "下一步" in text


def test_test_commands_do_not_count_as_cases_or_hide_execution_errors():
    detail = replace(evidence(), tests=tuple(CommandView("test command", outcome, code) for outcome, code in (
        ("passed", 0), ("test_failed", 1), ("command_error", 127), ("timeout", -1), ("sandbox_error", -1), ("passed", 2))))
    text = rendering.tests(detail).plain
    for term in ("6 条命令记录", "1 条通过", "5 条需检查", "不是测试用例数", "命令执行异常", "执行超时", "隔离环境异常", "不一致"):
        assert term in text
    assert "没有测试证据不代表测试通过" in rendering.tests(replace(detail, tests=())).plain


def test_repository_facts_and_partial_publication_are_separate():
    detail = evidence()
    repository = rendering.repositories(detail).plain
    for term in ("a" * 40, "b" * 40, "c" * 64, "d" * 40, "tests/test_camera.py", "另有 1 个文件", "主仓库"):
        assert term in repository
    pending = replace(detail.repositories[0], key="desktop", commit_hash="", pushed=False, pr_url="", error="publication failed safely")
    text = rendering.publication(replace(detail, publication=PublicationView((*detail.repositories, pending), "delivered", "publication failed safely"))).plain
    assert "1 / 2 个仓库" in text and "已送达" in text
    assert "发布异常" in text and "发布操作未完成" in text
    assert "全部发布成功" not in text


def test_completed_verification_only_does_not_claim_publication():
    detail = replace(evidence(), summary=replace(_summary(1), state=WorkflowState.COMPLETED),
        review_report=ReviewView("完成本地验证", (), (), (), True), publication=PublicationView((), "", ""))
    assert "本次仅验证当前代码，不执行发布" in rendering.publication(detail).plain
    empty = rendering.publication(_detail(_summary(1))).plain
    assert "尚无发布成功记录" in empty and "未记录送达" in empty


def test_history_keeps_order_and_timezone():
    text = rendering.history(evidence()).plain
    assert text.index("TESTING") < text.index("AI_REVIEW") < text.index("BLOCKED")
    assert "+00:00" in text and "2 次状态变更" in text


@pytest.mark.parametrize("width", [36, 72, 140])
def test_reports_wrap_long_values_without_horizontal_overflow(width):
    detail = evidence()
    for render in (rendering.overview, rendering.repositories, rendering.tests, rendering.publication, rendering.history):
        text = output(render(detail), width)
        assert all(cell_len(line) <= width for line in text.splitlines())


def test_literal_fields_cannot_inject_rich_links_or_styles():
    raw = "[link=https://evil.example]name[/link]"
    value = safe_tui_text(raw)
    result = rendering.literal(value)
    assert result.plain == raw and result.spans == []
    detail = replace(evidence(), status_message=value)
    assert raw in rendering.overview(detail).plain


@pytest.mark.parametrize("size", [(60, 24), (160, 45)])
async def test_five_detail_tabs_scroll_and_refresh_without_changing_selection(size):
    class DetailApp(App):
        def compose(self):
            yield RunDetailPane()

        def on_mount(self):
            self.query_one(RunDetailPane).set_detail(evidence())

    app = DetailApp()
    async with app.run_test(size=size) as pilot:
        pane = app.query_one(RunDetailPane)
        for tab in ("overview", "repositories", "tests", "publication", "history"):
            pane.query_one("#detail-tabs", TabbedContent).active = tab
            await pilot.pause()
            assert isinstance(pane.query_one(f"#{tab}-content", Static).renderable, rendering.DetailReport)
            pane.query_one(f"#{tab}-scroll", VerticalScroll).scroll_end(animate=False)
            pane.set_detail(evidence())
            assert pane.query_one("#detail-tabs", TabbedContent).active == tab
