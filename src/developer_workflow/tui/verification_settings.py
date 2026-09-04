"""Module configuration: node list and individually persisted detail forms."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, ListItem, ListView, Static

from ..verification import digest
from ..verification_models import VerificationNode
from .controller import TuiController
from .supervisor import RunTaskSupervisor
from .verification_forms import VerificationNodeForm


class VerificationNodeDetails(VerificationNodeForm):
    """Save a single node against the version opened; retain failed edits."""

    def __init__(self, nodes: tuple[dict, ...], index: int | None,
                 repositories: tuple[str, ...], saver: Callable[[str, str], None]) -> None:
        self.snapshot = tuple(VerificationNode.model_validate(node) for node in nodes)
        self.expected_digest = digest(nodes)
        self.index = index
        self.saver = saver
        self.saving = False
        self.delete_confirmed = False
        super().__init__(self.snapshot[index] if index is not None else None,
            used_keys=tuple(node.key for i, node in enumerate(self.snapshot) if i != index),
            repositories=repositories)

    def extra_actions(self) -> ComposeResult:
        if self.index is not None:
            yield Button("移除节点", id="node-delete", variant="warning")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one(".form-title", Label).update("节点详情" if self.source else "添加节点")
        self.query_one("#node-save", Button).label = "保存节点"
        self.query_one("#node-cancel", Button).label = "取消并返回"

    def submit_node(self, node: VerificationNode) -> None:
        self._start_save(node)

    def _start_save(self, node: VerificationNode | None) -> None:
        if self.saving:
            return
        self.saving = True
        self.query_one(Vertical).disabled = True
        self.error("正在保存节点配置…")
        self.run_worker(self._persist(node), exclusive=True)

    async def _persist(self, node: VerificationNode | None) -> None:
        candidate = list(self.snapshot)
        if self.index is None and node is not None:
            candidate.append(node)
        elif self.index is not None:
            if node is None:
                candidate.pop(self.index)
            else:
                candidate[self.index] = node
        try:
            await asyncio.to_thread(self.saver,
                json.dumps([item.model_dump(mode="json") for item in candidate], ensure_ascii=False),
                self.expected_digest)
        except Exception:
            self.error("未保存，输入已保留。请检查保存权限；若配置已被其他操作修改，请取消后重新打开节点。")
        else:
            self.dismiss(True)
        finally:
            self.saving = False
            self.query_one(Vertical).disabled = False

    @on(Button.Pressed, "#node-delete")
    def delete_node(self) -> None:
        if not self.delete_confirmed:
            self.delete_confirmed = True
            self.query_one("#node-delete", Button).label = "确认移除"
            self.error("再次点击“确认移除”将删除此节点配置，不删除远端文件。取消并返回可放弃。")
            return
        self._start_save(None)

    def action_cancel(self) -> None:
        if not self.saving:
            self.dismiss(None)


class VerificationNodesPane(Vertical):
    DEFAULT_CSS = """
    VerificationNodesPane { height: 1fr; }
    VerificationNodesPane .nodes-toolbar { height: auto; min-height: 3; }
    VerificationNodesPane .nodes-toolbar Button { margin-right: 1; }
    VerificationNodesPane #configuration-node-status { height: auto; margin: 1 0; }
    VerificationNodesPane #configuration-node-list { height: 1fr; }
    VerificationNodesPane ListItem { height: auto; padding: 1; border-bottom: solid $primary-background; }
    VerificationNodesPane ListItem Label { width: 100%; height: auto; }
    """

    def __init__(self, controller: TuiController, supervisor: RunTaskSupervisor) -> None:
        super().__init__(id="verification-nodes-pane")
        self.controller = controller
        self.supervisor = supervisor
        self.nodes: tuple[dict, ...] = ()
        self.repositories: tuple[str, ...] = ()
        self.loading = False
        self.loaded = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="nodes-toolbar"):
            yield Button("添加节点", id="configuration-node-add", variant="primary", disabled=True)
            yield Button("刷新列表", id="configuration-node-refresh")
        yield Static("切换到此页后加载节点。", id="configuration-node-status", markup=False)
        yield ListView(id="configuration-node-list")

    async def load_nodes(self) -> None:
        if self.loading:
            return
        self.loading = True
        self.loaded = False
        listing = self.query_one(ListView)
        self.query_one("#configuration-node-add", Button).disabled = True
        listing.disabled = True
        status = self.query_one("#configuration-node-status", Static)
        status.update("正在加载节点…")
        try:
            await listing.clear()
            nodes = await self.supervisor.run_readonly("verification-nodes", self.controller.verification_nodes)
            reader = getattr(self.controller, "verification_repositories", None)
            repositories = await self.supervisor.run_readonly("verification-repositories", reader) if reader else ()
            parsed = tuple(VerificationNode.model_validate(node) for node in nodes)
            self.nodes, self.repositories = tuple(nodes), tuple(repositories)
            for i, node in enumerate(parsed):
                description = (f"{node.key}  ·  {'已启用' if node.enabled else '已禁用'}  ·  "
                    f"{'本机' if node.transport == 'local' else 'SSH'}  ·  {len(node.recipes)} 个脚本\n"
                    f"{', '.join(node.capabilities) or '尚未声明能力'}")
                await listing.append(ListItem(Label(description, markup=False), id=f"configuration-node-{i}"))
            self.loaded = True
            status.update(f"共 {len(parsed)} 个节点 · 点击节点查看详情，或按 Enter 打开。保存配置不会执行验证。"
                          if parsed else "暂无验证节点，点击“添加节点”配置本机或 SSH 远程机器。")
        except Exception:
            self.nodes = ()
            await listing.clear()
            status.update("节点配置加载失败，请刷新重试。未加载成功前不会覆盖已有配置。")
        finally:
            self.loading = False
            listing.disabled = not self.loaded
            self.query_one("#configuration-node-add", Button).disabled = not self.loaded

    def open_node(self, index: int | None) -> None:
        if not self.loaded or self.loading:
            return
        if index is None and len(self.nodes) >= 64:
            self.query_one("#configuration-node-status", Static).update("最多配置 64 个节点。")
            return
        self.app.push_screen(VerificationNodeDetails(self.nodes, index, self.repositories,
            self.controller.save_verification_nodes), callback=self._details_done)

    async def _details_done(self, saved: bool | None) -> None:
        # Cancellation after a concurrent-change rejection must also refresh
        # the snapshot, so reopening details does not repeat the stale save.
        await self.load_nodes()

    @on(Button.Pressed, "#configuration-node-add")
    def add_node(self) -> None:
        self.open_node(None)

    @on(Button.Pressed, "#configuration-node-refresh")
    async def refresh_nodes(self) -> None:
        await self.load_nodes()

    @on(ListView.Selected, "#configuration-node-list")
    def select_node(self, event: ListView.Selected) -> None:
        event.stop()
        index = event.list_view.index
        if index is not None and 0 <= index < len(self.nodes):
            self.open_node(index)
