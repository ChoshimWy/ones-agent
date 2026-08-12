from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.developer_workflow.setup_import import (
    ImportDetection,
    SetupImportError,
    detect_import_sources,
    import_selected,
    parse_dotenv,
)
from src.developer_workflow.setup_models import RuntimeSecrets, SecretKind


def test_detection_reports_allowlisted_names_only_without_secret_values(
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "ONES_PASSWORD=DOTENV-TOKEN\nONES_DEV_PROVIDER_TOKEN=provider-secret\n",
        encoding="utf-8",
    )
    template = tmp_path / "template.json"
    template.write_text("{}", encoding="utf-8")

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
    dotenv.write_text(
        "# bootstrap credentials\nONES_EMAIL=dev@example.invalid\n"
        "ONES_PASSWORD=a safe:/._-+@% value\n",
        encoding="utf-8",
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
    dotenv.write_bytes(payload.encode("utf-8"))
    with pytest.raises(SetupImportError) as caught:
        parse_dotenv(dotenv)
    assert str(caught.value) == "dotenv file is invalid"
    assert caught.value.__cause__ is None
    assert "value" not in str(caught.value)


def test_parse_dotenv_rejects_invalid_utf8(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_bytes(b"ONES_PASSWORD=\xff\n")
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
        dotenv.write_bytes(payload)
        with pytest.raises(SetupImportError, match="^dotenv file is invalid$"):
            parse_dotenv(dotenv)


def test_missing_dotenv_is_empty_but_non_regular_and_symlink_fail_closed(
    tmp_path: Path,
) -> None:
    assert parse_dotenv(tmp_path / "missing.env") == {}
    with pytest.raises(SetupImportError, match="^dotenv path is unsafe$"):
        parse_dotenv(tmp_path)

    target = tmp_path / "target"
    target.write_text("ONES_PASSWORD=TOKEN\n", encoding="utf-8")
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
    dotenv.write_text("ONES_PASSWORD=TOKEN\n", encoding="utf-8")
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
    dotenv.write_text("ONES_PASSWORD=TOKEN\n", encoding="utf-8")
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
    corrupt.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    with pytest.raises(SetupImportError, match="^template config is invalid$"):
        detect_import_sources(
            template_config_path=corrupt,
            dotenv_path=tmp_path / "missing.env",
            environment={},
        )


def test_deep_template_json_has_sanitized_failure(tmp_path: Path) -> None:
    template = tmp_path / "deep.json"
    template.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
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
