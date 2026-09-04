from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Button, Checkbox, Input, Select, Static, TextArea

from src.developer_workflow.tui.controller import TuiController
from src.developer_workflow.tui.verification_forms import (
    VerificationNodeForm, VerificationNodesModal, VerificationRecipeForm,
)
from src.developer_workflow.verification import digest
from src.developer_workflow.verification_models import VerificationNode, VerificationRecipe


class FormApp(App):
    def __init__(self, modal):
        super().__init__()
        self.modal = modal
        self.results = []

    def on_mount(self):
        self.push_screen(self.modal, self.results.append)


async def press(app, pilot, selector):
    # Textual suppresses presses while a button's active animation is running.
    await pilot.pause(0.35)
    app.screen.query_one(selector, Button).focus()
    await pilot.press("enter")
    await pilot.pause()


def existing_node():
    return VerificationNode(key="remote", enabled=True, transport="ssh", ssh_alias="lab",
        worker_argv=("/usr/bin/python3", "/opt/worker.py"),
        capabilities=("os:freebsd", "arch:riscv64", "device:camera"),
        recipes=(VerificationRecipe(key="camera", repository_key="unlisted",
            capabilities=("os:freebsd", "check:camera"),
            argv=(r"C:\test env\python.exe", "-m", "pytest", "tests/test camera.py", " leading space "),
            timeout_seconds=123),))


@pytest.mark.parametrize("size", [(120, 42), (80, 24)])
async def test_add_node_and_recipe_form_end_to_end(size):
    modal = VerificationNodesModal((), repositories=("app", "camera-sdk"))
    app = FormApp(modal)
    async with app.run_test(size=size) as pilot:
        await press(app, pilot, "#verification-node-add")
        form = app.screen
        assert isinstance(form, VerificationNodeForm)
        assert not form.query_one("#node-ssh-fields").display
        assert not form.query_one("#node-enabled", Checkbox).value
        form.query_one("#node-key", Input).value = "windows-test"
        form.query_one("#node-os", Select).value = "os:windows"
        form.query_one("#node-arch", Select).value = "arch:x86_64"
        form.query_one("#node-capabilities", Input).value = "device:camera, check:camera"
        await press(app, pilot, "#recipe-add")
        recipe = app.screen
        assert isinstance(recipe, VerificationRecipeForm)
        recipe.query_one("#recipe-key", Input).value = "camera"
        recipe.query_one("#recipe-repository", Select).value = "camera-sdk"
        recipe.query_one("#recipe-program", Input).value = r"C:\test env\python.exe"
        recipe.query_one("#recipe-arguments", TextArea).load_text("-m\npytest\ntests/test camera.py\n")
        await press(app, pilot, "#recipe-save")
        assert app.screen is form and not app.results and not modal.nodes
        await press(app, pilot, "#node-save")
        assert app.screen is modal and not app.results
        # Final action remains visible even on a short terminal, outside scroll.
        button = modal.query_one("#verification-nodes-save", Button)
        assert 0 <= button.region.y < size[1] and button.region.bottom <= size[1]
        await pilot.click("#verification-nodes-save")
        await pilot.pause()
        raw, original = app.results[0]
        assert original == digest(())
        saved = VerificationNode.model_validate(json.loads(raw)[0])
        assert saved.key == "windows-test" and not saved.enabled
        assert saved.transport == "local"
        assert saved.capabilities == ("arch:x86_64", "check:camera", "device:camera", "os:windows")
        assert saved.recipes[0].repository_key == "camera-sdk"
        assert saved.recipes[0].argv == (r"C:\test env\python.exe", "-m", "pytest", "tests/test camera.py")


async def test_edit_round_trip_preserves_custom_capabilities_and_command_arguments():
    node = existing_node()
    original = (node.model_dump(mode="json"),)
    modal = VerificationNodesModal(original, repositories=("app",))
    app = FormApp(modal)
    async with app.run_test() as pilot:
        modal.query_one("#verification-node-list", Select).value = 0
        await press(app, pilot, "#verification-node-edit")
        form = app.screen
        assert form.query_one("#node-ssh-fields").display
        form.query_one("#node-recipes", Select).value = 0
        await press(app, pilot, "#recipe-edit")
        assert app.screen.query_one("#recipe-repository", Select).value == "unlisted"
        await press(app, pilot, "#recipe-save")
        await press(app, pilot, "#node-save")
        await press(app, pilot, "#verification-nodes-save")
        raw, expected = app.results[0]
        assert expected == digest(original)
        assert json.loads(raw) == list(original)


