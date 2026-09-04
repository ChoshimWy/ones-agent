"""Inline ONES editor; credentials never populate from saved configuration."""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, Static

from ..setup_models import SecretKind


class OnesSettingsPane(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="ones-settings-form")
        self._loaded = False
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Label("ONES 连接与认证", classes="configuration-description")
        yield Static("直接编辑并保存。账号、密码不回显，留空保留原凭据；更换服务地址须重新填写账号和密码。请在任务空闲时保存，保存会校验并重新加载运行环境。", markup=False)
        for name, label in (("base_url", "服务地址（站点根地址）"), ("team_id", "团队 ID"), ("issue_type_id", "缺陷类型 ID")):
            yield Label(label)
            yield Input(id=f"inline-ones-{name}")
        yield Label("账号 / 邮箱（留空保留）")
        yield Input(id="inline-ones-email", password=True)
        yield Label("密码（留空保留）")
        yield Input(id="inline-ones-password", password=True)
        with Horizontal(id="inline-ones-actions"):
            yield Button("保存并应用", id="inline-ones-save", variant="primary", disabled=True)
            yield Button("重新加载", id="inline-ones-reload")
        yield Static("", id="inline-ones-notice", markup=False)

    async def load(self, *, force: bool = False) -> None:
        if self._busy or (self._loaded and not force):
            return
        self._busy = True
        try:
            fields = await self.app.read_inline_ones()
            for name in ("base_url", "team_id", "issue_type_id"):
                self.query_one(f"#inline-ones-{name}", Input).value = fields.get(f"ones_{name}", "")
            self._clear_credentials()
            self._loaded = True
            self.query_one("#inline-ones-save", Button).disabled = False
            self.query_one("#inline-ones-notice", Static).update("已加载当前配置；修改后点击保存生效。")
        except Exception:
            self.query_one("#inline-ones-save", Button).disabled = True
            self.query_one("#inline-ones-notice", Static).update("当前运行方式无法读取可编辑配置，请检查配置存储是否可用。")
        finally:
            self._busy = False

    def _clear_credentials(self) -> None:
        for name in ("email", "password"):
            self.query_one(f"#inline-ones-{name}", Input).value = ""

    @on(Button.Pressed, "#inline-ones-reload")
    async def reload(self) -> None:
        await self.load(force=True)

    @on(Button.Pressed, "#inline-ones-save")
    def save(self) -> None:
        if self._busy or not self._loaded:
            return
        self._busy = True
        self.app.run_worker(self._save(), group="inline-ones-save", exclusive=False)

    async def _save(self) -> None:
        fields = {f"ones_{name}": self.query_one(f"#inline-ones-{name}", Input).value.strip()
                  for name in ("base_url", "team_id", "issue_type_id")}
        credentials = {kind: self.query_one(f"#inline-ones-{name}", Input).value
                       for kind, name in ((SecretKind.ONES_EMAIL, "email"), (SecretKind.ONES_PASSWORD, "password"))}
        self._clear_credentials()
        self.query_one("#inline-ones-save", Button).disabled = True
        self.query_one("#inline-ones-notice", Static).update("正在校验并保存配置…")
        try:
            await self.app.save_inline_ones(fields, credentials)
        except Exception:
            if self.is_attached:
                self.query_one("#inline-ones-notice", Static).update("配置未应用，请检查连接及必填项后重试。")
        finally:
            credentials.clear()
            self._busy = False
            if self.is_attached:
                self.query_one("#inline-ones-save", Button).disabled = False
