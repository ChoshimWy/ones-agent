from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.contracts import DefectRecord, IdentityRef, PriorityRef, ProjectRef, StatusRef
from src.developer_workflow.contracts import WorkflowRun
from src.developer_workflow.tui.detail_rendering import overview
from src.developer_workflow.tui.models import DefectInfoView, RunDetail
from src.developer_workflow.tui.defect_text import defect_display_text


def snapshot(**updates):
    values = dict(defect_id="defect-123", number="BUG-42", title="启动后预览黑屏",
        status=StatusRef(id="doing", name="处理中"), priority=PriorityRef(value="高"),
        project=ProjectRef(name="桌面应用"), assignee=IdentityRef(name="Tester"),
        description="复现步骤：\n1. 启动程序\n2. 打开预览\n\n预期：正常显示画面", updated_at="2026-09-03T10:00:00Z",
        raw={"token": "RAW-MUST-NOT-LEAK"})
    values.update(updates)
    return DefectRecord(**values)


def detail(record=None, kind="defect"):
    run = (WorkflowRun.new_defect("project", "iteration", "user", "defect-123")
           if kind == "defect" else WorkflowRun.new(kind, "defect-123"))
    return RunDetail.from_run(run.validated_update(defect=record))


def test_overview_reads_stored_defect_details_without_changing_record():
    record = snapshot()
    view = detail(record)
    text = overview(view).plain
    for value in ("缺陷信息", "BUG-42", "启动后预览黑屏", "处理中", "优先级", "桌面应用", "Tester", "打开预览", "不代表实时状态", record.updated_at):
        assert value in text
    assert text.index("缺陷信息") < text.index("任务概览")
    assert "RAW-MUST-NOT-LEAK" not in text
    assert record.description == snapshot().description
    assert not hasattr(view.defect_info, "raw")
    with pytest.raises(FrozenInstanceError):
        view.defect_info.title = "changed"


def test_missing_snapshot_and_empty_fields_have_honest_placeholders():
    assert "尚未读取或保存缺陷详情" in overview(detail()).plain
    assert "defect-123" in overview(detail()).plain
    text = overview(detail(snapshot(title="", description="", number="", assignee=None))).plain
    for value in ("未提供缺陷标题", "未提供缺陷描述", "未记录负责人"):
        assert value in text
    assert "缺陷信息" not in overview(detail(kind="requirement")).plain


def test_html_description_preserves_steps_but_never_renders_resources_or_scripts():
    record = snapshot(description='<p>复现步骤</p><ol><li>启动程序</li><li>打开预览</li></ol>'
        '<script>HIDDEN-SCRIPT</script><style>HIDDEN-STYLE</style><img src="https://example.invalid/secret">'
        '<p>预期：a &lt; b &amp; c</p>')
    view = DefectInfoView.from_record(record)
    assert "• 启动程序\n" in view.description and "• 打开预览" in view.description
    assert "a < b & c" in view.description
    assert not any(value in view.description for value in ("<p>", "HIDDEN", "https://", "<img"))


def test_overview_escapes_markup_redacts_credentials_and_bounds_long_description():
    record = snapshot(title="[bold]literal[/bold] password=TITLE-SECRET",
        description='<p>token=TOKEN-SECRET</p><p>pass&#119;ord=ENTITY-SECRET</p><p>[link=https://bad]literal[/link]</p>'
        + "a" * 9000)
    view = detail(record)
    text = overview(view).plain
    assert "TITLE-SECRET" not in text and "TOKEN-SECRET" not in text and "ENTITY-SECRET" not in text
    assert "[bold]literal[/bold]" in text and "\\[bold]" not in text
    assert "已截断" in view.defect_info.description
    assert len(view.defect_info.description) < 8100
    assert "\x1b" not in defect_display_text("hello\x1b[31m")


def test_defect_status_priority_project_and_assignee_fall_back_to_recorded_ids():
    view = DefectInfoView.from_record(snapshot(status=StatusRef(id="todo"), priority=PriorityRef(id="p1"),
        project=ProjectRef(id="project-id"), assignee=IdentityRef(id="user-id")))
    assert (view.status, view.priority, view.project, view.assignee) == ("todo", "p1", "project-id", "user-id")
