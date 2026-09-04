"""Draft-only forms for verification nodes and recipes; no command execution."""
from __future__ import annotations

import json
import re

from pydantic import ValidationError
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, TextArea

from ..verification import digest, public_text
from ..verification_models import VerificationNode, VerificationRecipe


def _choices(values: tuple[tuple[str, str], ...], selected: str) -> list[tuple[str, str]]:
    result = list(values)
    if selected and selected not in {value for _, value in values}:
        result.append((f"其他：{selected}", selected))
    return result


class CapabilityFields(Vertical):
    """Convenient platform selectors without dropping custom capability tags."""

    def __init__(self, prefix: str, capabilities: tuple[str, ...]) -> None:
        super().__init__(classes="capability-fields")
        self.prefix = prefix
        remaining = list(capabilities)
        self.os_tag = next((tag for tag in remaining if tag.startswith("os:")), "")
        self.arch_tag = next((tag for tag in remaining if tag.startswith("arch:")), "")
        for tag in (self.os_tag, self.arch_tag):
            if tag:
                remaining.remove(tag)
        self.extra = ", ".join(remaining)

    def compose(self) -> ComposeResult:
        yield Label("操作系统", classes="form-label")
        yield Select(_choices((("不限定 / 自定义", ""), ("macOS", "os:macos"),
            ("Windows", "os:windows"), ("Linux", "os:linux")), self.os_tag),
            value=self.os_tag, allow_blank=False, id=f"{self.prefix}-os")
        yield Label("处理器架构", classes="form-label")
        yield Select(_choices((("不限定 / 自定义", ""), ("ARM64", "arch:arm64"),
            ("x86_64", "arch:x86_64")), self.arch_tag), value=self.arch_tag,
            allow_blank=False, id=f"{self.prefix}-arch")
        yield Label("其他能力（逗号分隔，可留空）", classes="form-label")
        yield Input(self.extra, placeholder="device:camera, gpu:opengl, check:camera-output",
                    id=f"{self.prefix}-capabilities")
        yield Static("支持自定义标签，例如 os:freebsd。能力声明仍需验证脚本实际检查。", classes="form-hint", markup=False)

    def values(self) -> tuple[str, ...]:
        tags = [self.query_one(f"#{self.prefix}-os", Select).value,
                self.query_one(f"#{self.prefix}-arch", Select).value]
        extra = self.query_one(f"#{self.prefix}-capabilities", Input).value
        return tuple(str(tag) for tag in tags if isinstance(tag, str) and tag) + tuple(
            tag for tag in re.split(r"[,，\s]+", extra.strip()) if tag)


class VerificationEditor(ModalScreen):
    DEFAULT_CSS = """
    VerificationEditor { align: center middle; }
    VerificationEditor > Vertical { width: 94%; max-width: 110; height: 92%; border: solid $primary; padding: 1 2; background: $surface; }
    VerificationEditor VerticalScroll { height: 1fr; }
    VerificationEditor .capability-fields, VerificationEditor .ssh-fields { height: auto; }
    VerificationEditor .form-label { height: auto; margin-top: 1; text-style: bold; }
    VerificationEditor .form-title { height: auto; text-style: bold; color: $primary; }
    VerificationEditor Static { height: auto; }
    VerificationEditor .form-hint { color: $text-muted; margin-bottom: 1; }
    VerificationEditor TextArea { height: 5; }
    VerificationEditor Horizontal { height: auto; min-height: 3; }
    VerificationEditor Horizontal Button { width: 1fr; min-width: 8; }
    VerificationEditor .form-error { color: $error; height: auto; }
    """
    BINDINGS = [("escape", "cancel", "取消")]

    def error(self, message: str, field: str | None = None) -> None:
        self.query_one(".form-error", Static).update(message)
        if field:
            self.query_one(f"#{field}").focus()

    def action_cancel(self) -> None:
        self.dismiss(None)


