"""Cross-platform configured-command parsing and stable argv display."""

from __future__ import annotations

import ctypes
import os
import re
import shlex
import subprocess
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit


class CommandArgvError(ValueError):
    pass


_CREDENTIAL_NAMES = (
    "password|passwd|pass|token|access[-_]?token|auth[-_]?token|api[-_]?key|apikey|"
    "private[-_]?key|client[-_]?secret|authorization|cookie|credential|pat"
    "|user|netrc(?:[-_]?file)?"
)
_CREDENTIAL_PATTERN = re.compile(rf"(?i)(?:^|[^a-z0-9])(?:{_CREDENTIAL_NAMES})(?:$|[^a-z0-9])")
_CREDENTIAL_ASSIGNMENT = re.compile(rf"(?i)(?:{_CREDENTIAL_NAMES})\s*[:=]")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _executable_name(value: str) -> str:
    return PureWindowsPath(PurePosixPath(value).name).name.casefold().removesuffix(".exe")


def _actual_executable(argv: tuple[str, ...]) -> str:
    first = _executable_name(argv[0])
    if _ENV_ASSIGNMENT.match(argv[0]):
        raise CommandArgvError("configured command contains environment assignment")
    if first == "env":
        for item in argv[1:]:
            if _ENV_ASSIGNMENT.match(item):
                raise CommandArgvError("configured command contains environment assignment")
            if item in {"-i", "--ignore-environment"}:
                continue
            if item.startswith("-"):
                raise CommandArgvError("configured env wrapper is ambiguous")
            return _executable_name(item)
        raise CommandArgvError("configured env wrapper has no executable")
    if first == "uv" and len(argv) >= 2 and argv[1].casefold() == "run":
        for item in argv[2:]:
            if _ENV_ASSIGNMENT.match(item):
                raise CommandArgvError("configured command contains environment assignment")
            if item in {"--offline", "--frozen", "--no-project", "--isolated"}:
                continue
            if item.startswith("-"):
                raise CommandArgvError("configured uv wrapper is ambiguous")
            return _executable_name(item)
        raise CommandArgvError("configured uv wrapper has no executable")
    return first


def _reject_credential_argv(argv: tuple[str, ...]) -> None:
    actual_executable = _actual_executable(argv)
    is_pytest = actual_executable == "pytest" or (
        actual_executable in {"python", "python3"}
        and "-m" in argv
        and argv.index("-m") + 1 < len(argv)
        and argv[argv.index("-m") + 1].casefold() == "pytest"
    )
    is_curl = actual_executable == "curl"
    curl_sensitive_options = {
        "user", "proxy-user", "netrc", "netrc-file", "cert", "key",
        "proxy-cert", "proxy-key", "oauth2-bearer",
    }
    for index, argument in enumerate(argv):
        normalized = argument.strip("'\"")
        option_name = normalized.split("=", 1)[0].lstrip("-/").replace("_", "-")
        if normalized.startswith(("--", "/")) and _CREDENTIAL_PATTERN.search(option_name):
            raise CommandArgvError("configured command contains credential material")
        if re.fullmatch(r"-p.+", normalized, flags=re.IGNORECASE):
            raise CommandArgvError("configured command contains credential material")
        if normalized.casefold() == "-p" and not is_pytest:
            raise CommandArgvError("configured command contains credential material")
        if is_curl and normalized in {"-u", "-b", "-c", "-E", "-U"}:
            raise CommandArgvError("configured command contains credential material")
        if is_curl and re.fullmatch(r"-[A-Za-z]*[ubcEU].*", normalized):
            raise CommandArgvError("configured command contains credential material")
        if is_curl and option_name.casefold() in curl_sensitive_options:
            raise CommandArgvError("configured command contains credential material")
        if _CREDENTIAL_ASSIGNMENT.search(normalized):
            raise CommandArgvError("configured command contains credential material")
        try:
            parsed = urlsplit(normalized)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.scheme in {"http", "https", "ssh"} and (
            parsed.username is not None or parsed.password is not None
        ):
            raise CommandArgvError("configured command contains credential material")
        if normalized.casefold().startswith(("authorization:", "cookie:", "private-token:")):
            raise CommandArgvError("configured command contains credential material")
        if index and _CREDENTIAL_PATTERN.fullmatch(
            argv[index - 1].strip("'\"").lstrip("-/").replace("_", "-")
        ):
            raise CommandArgvError("configured command contains credential material")


def parse_command_argv(command: str) -> tuple[str, ...]:
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise CommandArgvError("configured command is invalid")
    try:
        if os.name != "nt":
            argv = shlex.split(command, posix=True)
        else:
            count = ctypes.c_int()
            parse = ctypes.windll.shell32.CommandLineToArgvW
            parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
            parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
            pointer = parse(command, ctypes.byref(count))
            if not pointer:
                raise OSError("CommandLineToArgvW failed")
            try:
                argv = [pointer[index] for index in range(count.value)]
            finally:
                ctypes.windll.kernel32.LocalFree(pointer)
    except (OSError, ValueError) as error:
        raise CommandArgvError("configured command cannot be parsed") from error
    if not argv or any(not item or "\x00" in item for item in argv):
        raise CommandArgvError("configured command has invalid argv")
    result = tuple(argv)
    _reject_credential_argv(result)
    return result


def display_argv(argv: tuple[str, ...]) -> str:
    if not argv or any(not item or "\x00" in item for item in argv):
        raise CommandArgvError("argv is invalid")
    return subprocess.list2cmdline(argv)


__all__ = ["CommandArgvError", "display_argv", "parse_command_argv"]
