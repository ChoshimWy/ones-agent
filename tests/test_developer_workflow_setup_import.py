from __future__ import annotations

import os
import errno
import json
from pathlib import Path
import sys

import pytest

from src.developer_workflow.setup_import import (
    ImportDetection,
    SetupImportError,
    detect_import_sources,
    import_selected,
    load_template_workflow,
    parse_dotenv,
)


def test_load_template_workflow_is_read_only_and_resolves_private_roots(
    tmp_path: Path,
) -> None:
    path = tmp_path / "template.json"
    path.write_text(
        json.dumps(
            {
                "run_root": "runs",
                "mirror_root": "mirrors",
                "worktree_root": "worktrees",
                "sandbox_permission_profile": "managed-dev",
                "max_codex_attempts": 3,
                "repositories": [{
                    "key": "repo", "project_id": "P", "iteration_id": "I",
                    "repo_url": "https://git.example.invalid/o/repo.git",
                    "repo_name": "repo", "base_branch": "main",
                }],
                "publishing": {"provider": "github"},
            }
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()
    workflow = load_template_workflow(path)
    assert workflow is not None
    assert workflow.run_root == (tmp_path / "runs").absolute()
    assert path.read_bytes() == before


def test_load_template_workflow_allows_missing_but_rejects_secret_keys(
    tmp_path: Path,
) -> None:
    assert load_template_workflow(tmp_path / "missing.json") is None
    unsafe = tmp_path / "unsafe.json"
    _write_private(unsafe, '{"provider_token":"must-not-load"}')
    with pytest.raises(SetupImportError, match="template config is invalid"):
        load_template_workflow(unsafe)
from src.developer_workflow.setup_models import RuntimeSecrets, SecretKind


def _write_private(path: Path, payload: str | bytes) -> None:
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    from src.developer_workflow.setup_store import _protect_private_file

    _protect_private_file(path)


def test_detection_reports_allowlisted_names_only_without_secret_values(
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env"
    _write_private(dotenv,
        "ONES_PASSWORD=DOTENV-TOKEN\nONES_DEV_PROVIDER_TOKEN=provider-secret\n",
    )
    template = tmp_path / "template.json"
    _write_private(template, "{}")

    detection = detect_import_sources(
        template_config_path=template,
        dotenv_path=dotenv,
        environment={
            "ONES_EMAIL": "developer@example.invalid",
            "CODEX_API_KEY": "ENV-TOKEN",
            "ONES_DEV_GIT_ASKPASS": "C:/safe/helper.exe",
            "GIT_ASKPASS": "must-not-import-parent-git-state",
            "ONES_TEAM_ID": "public-config",
            "UNKNOWN_TOKEN": "must-not-be-guessed",
        },
    )

    assert detection == ImportDetection(
        environment=(
            SecretKind.ONES_EMAIL,
            SecretKind.CODEX_API_KEY,
            SecretKind.GIT_ASKPASS,
        ),
        dotenv=(SecretKind.ONES_PASSWORD, SecretKind.PROVIDER_TOKEN),
        template_available=True,
    )
    rendered = repr(detection)
    assert "ENV-TOKEN" not in rendered
    assert "DOTENV-TOKEN" not in rendered
    assert "secret" not in rendered
    assert str(dotenv) not in rendered
    assert not hasattr(detection, "__dict__")
    with pytest.raises(Exception):
        detection.template_available = False  # type: ignore[misc]


def test_detection_ignores_empty_values_and_public_configuration(tmp_path: Path) -> None:
    detection = detect_import_sources(
        template_config_path=None,
        dotenv_path=tmp_path / ".env",
        environment={
            "ONES_EMAIL": "",
            "ONES_BASE_URL": "https://ones.invalid",
            "ONES_DEV_PROVIDER_HOST": "gitlab.invalid",
            "CODEX_HOME": "C:/codex",
        },
    )
    assert detection == ImportDetection((), (), False)


def test_detection_template_path_can_be_omitted(tmp_path: Path) -> None:
    detection = detect_import_sources(
        dotenv_path=tmp_path / ".env",
        environment={},
    )
    assert detection.template_available is False


def test_detection_dotenv_and_template_paths_can_both_be_omitted() -> None:
    detection = detect_import_sources(
        environment={"ONES_EMAIL": "developer@example.invalid"}
    )
    assert detection == ImportDetection((SecretKind.ONES_EMAIL,), (), False)


def test_detection_requires_explicit_environment_mapping() -> None:
    with pytest.raises(SetupImportError, match="^import source is invalid$"):
        detect_import_sources(environment=None)


def test_detection_rejects_non_string_environment_values_without_leaking() -> None:
    with pytest.raises(SetupImportError) as caught:
        detect_import_sources(
            template_config_path=None,
            dotenv_path=Path("missing.env"),
            environment={"CODEX_API_KEY": object()},  # type: ignore[dict-item]
        )
    assert str(caught.value) == "import source is invalid"
    assert caught.value.__cause__ is None


def test_parse_dotenv_accepts_simple_utf8_values_and_whole_line_comments(
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env"
    _write_private(dotenv,
        "# bootstrap credentials\nONES_EMAIL=dev@example.invalid\n"
        "ONES_PASSWORD=a safe:/._-+@% value\n",
    )
    before = (dotenv.read_bytes(), dotenv.stat().st_mtime_ns)
    values = parse_dotenv(dotenv)
    assert values == {
        "ONES_EMAIL": "dev@example.invalid",
        "ONES_PASSWORD": "a safe:/._-+@% value",
    }
    assert (dotenv.read_bytes(), dotenv.stat().st_mtime_ns) == before


@pytest.mark.parametrize(
    "payload",
    [
        "export ONES_PASSWORD=value\n",
        " ONES_PASSWORD=value\n",
        "ONES PASSWORD=value\n",
        "ONES_PASSWORD =value\n",
        "ONES_PASSWORD=value\nONES_PASSWORD=other\n",
        "ONES_PASSWORD='quoted'\n",
        'ONES_PASSWORD="quoted"\n',
        "ONES_PASSWORD=value # ambiguous\n",
        "ONES_PASSWORD=value\\\ncontinued\n",
        "ONES_PASSWORD=${HOME}\n",
        "ONES_PASSWORD=$HOME\n",
        "ONES_PASSWORD=$VAR\n",
        "ONES_PASSWORD=$1\n",
        "ONES_PASSWORD=\\$HOME\n",
        "ONES_PASSWORD=$(whoami)\n",
        "ONES_PASSWORD=`whoami`\n",
        "source secrets.env\n",
        "include=secrets.env\n",
        "ONES_PASSWORD=line1\nline2\n",
        "ONES_PASSWORD=bad\x00value\n",
        "ONES_PASSWORD=bad\x1bvalue\n",
        "ONES_PASSWORD=bad\u2028value\n",
        "ONES_PASSWORD=bad\u2029value\n",
        "ONES_PASSWORD=bad\u200bvalue\n",
    ],
)
def test_parse_dotenv_rejects_shell_and_ambiguous_syntax(
    tmp_path: Path, payload: str
) -> None:
    dotenv = tmp_path / ".env"
    _write_private(dotenv, payload.encode("utf-8"))
    with pytest.raises(SetupImportError) as caught:
        parse_dotenv(dotenv)
    assert str(caught.value) == "dotenv file is invalid"
    assert caught.value.__cause__ is None
    assert "value" not in str(caught.value)


def test_parse_dotenv_rejects_invalid_utf8(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    _write_private(dotenv, b"ONES_PASSWORD=\xff\n")
    with pytest.raises(SetupImportError, match="^dotenv file is invalid$"):
        parse_dotenv(dotenv)


def test_parse_dotenv_enforces_file_line_count_and_line_length_bounds(
    tmp_path: Path,
) -> None:
    for name, payload in (
        ("large", b"A" * (1024 * 1024 + 1)),
        ("long-line", b"ONES_PASSWORD=" + b"x" * 8192 + b"\n"),
        ("many-lines", b"# x\n" * 4097),
    ):
        dotenv = tmp_path / name
        _write_private(dotenv, payload)
        with pytest.raises(SetupImportError, match="^dotenv file is invalid$"):
            parse_dotenv(dotenv)


def test_missing_dotenv_is_empty_but_non_regular_and_symlink_fail_closed(
    tmp_path: Path,
) -> None:
    assert parse_dotenv(tmp_path / "missing.env") == {}
    with pytest.raises(SetupImportError, match="^dotenv path is unsafe$"):
        parse_dotenv(tmp_path)

    target = tmp_path / "target"
    _write_private(target, "ONES_PASSWORD=TOKEN\n")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(SetupImportError, match="^dotenv path is unsafe$"):
        parse_dotenv(link)


def test_parse_dotenv_rejects_identity_or_metadata_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    dotenv = tmp_path / ".env"
    _write_private(dotenv, "ONES_PASSWORD=TOKEN\n")
    original = setup_import._descriptor_identity
    calls = 0

    def changed(descriptor: int) -> tuple[int, int, int, int]:
        nonlocal calls
        calls += 1
        identity = original(descriptor)
        if calls > 1:
            return identity[0], identity[1], identity[2] + 1, identity[3]
        return identity

    monkeypatch.setattr(setup_import, "_descriptor_identity", changed)
    with pytest.raises(SetupImportError, match="^dotenv path is unsafe$"):
        parse_dotenv(dotenv)


def test_parse_dotenv_deletion_after_initial_identity_is_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    dotenv = tmp_path / ".env"
    _write_private(dotenv, "ONES_PASSWORD=TOKEN\n")
    original = setup_import._path_identity
    calls = 0

    def deleted(path: Path) -> tuple[int, int, int, int]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise FileNotFoundError
        return original(path)

    monkeypatch.setattr(setup_import, "_path_identity", deleted)
    with pytest.raises(SetupImportError, match="^dotenv path is unsafe$"):
        parse_dotenv(dotenv)


def test_template_missing_is_false_but_corrupt_or_unsafe_fails_closed(
    tmp_path: Path,
) -> None:
    missing = detect_import_sources(
        template_config_path=tmp_path / "missing.json",
        dotenv_path=tmp_path / "missing.env",
        environment={},
    )
    assert missing.template_available is False

    corrupt = tmp_path / "corrupt.json"
    _write_private(corrupt, '{"a": 1, "a": 2}')
    with pytest.raises(SetupImportError, match="^template config is invalid$"):
        detect_import_sources(
            template_config_path=corrupt,
            dotenv_path=tmp_path / "missing.env",
            environment={},
        )


def test_deep_template_json_has_sanitized_failure(tmp_path: Path) -> None:
    template = tmp_path / "deep.json"
    _write_private(template, "[" * 2000 + "]" * 2000)
    with pytest.raises(SetupImportError, match="^template config is invalid$") as caught:
        detect_import_sources(
            template_config_path=template,
            dotenv_path=tmp_path / "missing.env",
            environment={},
        )
    assert caught.value.__cause__ is None

    directory = tmp_path / "template.json"
    directory.mkdir()
    with pytest.raises(SetupImportError, match="^template config path is unsafe$"):
        detect_import_sources(
            template_config_path=directory,
            dotenv_path=tmp_path / "missing.env",
            environment={},
        )


def test_import_selected_empty_returns_empty_runtime_secrets() -> None:
    imported = import_selected(
        environment={"ONES_PASSWORD": "ENV-TOKEN"},
        dotenv_values={"ONES_PASSWORD": "DOTENV-TOKEN"},
        selected=(),
    )
    assert type(imported) is RuntimeSecrets
    assert dict(imported.values) == {}
    assert "TOKEN" not in repr(imported)


def test_import_selected_requires_explicit_source_for_conflict() -> None:
    with pytest.raises(SetupImportError, match="^credential source selection is required$"):
        import_selected(
            environment={"ONES_PASSWORD": "ENV-TOKEN"},
            dotenv_values={"ONES_PASSWORD": "DOTENV-TOKEN"},
            selected=(SecretKind.ONES_PASSWORD,),
        )

    imported = import_selected(
        environment={"ONES_PASSWORD": "ENV-TOKEN"},
        dotenv_values={"ONES_PASSWORD": "DOTENV-TOKEN"},
        selected=(SecretKind.ONES_PASSWORD,),
        source_choice={SecretKind.ONES_PASSWORD: "dotenv"},
    )
    assert imported.require(SecretKind.ONES_PASSWORD) == "DOTENV-TOKEN"


def test_import_selected_rejects_missing_selected_or_wrong_source() -> None:
    with pytest.raises(SetupImportError, match="^selected credential is unavailable$"):
        import_selected(
            environment={},
            dotenv_values={},
            selected=(SecretKind.CODEX_API_KEY,),
        )
    with pytest.raises(SetupImportError, match="^selected credential is unavailable$"):
        import_selected(
            environment={"CODEX_API_KEY": "ENV-TOKEN"},
            dotenv_values={},
            selected=(SecretKind.CODEX_API_KEY,),
            source_choice={SecretKind.CODEX_API_KEY: "dotenv"},
        )


def test_import_selected_rejects_non_exact_types_duplicates_and_secret_controls() -> None:
    bad_calls = (
        lambda: import_selected({}, {}, [SecretKind.ONES_PASSWORD]),  # type: ignore[arg-type]
        lambda: import_selected({}, {}, ("ones_password",)),  # type: ignore[arg-type]
        lambda: import_selected(
            {}, {}, (SecretKind.ONES_PASSWORD, SecretKind.ONES_PASSWORD)
        ),
        lambda: import_selected(
            {"ONES_PASSWORD": b"TOKEN"},  # type: ignore[dict-item]
            {},
            (SecretKind.ONES_PASSWORD,),
        ),
        lambda: import_selected(
            {"ONES_PASSWORD": "bad\nTOKEN"},
            {},
            (SecretKind.ONES_PASSWORD,),
        ),
    )
    for call in bad_calls:
        with pytest.raises(SetupImportError) as caught:
            call()
        assert caught.value.__cause__ is None
        assert "TOKEN" not in str(caught.value)


def test_import_does_not_mutate_sources() -> None:
    environment = {"ONES_EMAIL": "dev@example.invalid"}
    dotenv = {"ONES_PASSWORD": "password"}
    environment_copy = environment.copy()
    dotenv_copy = dotenv.copy()
    import_selected(
        environment,
        dotenv,
        (SecretKind.ONES_EMAIL, SecretKind.ONES_PASSWORD),
    )
    assert environment == environment_copy
    assert dotenv == dotenv_copy


@pytest.mark.parametrize(
    "value",
    [
        "ssh -o StrictHostKeyChecking=yes;calc",
        "ssh & calc",
        "ssh | calc",
        "ssh > output",
        "cmd /c calc",
        "ssh (calc)",
        "ssh.exe -oBatchMode=yes",
        '"C:\\Program Files\\ssh.exe"',
    ],
)
def test_git_command_import_rejects_commands_and_shell_grammar(value: str) -> None:
    with pytest.raises(SetupImportError, match="^selected credential is invalid$"):
        import_selected(
            {"ONES_DEV_GIT_SSH_COMMAND": value},
            {},
            (SecretKind.GIT_SSH_COMMAND,),
        )


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        (SecretKind.GIT_ASKPASS, "ONES_DEV_GIT_ASKPASS"),
        (SecretKind.GIT_SSH, "ONES_DEV_GIT_SSH"),
        (SecretKind.GIT_SSH_COMMAND, "ONES_DEV_GIT_SSH_COMMAND"),
        (SecretKind.SSH_ASKPASS, "ONES_DEV_SSH_ASKPASS"),
    ],
)
def test_git_executable_import_accepts_only_existing_absolute_regular_path(
    kind: SecretKind, name: str
) -> None:
    executable = str(Path(sys.executable).resolve())
    imported = import_selected({name: executable}, {}, (kind,))
    assert imported.require(kind) == executable
    with pytest.raises(SetupImportError, match="^selected credential is invalid$"):
        import_selected({name: "relative-helper"}, {}, (kind,))


def test_ssh_auth_sock_import_requires_absolute_safe_path(tmp_path: Path) -> None:
    imported = import_selected(
        {"ONES_DEV_SSH_AUTH_SOCK": str(tmp_path.resolve() / "agent.sock")},
        {},
        (SecretKind.SSH_AUTH_SOCK,),
    )
    assert imported.require(SecretKind.SSH_AUTH_SOCK).endswith("agent.sock")
    with pytest.raises(SetupImportError, match="^selected credential is invalid$"):
        import_selected(
            {"ONES_DEV_SSH_AUTH_SOCK": "relative.sock"},
            {},
            (SecretKind.SSH_AUTH_SOCK,),
        )


def test_read_bounded_zeroes_content_when_postread_identity_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    dotenv = tmp_path / ".env"
    _write_private(dotenv, "ONES_PASSWORD=TOKEN\n")
    original_identity = setup_import._descriptor_identity
    identities = 0
    zeroed: list[bytes] = []
    original_zero = setup_import._zero_buffer

    def changed(descriptor: int) -> tuple[int, int, int, int]:
        nonlocal identities
        identities += 1
        result = original_identity(descriptor)
        if identities > 1:
            return result[0], result[1], result[2], result[3] + 1
        return result

    def observe(value: bytearray) -> None:
        original_zero(value)
        zeroed.append(bytes(value))

    monkeypatch.setattr(setup_import, "_descriptor_identity", changed)
    monkeypatch.setattr(setup_import, "_zero_buffer", observe)
    with pytest.raises(SetupImportError, match="^dotenv path is unsafe$"):
        parse_dotenv(dotenv)
    assert zeroed
    assert all(set(item) <= {0} for item in zeroed)


def test_parse_error_zeroes_returned_source_buffer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    dotenv = tmp_path / ".env"
    _write_private(dotenv, "ONES_PASSWORD=$HOME\n")
    zeroed: list[bytes] = []
    original_zero = setup_import._zero_buffer

    def observe(value: bytearray) -> None:
        original_zero(value)
        zeroed.append(bytes(value))

    monkeypatch.setattr(setup_import, "_zero_buffer", observe)
    with pytest.raises(SetupImportError, match="^dotenv file is invalid$"):
        parse_dotenv(dotenv)
    assert zeroed and all(set(item) <= {0} for item in zeroed)


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_windows_source_read_handle_blocks_write_and_delete(
    tmp_path: Path,
) -> None:
    from src.developer_workflow import setup_import

    dotenv = tmp_path / ".env"
    _write_private(dotenv, "ONES_PASSWORD=TOKEN\n")
    descriptor = setup_import._open_source_readonly(dotenv)
    try:
        with pytest.raises(OSError):
            os.open(dotenv, os.O_WRONLY)
        with pytest.raises(OSError):
            dotenv.unlink()
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL semantics")
def test_windows_source_with_inherited_writable_acl_is_rejected(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.private_paths import _windows_descriptor

    dotenv = tmp_path / "inherited.env"
    with open(dotenv, "w", encoding="utf-8") as stream:
        stream.write("ONES_PASSWORD=TOKEN\n")
    _owner, _entries, protected = _windows_descriptor(dotenv)
    if protected:
        pytest.skip("test filesystem creates protected files by default")
    with pytest.raises(SetupImportError, match="^dotenv path is unsafe$"):
        parse_dotenv(dotenv)


def test_posix_open_source_closes_descriptor_when_fstat_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    closed: list[int] = []
    candidate = tmp_path / "source.env"
    monkeypatch.setattr(setup_import.os, "name", "posix")
    monkeypatch.setattr(setup_import.os, "open", lambda *_args: 31337)
    monkeypatch.setattr(
        setup_import.os,
        "fstat",
        lambda _descriptor: (_ for _ in ()).throw(RuntimeError("TOKEN-FSTAT")),
    )
    monkeypatch.setattr(setup_import.os, "close", closed.append)

    with pytest.raises(RuntimeError, match="^TOKEN-FSTAT$"):
        setup_import._open_source_readonly(candidate)
    assert closed == [31337]


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ownership")
def test_windows_open_osfhandle_error_is_not_source_access_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    class Function:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def __call__(self, *args: object) -> object:
            return self.callback(*args)  # type: ignore[operator]

    class Kernel:
        def __init__(self) -> None:
            self.closed: list[int] = []
            self.CreateFileW = Function(lambda *_args: 31337)
            self.GetFinalPathNameByHandleW = Function(self._final_path)
            self.CloseHandle = Function(self._close)

        def _final_path(
            self, _handle: int, buffer: object, _size: int, _flags: int
        ) -> int:
            buffer.value = str(tmp_path / "source.env")  # type: ignore[attr-defined]
            return len(buffer.value)  # type: ignore[attr-defined]

        def _close(self, handle: int) -> bool:
            self.closed.append(handle)
            return True

    class Msvcrt:
        @staticmethod
        def open_osfhandle(_handle: int, _flags: int) -> int:
            raise PermissionError(errno.EACCES, "TOKEN-OSFHANDLE")

    kernel = Kernel()
    candidate = tmp_path / "source.env"
    monkeypatch.setattr(setup_import.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel)
    monkeypatch.setitem(sys.modules, "msvcrt", Msvcrt)
    monkeypatch.setattr(
        setup_import, "_windows_descriptor", lambda _path: ("user", [("user", 0x001F01FF, 0, 0)], True)
    )
    monkeypatch.setattr(setup_import, "_current_user_sid", lambda: "user")

    with pytest.raises(PermissionError, match=r"^\[Errno 13\] TOKEN-OSFHANDLE$"):
        setup_import._open_source_readonly(candidate)
    assert kernel.closed == [31337]


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ownership")
def test_windows_close_handle_false_is_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow import setup_import

    class Kernel:
        @staticmethod
        def CloseHandle(_handle: int) -> bool:
            return False

    monkeypatch.setattr(setup_import.ctypes, "get_last_error", lambda: 5)
    cleanup_failure = setup_import._close_windows_handle(Kernel(), 31337)

    assert isinstance(cleanup_failure, OSError)
    assert setup_import._prefer_cleanup_failure(
        setup_import._SourcePathRejected(), cleanup_failure
    ) is cleanup_failure


def test_buffer_allocation_failure_closes_open_descriptor_without_reclassification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    dotenv = tmp_path / "allocation.env"
    _write_private(dotenv, "ONES_PASSWORD=TOKEN\n")
    opened: list[int] = []
    original_open = setup_import._open_source_readonly

    def capture(path: Path) -> int:
        descriptor = original_open(path)
        opened.append(descriptor)
        return descriptor

    def fail_allocation(_size: int) -> bytearray:
        raise MemoryError("TOKEN-ALLOCATION")

    monkeypatch.setattr(setup_import, "_open_source_readonly", capture)
    monkeypatch.setattr(setup_import, "_allocate_buffer", fail_allocation)
    with pytest.raises(MemoryError, match="^TOKEN-ALLOCATION$") as caught:
        parse_dotenv(dotenv)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_runtime_read_failure_is_fatal_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    dotenv = tmp_path / "runtime.env"
    _write_private(dotenv, "ONES_PASSWORD=TOKEN\n")

    def fail_read(_reader: object, _target: memoryview) -> int:
        raise RuntimeError("TOKEN-RUNTIME")

    monkeypatch.setattr(setup_import, "_read_into", fail_read)
    with pytest.raises(SetupImportError, match="^source read failed$") as caught:
        parse_dotenv(dotenv)
    assert type(caught.value) is SetupImportError
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "TOKEN" not in repr(caught.value)


def test_zero_failure_still_closes_descriptor_and_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    dotenv = tmp_path / "zero.env"
    _write_private(dotenv, "ONES_PASSWORD=TOKEN\n")
    opened: list[int] = []
    original_open = setup_import._open_source_readonly
    original_zero = setup_import._zero_buffer

    def capture(path: Path) -> int:
        descriptor = original_open(path)
        opened.append(descriptor)
        return descriptor

    def fail_zero(value: bytearray) -> None:
        original_zero(value)
        raise RuntimeError("TOKEN-ZERO")

    monkeypatch.setattr(setup_import, "_open_source_readonly", capture)
    monkeypatch.setattr(setup_import, "_zero_buffer", fail_zero)
    with pytest.raises(SetupImportError, match="^source read failed$") as caught:
        parse_dotenv(dotenv)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "TOKEN" not in repr(caught.value)
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_dotenv_source_rejection_has_specific_unavailable_class(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.env"
    unsafe.mkdir()

    with pytest.raises(SetupImportError) as caught:
        parse_dotenv(unsafe)
    assert type(caught.value).__name__ == "SetupImportSourceUnavailable"


def test_close_failure_still_zeroes_content_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    dotenv = tmp_path / "close.env"
    _write_private(dotenv, "ONES_PASSWORD=TOKEN\n")
    opened: list[int] = []
    zeroed: list[bytes] = []
    original_open = setup_import._open_source_readonly
    original_zero = setup_import._zero_buffer

    def capture(path: Path) -> int:
        descriptor = original_open(path)
        opened.append(descriptor)
        return descriptor

    def close_then_fail(descriptor: int) -> None:
        os.close(descriptor)
        raise OSError("TOKEN-CLOSE")

    def observe(value: bytearray) -> None:
        original_zero(value)
        zeroed.append(bytes(value))

    monkeypatch.setattr(setup_import, "_open_source_readonly", capture)
    monkeypatch.setattr(setup_import, "_close_descriptor", close_then_fail)
    monkeypatch.setattr(setup_import, "_zero_buffer", observe)
    with pytest.raises(SetupImportError, match="^source read failed$") as caught:
        parse_dotenv(dotenv)
    assert type(caught.value) is SetupImportError
    assert caught.value.__cause__ is None
    assert "TOKEN" not in repr(caught.value.__context__)
    assert zeroed and all(set(item) <= {0} for item in zeroed)
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_read_error_precedes_close_error_as_fatal_sanitized_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    dotenv = tmp_path / "read-close.env"
    _write_private(dotenv, "ONES_PASSWORD=TOKEN\n")

    def fail_read(_reader: object, _target: memoryview) -> int:
        raise ValueError("TOKEN-READ")

    def close_then_fail(descriptor: int) -> None:
        os.close(descriptor)
        raise OSError("TOKEN-CLOSE")

    monkeypatch.setattr(setup_import, "_read_into", fail_read)
    monkeypatch.setattr(setup_import, "_close_descriptor", close_then_fail)
    with pytest.raises(SetupImportError, match="^source read failed$") as caught:
        parse_dotenv(dotenv)
    assert type(caught.value) is SetupImportError
    assert caught.value.__cause__ is None
    assert "TOKEN" not in repr(caught.value.__context__)


def test_successful_read_closes_descriptor_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import setup_import

    dotenv = tmp_path / "success.env"
    _write_private(dotenv, "ONES_PASSWORD=TOKEN\n")
    closes: list[int] = []

    def close_once(descriptor: int) -> None:
        closes.append(descriptor)
        os.close(descriptor)

    monkeypatch.setattr(setup_import, "_close_descriptor", close_once)
    assert parse_dotenv(dotenv) == {"ONES_PASSWORD": "TOKEN"}
    assert len(closes) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point coverage")
def test_windows_reparse_dotenv_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    os.system(f'cmd /c mklink /J "{junction}" "{target}" >nul')
    if not junction.exists():
        pytest.skip("junction creation is unavailable")
    with pytest.raises(SetupImportError, match="^dotenv path is unsafe$"):
        parse_dotenv(junction / ".env")
