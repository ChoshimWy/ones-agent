from __future__ import annotations

import os
import shlex

import pytest

from src.developer_workflow.repository import _isolated_git_environment, _windows_ssh_command


def config_values(environment, key):
    return [environment[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(environment["GIT_CONFIG_COUNT"]))
            if environment[f"GIT_CONFIG_KEY_{i}"] == key]


def test_native_credential_fallback_keeps_configuration_isolated(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "8")
    monkeypatch.setenv("GIT_CONFIG_KEY_7", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_7", "!malicious-helper")
    monkeypatch.setenv("GIT_ASKPASS", "ambient-untrusted-askpass")
    environment = _isolated_git_environment()
    assert config_values(environment, "credential.helper") == (["", "manager"] if os.name == "nt" else [""])
    assert config_values(environment, "credential.interactive") == ["false"]
    assert config_values(environment, "core.hooksPath") == [os.devnull]
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "Never"
    assert "GIT_ASKPASS" not in environment
    assert "GIT_CONFIG_VALUE_7" not in environment


def test_explicit_askpass_does_not_fall_back_to_personal_credentials():
    environment = _isolated_git_environment({"GIT_ASKPASS": "configured-askpass"})
    assert environment["GIT_ASKPASS"] == "configured-askpass"
    assert config_values(environment, "credential.helper") == [""]
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.skipif(os.name != "nt", reason="Windows native profile API")
def test_native_ssh_uses_only_pinned_files_not_ambient_config(tmp_path, monkeypatch):
    import ctypes

    def profile(_window, _folder, _token, _flags, buffer):
        buffer.value = str(tmp_path)
        return 0

    monkeypatch.setattr(ctypes.windll.shell32, "SHGetFolderPathW", profile)
    directory = tmp_path / ".ssh"
    directory.mkdir()
    (directory / "known_hosts").write_text("fixture host key", encoding="utf-8")
    (directory / "id_ed25519").write_text("fixture private key", encoding="utf-8")
    (directory / "config").write_text("ProxyCommand malicious-command", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "poison"))
    command = shlex.split(_windows_ssh_command())
    assert command[:3] == ["ssh", "-F", "none"]
    assert "-oStrictHostKeyChecking=yes" in command
    assert "-oUpdateHostKeys=no" in command
    assert "-oBatchMode=yes" in command
    assert "-oUserKnownHostsFile=" + (directory / "known_hosts").as_posix() in command
    assert (directory / "id_ed25519").as_posix() in command
    assert "fixture private key" not in str(command)
    assert "malicious-command" not in str(command)


def test_explicit_ssh_settings_take_precedence(monkeypatch):
    monkeypatch.setattr("src.developer_workflow.repository._windows_ssh_command", lambda: "native-default")
    env = _isolated_git_environment({"GIT_SSH_COMMAND": "configured-ssh"})
    assert env["GIT_SSH_COMMAND"] == "configured-ssh"
    env = _isolated_git_environment({"GIT_SSH": "configured-ssh-executable"})
    assert env["GIT_SSH"] == "configured-ssh-executable"
    assert "GIT_SSH_COMMAND" not in env
