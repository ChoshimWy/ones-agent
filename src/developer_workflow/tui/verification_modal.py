"""Explicit per-check authorization; never execute while the modal is open."""
from __future__ import annotations

from dataclasses import dataclass
from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Select, Static

from .models import RunDetail
from .verification_forms import VerificationNodesModal
from ..verification import public_text


@dataclass(frozen=True)
class VerificationSubmission:
    run_id: str
    version: int
    task_key: str
    actor: str
    evidence: str | None
    passed: bool
    replan: bool = False
    recipe_digest: str = ""
    defer_to_pr: bool = False


class VerificationModal(ModalScreen[VerificationSubmission | None]):
    DEFAULT_CSS = """
    VerificationModal { align: center middle; }
    VerificationModal > VerticalScroll { width: 90%; height: 85%; border: solid $primary; padding: 1 2; background: $surface; }
    VerificationModal Static { height: auto; margin-bottom: 1; }
    """
    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, detail: RunDetail, nodes: tuple[dict, ...]) -> None:
        super().__init__()
        self.detail, self.nodes = detail, nodes

    def compose(self) -> ComposeResult:
        tasks = [task for task in self.detail.verification_tasks if task.status != "passed"]
        with VerticalScroll():
            yield Static("环境验证：选择检查项并确认执行权限", markup=False)
            yield Select([(public_text(task.need.description, 100).replace("\n", " "), task.key) for task in tasks],
                         id="verification-task", allow_blank=False)
            yield Static("", id="verification-description", markup=False)
            yield Select([("在配置节点执行", "execute"), ("记录人工通过证据", "passed"),
                          ("记录人工失败证据", "failed")], id="verification-mode", allow_blank=False)
            yield Input(placeholder="操作人（必填）", id="verification-actor")
            yield Input(placeholder="人工验证：结果、设备与日志/截图位置（必填；必须针对当前快照）", id="verification-evidence")
            yield Checkbox("已确认验收标准与所选脚本匹配，并授权本次执行；人工记录代表我的核验结论", id="verification-consent")
            yield Static("", id="verification-error", markup=False)
            yield Button("确认验证", id="verification-confirm", variant="primary")
            if self.detail.can_defer_verification:
                yield Static("也可先提交 Draft PR，由 PR 审核人完成以下验证。此操作仅生成审批包，推送仍须另行审批。", markup=False)
                yield Button("转交 PR 人工验证", id="verification-defer", variant="warning")
            yield Button("重新规划验证需求（旧任务）", id="verification-replan")
            yield Button("取消", id="verification-cancel")

    def on_mount(self) -> None:
        self._describe()

    @on(Select.Changed, "#verification-task")
    def _selected(self) -> None:
        self._describe()

    def _describe(self) -> None:
        key = self.query_one("#verification-task", Select).value
        task = next((t for t in self.detail.verification_tasks if t.key == key), None)
        if task is None:
            return
        node = next((n for n in self.nodes if n["key"] == task.node_key), {})
        recipe = next((r for r in node.get("recipes", []) if r["key"] == task.recipe_key), {})
        text = (f"检查：{task.need.description}\n能力：{', '.join(task.need.capabilities) or '需要人工确认'}\n"
                f"验收：{task.need.acceptance or task.need.description}\n快照：{task.snapshot_digest}\n"
                f"节点：{task.node_key or '无匹配节点'}　脚本：{task.recipe_key or '无'}\n"
                f"执行参数：{recipe.get('argv', [])}\n"
                "执行会向节点复制 Git 管理的源码及非忽略新增文件，在新目录运行。复制目录不是安全沙箱；"
                "脚本拥有节点账号权限，可能使用设备或网络。请使用专用测试账号。\n"
                "没有匹配节点时，打开 Configuration → 验证节点 Tab，通过“添加节点”填写表单；"
                "也可在目标设备完成人工验证，再记录真实证据。不会自动安装依赖或跳过验证。")
        self.query_one("#verification-description", Static).update(public_text(text, 12000))

    @on(Button.Pressed, "#verification-confirm")
    def _confirm(self) -> None:
        key = self.query_one("#verification-task", Select).value
        task = next((t for t in self.detail.verification_tasks if t.key == key), None)
        mode = self.query_one("#verification-mode", Select).value
        actor = self.query_one("#verification-actor", Input).value.strip()
        evidence = self.query_one("#verification-evidence", Input).value.strip()
        valid = task is not None and actor and self.query_one("#verification-consent", Checkbox).value
        valid = valid and ((mode == "execute" and bool(task.node_key)) or (mode in {"passed", "failed"} and bool(evidence)))
        if not valid:
            self.query_one("#verification-error", Static).update("请填写操作人、授权确认，并选择已配置节点或提供人工证据。")
            return
        self.dismiss(VerificationSubmission(self.detail.summary.run_id, self.detail.summary.version,
            str(key), actor, None if mode == "execute" else evidence, mode != "failed", recipe_digest=task.recipe_digest))

    @on(Button.Pressed, "#verification-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#verification-replan")
    def _replan(self) -> None:
        self.dismiss(VerificationSubmission(self.detail.summary.run_id, self.detail.summary.version,
                                            "", "", None, False, replan=True))

    @on(Button.Pressed, "#verification-defer")
    def _defer(self) -> None:
        if self.detail.can_defer_verification:
            self.dismiss(VerificationSubmission(self.detail.summary.run_id, self.detail.summary.version,
                                                "", "", None, False, defer_to_pr=True))