async def test_removals_and_nested_cancel_only_change_drafts():
    node = existing_node()
    original = (node.model_dump(mode="json"),)
    modal = VerificationNodesModal(original)
    app = FormApp(modal)
    async with app.run_test() as pilot:
        modal.query_one("#verification-node-list", Select).value = 0
        await press(app, pilot, "#verification-node-edit")
        form = app.screen
        form.query_one("#node-recipes", Select).value = 0
        await press(app, pilot, "#recipe-remove")
        assert not form.recipes and modal.nodes[0] == node
        await press(app, pilot, "#node-cancel")
        assert modal.nodes == [node]
        await press(app, pilot, "#verification-node-remove")
        assert not modal.nodes and original == (node.model_dump(mode="json"),)
        await press(app, pilot, "#verification-nodes-cancel")
        assert app.results == [None]


async def test_cancel_recipe_keeps_parent_unsaved_fields_and_save_allows_empty_list():
    modal = VerificationNodesModal((existing_node().model_dump(mode="json"),))
    app = FormApp(modal)
    async with app.run_test() as pilot:
        modal.query_one("#verification-node-list", Select).value = 0
        await press(app, pilot, "#verification-node-edit")
        form = app.screen
        form.query_one("#node-key", Input).value = "renamed"
        await press(app, pilot, "#recipe-add")
        await press(app, pilot, "#recipe-cancel")
        assert app.screen is form and form.query_one("#node-key", Input).value == "renamed"
        await press(app, pilot, "#node-save")
        assert modal.nodes[0].key == "renamed"
        await press(app, pilot, "#verification-node-remove")
        await press(app, pilot, "#verification-nodes-save")
        assert json.loads(app.results[0][0]) == []


async def test_node_validation_and_transport_visibility():
    form = VerificationNodeForm(used_keys=("taken",))
    app = FormApp(form)
    async with app.run_test() as pilot:
        form.query_one("#node-key", Input).value = "taken"
        await press(app, pilot, "#node-save")
        assert not app.results and "同名" in str(form.query_one(".form-error", Static).render())
        form.query_one("#node-key", Input).value = "remote"
        form.query_one("#node-transport", Select).value = "ssh"
        form.query_one("#node-enabled", Checkbox).value = True
        await pilot.pause()
        assert form.query_one("#node-ssh-fields").display
        await press(app, pilot, "#node-save")
        assert not app.results and "启用 SSH" in str(form.query_one(".form-error", Static).render())
        form.query_one("#node-ssh-alias", Input).value = "lab"
        form.query_one("#node-worker-program", Input).value = "python;SECRET"
        form.query_one("#node-worker-arguments", TextArea).load_text("/opt/worker.py")
        await press(app, pilot, "#node-save")
        assert not app.results and "SECRET" not in str(form.query_one(".form-error", Static).render())
        form.query_one("#node-worker-program", Input).value = "/usr/bin/python3"
        form.query_one("#node-transport", Select).value = "local"
        await pilot.pause()
        assert not form.query_one("#node-ssh-fields").display
        await press(app, pilot, "#node-save")
        assert app.results[0].transport == "local"
        assert app.results[0].worker_argv == ("/usr/bin/python3", "/opt/worker.py")


@pytest.mark.parametrize(("field", "value", "message"), [
    ("recipe-key", "taken", "同名"),
    ("recipe-key", "bad key", "脚本标识"),
    ("recipe-timeout", "0", "1–3600"),
    ("recipe-timeout", "3601", "1–3600"),
    ("recipe-timeout", "", "整数"),
    ("recipe-capabilities", "UPPER", "小写"),
    ("recipe-program", "", "不能为空"),
    ("recipe-repository", "", "仓库标识"),
])
async def test_recipe_field_validation_stays_in_form(field, value, message):
    form = VerificationRecipeForm(existing_node().recipes[0], used_keys=("taken",))
    app = FormApp(form)
    async with app.run_test() as pilot:
        form.query_one(f"#{field}", Input).value = value
        await press(app, pilot, "#recipe-save")
        assert app.screen is form and not app.results
        assert message in str(form.query_one(".form-error", Static).render())


def test_repository_choices_include_legacy_and_groups_without_duplicates():
    controller = object.__new__(TuiController)
    controller._orchestrator = SimpleNamespace(config=SimpleNamespace(
        repositories=(SimpleNamespace(key="app"),),
        repository_groups=(SimpleNamespace(repositories=(SimpleNamespace(key="app"), SimpleNamespace(key="sdk"))),),
    ))
    assert controller.verification_repositories() == ("app", "sdk")


async def test_existing_standalone_worker_executable_remains_editable():
    node = VerificationNode(key="standalone", enabled=True, transport="ssh",
        ssh_alias="lab", worker_argv=("/opt/verification-worker",))
    app = FormApp(VerificationNodeForm(node))
    async with app.run_test() as pilot:
        await press(app, pilot, "#node-save")
        assert app.results == [node]
