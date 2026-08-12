"""Fail-closed discovery and explicit import of bootstrap credentials."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Literal, Mapping
import unicodedata

from .private_paths import _is_link_or_reparse
from .setup_models import RuntimeSecrets, SecretKind


class SetupImportError(RuntimeError):
    """A sanitized failure while inspecting or importing legacy inputs."""


@dataclass(frozen=True, slots=True)
class ImportDetection:
    """Secret kinds present in approved sources, never their values or paths."""

    environment: tuple[SecretKind, ...]
    dotenv: tuple[SecretKind, ...]
    template_available: bool

    def __post_init__(self) -> None:
        if (
            type(self.environment) is not tuple
            or type(self.dotenv) is not tuple
            or type(self.template_available) is not bool
            or any(type(kind) is not SecretKind for kind in self.environment)
            or any(type(kind) is not SecretKind for kind in self.dotenv)
            or len(set(self.environment)) != len(self.environment)
            or len(set(self.dotenv)) != len(self.dotenv)
        ):
            raise TypeError("import detection is invalid")


_ENVIRONMENT_SECRET_KINDS: dict[str, SecretKind] = {
    "ONES_EMAIL": SecretKind.ONES_EMAIL,
    "ONES_PASSWORD": SecretKind.ONES_PASSWORD,
    "ONES_DEV_PROVIDER_TOKEN": SecretKind.PROVIDER_TOKEN,
    "CODEX_API_KEY": SecretKind.CODEX_API_KEY,
    "CODEX_AUTH_TOKEN": SecretKind.CODEX_AUTH_TOKEN,
    "ONES_DEV_GIT_ASKPASS": SecretKind.GIT_ASKPASS,
    "ONES_DEV_GIT_SSH": SecretKind.GIT_SSH,
    "ONES_DEV_GIT_SSH_COMMAND": SecretKind.GIT_SSH_COMMAND,
    "ONES_DEV_SSH_ASKPASS": SecretKind.SSH_ASKPASS,
    "ONES_DEV_SSH_AUTH_SOCK": SecretKind.SSH_AUTH_SOCK,
}
_MAX_SOURCE_BYTES = 1024 * 1024
_MAX_DOTENV_LINES = 4096
_MAX_DOTENV_LINE_BYTES = 8192
_MAX_SECRET_BYTES = 2560
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_UNSAFE_VALUE_FRAGMENTS = ("${", "$(", "`", "#")
_READ_CHUNK = 64 * 1024


def detect_import_sources(
    template_config_path: Path | None = None,
    dotenv_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> ImportDetection:
    """Report only allowlisted credential kinds and template availability."""

    if dotenv_path is None or environment is None:
        raise SetupImportError("import source is invalid")
    environment_values = _copy_string_mapping(environment)
    dotenv_values = parse_dotenv(dotenv_path)
    template_available = _validate_template(template_config_path)
    return ImportDetection(
        environment=_detected_kinds(environment_values),
        dotenv=_detected_kinds(dotenv_values),
        template_available=template_available,
    )


def parse_dotenv(path: Path) -> dict[str, str]:
    """Read a bounded, non-shell ``NAME=value`` file without following links."""

    try:
        raw = _read_bounded(path, missing_ok=True)
    except FileNotFoundError:
        return {}
    except ValueError:
        raise SetupImportError("dotenv file is invalid") from None
    except (OSError, TypeError):
        raise SetupImportError("dotenv path is unsafe") from None
    if raw is None:
        return {}
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise SetupImportError("dotenv file is invalid") from None
    finally:
        _zero_buffer(raw)
    try:
        _validate_unicode_text(text)
        lines = text.splitlines()
        if len(lines) > _MAX_DOTENV_LINES:
            raise ValueError
        values: dict[str, str] = {}
        for line in lines:
            if len(line.encode("utf-8", errors="strict")) > _MAX_DOTENV_LINE_BYTES:
                raise ValueError
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError
            name, value = line.split("=", 1)
            if (
                _NAME.fullmatch(name) is None
                or name.casefold() in {"include", "source"}
                or name in values
            ):
                raise ValueError
            if (
                any(fragment in value for fragment in _UNSAFE_VALUE_FRAGMENTS)
                or "'" in value
                or '"' in value
                or value.endswith("\\")
            ):
                raise ValueError
            values[name] = value
        return values
    except ValueError:
        raise SetupImportError("dotenv file is invalid") from None


def import_selected(
    environment: Mapping[str, str],
    dotenv_values: Mapping[str, str],
    selected: tuple[SecretKind, ...],
    source_choice: Mapping[SecretKind, Literal["environment", "dotenv"]] | None = None,
) -> RuntimeSecrets:
    """Copy explicitly selected credentials from an unambiguous chosen source."""

    try:
        environment_copy = _copy_string_mapping(environment)
        dotenv_copy = _copy_string_mapping(dotenv_values)
        if (
            type(selected) is not tuple
            or any(type(kind) is not SecretKind for kind in selected)
            or len(set(selected)) != len(selected)
        ):
            raise SetupImportError("credential selection is invalid")
        choices = _copy_source_choices(source_choice)
        if any(kind not in selected for kind in choices):
            raise SetupImportError("credential source selection is invalid")

        imported: dict[SecretKind, str] = {}
        for kind in selected:
            name = _name_for_kind(kind)
            environment_present = bool(environment_copy.get(name, ""))
            dotenv_present = bool(dotenv_copy.get(name, ""))
            choice = choices.get(kind)
            if environment_present and dotenv_present and choice is None:
                raise SetupImportError("credential source selection is required")
            if choice == "environment":
                value = environment_copy.get(name, "")
            elif choice == "dotenv":
                value = dotenv_copy.get(name, "")
            elif environment_present:
                value = environment_copy[name]
            elif dotenv_present:
                value = dotenv_copy[name]
            else:
                value = ""
            if not value:
                raise SetupImportError("selected credential is unavailable")
            _validate_secret(value)
            imported[kind] = value
        return RuntimeSecrets(imported)
    except SetupImportError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise SetupImportError("import source is invalid") from None


def _copy_string_mapping(values: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise SetupImportError("import source is invalid")
    copied: dict[str, str] = {}
    try:
        items = values.items()
        for name, value in items:
            if type(name) is not str or type(value) is not str:
                raise SetupImportError("import source is invalid")
            copied[name] = value
    except SetupImportError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise SetupImportError("import source is invalid") from None
    return copied


def _copy_source_choices(
    source_choice: Mapping[SecretKind, Literal["environment", "dotenv"]] | None,
) -> dict[SecretKind, Literal["environment", "dotenv"]]:
    if source_choice is None:
        return {}
    if not isinstance(source_choice, Mapping):
        raise SetupImportError("credential source selection is invalid")
    result: dict[SecretKind, Literal["environment", "dotenv"]] = {}
    try:
        for kind, source in source_choice.items():
            if (
                type(kind) is not SecretKind
                or type(source) is not str
                or source not in {"environment", "dotenv"}
            ):
                raise SetupImportError("credential source selection is invalid")
            result[kind] = source
    except SetupImportError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise SetupImportError("credential source selection is invalid") from None
    return result


def _detected_kinds(values: Mapping[str, str]) -> tuple[SecretKind, ...]:
    return tuple(
        kind
        for name, kind in _ENVIRONMENT_SECRET_KINDS.items()
        if bool(values.get(name, ""))
    )


def _name_for_kind(kind: SecretKind) -> str:
    for name, candidate in _ENVIRONMENT_SECRET_KINDS.items():
        if candidate is kind:
            return name
    raise SetupImportError("credential selection is invalid")


def _validate_secret(value: str) -> None:
    try:
        encoded = bytearray(value.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise SetupImportError("selected credential is invalid") from None
    try:
        if (
            not encoded
            or len(encoded) > _MAX_SECRET_BYTES
            or _contains_unsafe_unicode(value)
        ):
            raise SetupImportError("selected credential is invalid")
    finally:
        _zero_buffer(encoded)


def _contains_unsafe_unicode(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for character in value
    )


def _validate_unicode_text(value: str) -> None:
    if _contains_unsafe_unicode(value.replace("\r\n", "").replace("\n", "")):
        raise ValueError
    # Only LF and CRLF are line separators; bare CR has shell-dependent meaning.
    if "\r" in value.replace("\r\n", ""):
        raise ValueError


def _validate_template(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        raw = _read_bounded(path, missing_ok=True)
    except FileNotFoundError:
        return False
    except ValueError:
        raise SetupImportError("template config is invalid") from None
    except (OSError, TypeError):
        raise SetupImportError("template config path is unsafe") from None
    if raw is None:
        return False
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise SetupImportError("template config is invalid") from None
    finally:
        _zero_buffer(raw)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda _value: _invalid_json(),
        )
        if type(value) is not dict:
            raise ValueError
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise SetupImportError("template config is invalid") from None
    return True


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_json() -> None:
    raise ValueError


def _read_bounded(path: Path, *, missing_ok: bool) -> bytearray | None:
    if not isinstance(path, Path):
        raise TypeError
    candidate = path.absolute()
    if _has_unsafe_ancestor(candidate):
        raise OSError
    try:
        expected = _path_identity(candidate)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    try:
        if os.name == "nt":
            from .setup_store import _open_windows

            descriptor = _open_windows(candidate)
        else:
            descriptor = os.open(
                candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
    except FileNotFoundError:
        raise OSError from None
    try:
        opened = _descriptor_identity(descriptor)
        if opened[:2] != expected[:2]:
            raise OSError
        if opened[2] > _MAX_SOURCE_BYTES:
            raise ValueError
        content = bytearray()
        total = 0
        while True:
            chunk = os.read(descriptor, min(_READ_CHUNK, _MAX_SOURCE_BYTES + 1 - total))
            if not chunk:
                break
            content.extend(chunk)
            total += len(chunk)
            if total > _MAX_SOURCE_BYTES:
                raise ValueError
        final = _descriptor_identity(descriptor)
        if final != opened or total != opened[2]:
            raise OSError
    finally:
        os.close(descriptor)
    try:
        if _path_identity(candidate) != expected:
            raise OSError
    except FileNotFoundError:
        raise OSError from None
    return content


def _zero_buffer(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def _path_identity(path: Path) -> tuple[int, int, int, int]:
    if _is_link_or_reparse(path):
        raise OSError
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise OSError
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _descriptor_identity(descriptor: int) -> tuple[int, int, int, int]:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise OSError
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _has_unsafe_ancestor(path: Path) -> bool:
    current = path
    while True:
        try:
            if _is_link_or_reparse(current):
                return True
        except OSError:
            return True
        if current.parent == current:
            return False
        current = current.parent


__all__ = [
    "ImportDetection",
    "SetupImportError",
    "detect_import_sources",
    "import_selected",
    "parse_dotenv",
]