class VerificationRecipeForm(VerificationEditor):
    def __init__(self, recipe: VerificationRecipe | None = None, *, used_keys: tuple[str, ...] = (),
                 capabilities: tuple[str, ...] = (), repositories: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.source = recipe
        self.used_keys = used_keys
        self.capabilities = recipe.capabilities if recipe else capabilities
        self.repositories = repositories

    def compose(self) -> ComposeResult:
        recipe = self.source
        argv = recipe.argv if recipe else ("python", "-m", "pytest")
        repository_key = recipe.repository_key if recipe else (self.repositories[0] if self.repositories else "")
        with Vertical():
            yield Label("编辑验证脚本" if recipe else "新增验证脚本", classes="form-title")
            with VerticalScroll():
                yield Label("脚本标识 *", classes="form-label")
                yield Input(recipe.key if recipe else "", placeholder="camera-regression", id="recipe-key")
                yield Label("仓库标识 *", classes="form-label")
                if self.repositories:
                    yield Select(_choices(tuple((key, key) for key in self.repositories), repository_key),
                        value=repository_key, allow_blank=False, id="recipe-repository")
                else:
                    yield Input(repository_key, placeholder="与工作区中的仓库 key 一致", id="recipe-repository")
                yield CapabilityFields("recipe", self.capabilities)
                yield Label("程序 / 解释器路径 *", classes="form-label")
                yield Input(argv[0], placeholder="C:/test env/Scripts/python.exe", id="recipe-program")
                yield Label("参数（每行一个，不加外层引号）", classes="form-label")
                yield TextArea("\n".join(argv[1:]), id="recipe-arguments")
                yield Static("例如三行：-m、pytest、tests/test_camera.py。路径中的空格保留，不按空格拆分。", classes="form-hint", markup=False)
                yield Label("超时时间（秒，1–3600） *", classes="form-label")
                yield Input(str(recipe.timeout_seconds if recipe else 300), type="integer", id="recipe-timeout")
            yield Static("", classes="form-error", markup=False)
            with Horizontal():
                yield Button("应用到节点草稿", id="recipe-save", variant="primary")
                yield Button("取消", id="recipe-cancel")

    @on(Button.Pressed, "#recipe-save")
    def save(self) -> None:
        key = self.query_one("#recipe-key", Input).value.strip()
        if key in self.used_keys:
            self.error("该节点中已有同名脚本，请换一个标识。", "recipe-key")
            return
        repository = self.query_one("#recipe-repository")
        try:
            timeout = int(self.query_one("#recipe-timeout", Input).value)
        except ValueError:
            self.error("超时时间必须是 1–3600 的整数。", "recipe-timeout")
            return
        try:
            recipe = VerificationRecipe(key=key, repository_key=str(repository.value).strip(),
                capabilities=self.query_one(CapabilityFields).values(),
                argv=(self.query_one("#recipe-program", Input).value,
                      *self.query_one("#recipe-arguments", TextArea).text.splitlines()), timeout_seconds=timeout)
        except ValidationError as error:
            field = str(error.errors(include_input=False)[0]["loc"][0])
            message, target = {
                "key": ("脚本标识须为英文、数字、点、短横线或下划线，且不能以符号开头。", "recipe-key"),
                "repository_key": ("请选择或填写有效的仓库标识。", "recipe-repository"),
                "capabilities": ("至少选择一项能力；标签须用小写英文、数字及 . _ : -。", "recipe-capabilities"),
                "argv": ("程序不能为空；参数不能包含空行或控制字符，最多 64 项。", "recipe-program"),
                "timeout_seconds": ("超时时间必须在 1–3600 秒之间。", "recipe-timeout"),
            }.get(field, ("请检查脚本配置。", None))
            self.error(message, target)
            return
        self.dismiss(recipe)

    @on(Button.Pressed, "#recipe-cancel")
    def cancel(self) -> None:
        self.action_cancel()


class VerificationNodeForm(VerificationEditor):
    def __init__(self, node: VerificationNode | None = None, *, used_keys: tuple[str, ...] = (),
                 repositories: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.source, self.used_keys, self.repositories = node, used_keys, repositories
        self.recipes = list(node.recipes) if node else []

    def compose(self) -> ComposeResult:
        node = self.source
        worker = node.worker_argv if node else ()
        with Vertical():
            yield Label("编辑验证节点" if node else "新增验证节点", classes="form-title")
            with VerticalScroll():
                yield Label("节点标识 *", classes="form-label")
                yield Input(node.key if node else "", placeholder="mac-lab / windows-test", id="node-key")
                yield Checkbox("启用此节点（每次执行仍需确认）", value=node.enabled if node else False, id="node-enabled")
                yield Label("连接方式", classes="form-label")
                yield Select([("本机", "local"), ("SSH 远程机器", "ssh")], value=node.transport if node else "local",
                             allow_blank=False, id="node-transport")
                with Vertical(classes="ssh-fields", id="node-ssh-fields"):
                    yield Label("SSH 配置别名 *", classes="form-label")
                    yield Input(node.ssh_alias if node else "", placeholder="~/.ssh/config 中的 Host 别名，例如 mac-validation", id="node-ssh-alias")
                    yield Label("远端 Python / 启动程序路径 *", classes="form-label")
                    yield Input(worker[0] if worker else "", placeholder="/usr/bin/python3", id="node-worker-program")
                    yield Label("远端 worker 脚本路径及参数（每行一项）", classes="form-label")
                    yield TextArea("\n".join(worker[1:]), id="node-worker-arguments")
                    yield Static("使用 SSH 密钥与已核验的主机指纹；不要填写密码。远端启动路径暂不支持空格或 shell 语法。", classes="form-hint", markup=False)
                yield Label("运行环境与能力", classes="form-label")
                yield CapabilityFields("node", node.capabilities if node else ())
                yield Label("验证脚本", classes="form-label")
                yield Select([(recipe.key, index) for index, recipe in enumerate(self.recipes)],
                             prompt="尚未选择脚本", id="node-recipes")
                with Horizontal():
                    yield Button("新增脚本", id="recipe-add")
                    yield Button("编辑脚本", id="recipe-edit")
                    yield Button("移除脚本", id="recipe-remove", variant="warning")
                yield Static("脚本需与验收标准匹配。未配置脚本的节点不能自动执行验证。", classes="form-hint", markup=False)
            yield Static("", classes="form-error", markup=False)
            with Horizontal():
                yield Button("应用到配置草稿", id="node-save", variant="primary")
                yield Button("取消", id="node-cancel")
                yield from self.extra_actions()

    def extra_actions(self) -> ComposeResult:
        return iter(())

    def on_mount(self) -> None:
        self.transport_changed()

    @on(Select.Changed, "#node-transport")
    def transport_changed(self) -> None:
        self.query_one("#node-ssh-fields").display = self.query_one("#node-transport", Select).value == "ssh"

    def recipe_index(self) -> int | None:
        value = self.query_one("#node-recipes", Select).value
        return value if type(value) is int and 0 <= value < len(self.recipes) else None

    def edit_recipe(self, index: int | None) -> None:
        if index is None and len(self.recipes) >= 64:
            self.error("每个节点最多配置 64 个脚本。")
            return
        self.app.push_screen(VerificationRecipeForm(self.recipes[index] if index is not None else None,
            used_keys=tuple(recipe.key for i, recipe in enumerate(self.recipes) if i != index),
            capabilities=self.query_one(CapabilityFields).values(), repositories=self.repositories),
            callback=lambda result: self.recipe_done(index, result))

    def recipe_done(self, index: int | None, result: VerificationRecipe | None) -> None:
        if result is None:
            return
        if index is None:
            self.recipes.append(result)
            index = len(self.recipes) - 1
        else:
            self.recipes[index] = result
        self.refresh_recipes(index)

    def refresh_recipes(self, index: int | None = None) -> None:
        select = self.query_one("#node-recipes", Select)
        select.set_options((recipe.key, i) for i, recipe in enumerate(self.recipes))
        select.value = index if index is not None else Select.BLANK

    @on(Button.Pressed, "#recipe-add")
    def add_recipe(self) -> None:
        self.edit_recipe(None)

    @on(Button.Pressed, "#recipe-edit")
    def change_recipe(self) -> None:
        index = self.recipe_index()
        if index is None:
            self.error("请先选择要编辑的脚本。")
            return
        self.edit_recipe(index)

    @on(Button.Pressed, "#recipe-remove")
    def remove_recipe(self) -> None:
        index = self.recipe_index()
        if index is None:
            self.error("请先选择要移除的脚本。")
            return
        self.recipes.pop(index)
        self.refresh_recipes()

    @on(Button.Pressed, "#node-save")
    def save(self) -> None:
        key = self.query_one("#node-key", Input).value.strip()
        if key in self.used_keys:
            self.error("已有同名节点，请换一个标识。", "node-key")
            return
        transport = self.query_one("#node-transport", Select).value
        enabled = self.query_one("#node-enabled", Checkbox).value
        program = self.query_one("#node-worker-program", Input).value
        args = self.query_one("#node-worker-arguments", TextArea).text.splitlines()
        alias = self.query_one("#node-ssh-alias", Input).value.strip()
        if transport == "ssh" and enabled and (not alias or not program):
            self.error("启用 SSH 节点前，请填写连接别名和远端启动程序；使用 Python 时还需填写 worker 脚本路径。", "node-ssh-alias")
            return
        try:
            node = VerificationNode(key=key, enabled=enabled, transport=transport, ssh_alias=alias,
                worker_argv=(program, *args) if program or args else (),
                capabilities=self.query_one(CapabilityFields).values(), recipes=tuple(self.recipes))
        except ValidationError as error:
            field = str(error.errors(include_input=False)[0]["loc"][0])
            message, target = {
                "key": ("节点标识格式无效：用英文数字开头，可含 . _ -；manual 为保留名称。", "node-key"),
                "capabilities": ("能力标签须用小写英文、数字及 . _ : -，最多 32 项。", "node-capabilities"),
                "ssh_alias": ("请填写有效的 SSH 配置别名，不要填写命令或密码。", "node-ssh-alias"),
                "worker_argv": ("远端启动程序和参数不能含空格或 shell 语法，最多 8 项。", "node-worker-program"),
            }.get(field, ("请检查节点配置。", None))
            if field in {"ssh_alias", "worker_argv"}:
                # Retain connection drafts when switching transports, and never
                # focus a hidden invalid field without revealing it first.
                self.query_one("#node-ssh-fields").display = True
            self.error(message, target)
            return
        self.submit_node(node)

    def submit_node(self, node: VerificationNode) -> None:
        self.dismiss(node)

    @on(Button.Pressed, "#node-cancel")
    def cancel(self) -> None:
        self.action_cancel()


class VerificationNodesModal(VerificationEditor):
    """All edits stay in a private draft until the final explicit save."""

    def __init__(self, nodes: tuple[dict, ...], repositories: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.original_digest = digest(nodes)
        self.nodes = [VerificationNode.model_validate(node) for node in nodes]
        self.repositories = repositories

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("验证节点配置", classes="form-title")
            with VerticalScroll():
                yield Static("选择节点后编辑，或新增本机 / SSH 远程节点。保存配置不会连接机器或执行脚本。", markup=False)
                yield Select([(self.node_label(node), i) for i, node in enumerate(self.nodes)],
                             prompt="请选择节点；没有节点时点击新增", id="verification-node-list")
                with Horizontal():
                    yield Button("新增节点", id="verification-node-add", variant="primary")
                    yield Button("编辑节点", id="verification-node-edit")
                    yield Button("移除节点", id="verification-node-remove", variant="warning")
                yield Static("", id="verification-node-summary", markup=False)
                yield Static("所有编辑、移除操作先保留在草稿中。点击下方保存后生效；取消会放弃本次全部更改。", classes="form-hint", markup=False)
            yield Static("", classes="form-error", markup=False)
            with Horizontal():
                yield Button("保存全部配置", id="verification-nodes-save", variant="primary")
                yield Button("取消", id="verification-nodes-cancel")

    @staticmethod
    def node_label(node: VerificationNode) -> str:
        return f"{node.key} · {'已启用' if node.enabled else '已禁用'} · {'本机' if node.transport == 'local' else 'SSH'}"

    def index(self) -> int | None:
        value = self.query_one("#verification-node-list", Select).value
        return value if type(value) is int and 0 <= value < len(self.nodes) else None

    @on(Select.Changed, "#verification-node-list")
    def selected(self) -> None:
        index = self.index()
        node = self.nodes[index] if index is not None else None
        summary = (f"能力：{', '.join(node.capabilities) or '尚未声明'}\n验证脚本：{len(node.recipes)} 个"
                   if node else f"已配置 {len(self.nodes)} 个节点。")
        self.query_one("#verification-node-summary", Static).update(public_text(summary))

    def edit(self, index: int | None) -> None:
        if index is None and len(self.nodes) >= 64:
            self.error("最多配置 64 个验证节点。")
            return
        self.app.push_screen(VerificationNodeForm(self.nodes[index] if index is not None else None,
            used_keys=tuple(node.key for i, node in enumerate(self.nodes) if i != index), repositories=self.repositories),
            callback=lambda result: self.node_done(index, result))

    def node_done(self, index: int | None, result: VerificationNode | None) -> None:
        if result is None:
            return
        if index is None:
            self.nodes.append(result)
            index = len(self.nodes) - 1
        else:
            self.nodes[index] = result
        self.refresh_nodes(index)

    def refresh_nodes(self, index: int | None = None) -> None:
        select = self.query_one("#verification-node-list", Select)
        select.set_options((self.node_label(node), i) for i, node in enumerate(self.nodes))
        select.value = index if index is not None else Select.BLANK
        self.selected()

    @on(Button.Pressed, "#verification-node-add")
    def add(self) -> None:
        self.edit(None)

    @on(Button.Pressed, "#verification-node-edit")
    def change(self) -> None:
        index = self.index()
        if index is None:
            self.error("请先选择要编辑的节点。")
            return
        self.edit(index)

    @on(Button.Pressed, "#verification-node-remove")
    def remove_node(self) -> None:
        index = self.index()
        if index is None:
            self.error("请先选择要移除的节点。")
            return
        self.nodes.pop(index)
        self.refresh_nodes()

    @on(Button.Pressed, "#verification-nodes-save")
    def save(self) -> None:
        self.dismiss((json.dumps([node.model_dump(mode="json") for node in self.nodes], ensure_ascii=False), self.original_digest))

    @on(Button.Pressed, "#verification-nodes-cancel")
    def cancel(self) -> None:
        self.action_cancel()
