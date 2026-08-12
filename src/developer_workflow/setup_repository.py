"""Strict, secret-free builders for repository setup drafts."""

from __future__ import annotations

import configparser
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from .command_utils import CommandArgvError, parse_command_argv
from .contracts import RepositoryGroupMapping, RepositoryMapping, RepositoryRole
from .private_paths import (
    _current_user_sid,
    _has_link_or_reparse_ancestor,
    _is_link_or_reparse,
    _windows_descriptor,
    prepare_private_directory,
)
from .repository import MirrorOriginMismatch, WorktreeRepository
from .setup_models import SetupValidationError
from .setup_validation import ReadOnlyRepositoryInspector


_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_GIT_CONFIG_BYTES = 1024 * 1024


def _fail(message: str) -> None:
    raise SetupValidationError(message)


def _strict_string(value: object, *, key: bool = False, wildcard: bool = False) -> str:
    if type(value) is not str:
        _fail("repository draft is invalid")
    pattern = _KEY if key else _IDENTIFIER
    if wildcard and value == "*":
        return value
    if pattern.fullmatch(value) is None:
        _fail("repository draft is invalid")
    return value


def _strict_strings(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str for item in value):
        _fail("repository draft is invalid")
    return value


def _validated_commands(value: object) -> tuple[str, ...]:
    commands = _strict_strings(value)
    try:
        tuple(parse_command_argv(command) for command in commands)
    except (CommandArgvError, TypeError, ValueError):
        _fail("repository draft is invalid")
    return commands


def _validated_allowed_paths(value: object) -> tuple[str, ...]:
    paths = _strict_strings(value)
    parts: list[tuple[str, ...]] = []
    for raw in paths:
        candidate = PurePosixPath(raw)
        raw_parts = tuple(raw.split("/"))
        if (
            not raw
            or raw != candidate.as_posix()
            or candidate.is_absolute()
            or "\\" in raw
            or ":" in raw
            or any(item in {"", ".", ".."} for item in raw_parts)
            or any(ord(character) < 32 or ord(character) == 127 for character in raw)
        ):
            _fail("repository draft is invalid")
        parts.append(raw_parts)
    if len(parts) != len(set(parts)):
        _fail("repository draft is invalid")
    for index, left in enumerate(parts):
        for right in parts[index + 1 :]:
            shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
            if longer[: len(shorter)] == shorter:
                _fail("repository draft is invalid")
    return paths


def _source_owned_by_current_user(path: Path) -> bool:
    try:
        metadata = path.stat()
        if os.name != "nt":
            return metadata.st_uid == os.geteuid()
        owner, _, _ = _windows_descriptor(path)
        return owner == _current_user_sid()
    except (OSError, TypeError, ValueError):
        return False


def _read_origin(source: Path, inspector: ReadOnlyRepositoryInspector) -> str:
    try:
        git_entry = source / ".git"
        config_path = inspector._git_config_path(source, git_entry)
        if _has_link_or_reparse_ancestor(config_path) or _is_link_or_reparse(config_path):
            raise ValueError
        before = config_path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_GIT_CONFIG_BYTES:
            raise ValueError
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(config_path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
            ):
                raise ValueError
            payload = os.read(descriptor, _MAX_GIT_CONFIG_BYTES + 1)
        finally:
            os.close(descriptor)
        after = config_path.stat()
        if len(payload) > _MAX_GIT_CONFIG_BYTES or (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError
        parser = configparser.RawConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        parser.read_string(payload.decode("utf-8", "strict"))
        if any(section.casefold().startswith(("include", "includeif")) for section in parser.sections()):
            raise ValueError
        section = next(
            (item for item in parser.sections() if item.casefold() == 'remote "origin"'),
            None,
        )
        if section is None:
            raise ValueError
        values = [value for key, value in parser.items(section) if key.casefold() == "url"]
        if len(values) != 1 or not values[0]:
            raise ValueError
        return values[0]
    except (OSError, UnicodeError, configparser.Error, StopIteration, ValueError):
        raise SetupValidationError("repository source is invalid") from None


def _isolated_ls_remote(
    inspector: ReadOnlyRepositoryInspector, url: str, *, timeout: float
) -> None:
    """Use the Task 5 inspector's isolated Git boundary for contract URL variants."""

    with tempfile.TemporaryDirectory(prefix="ones-ls-remote-") as raw_private:
        private = prepare_private_directory(Path(raw_private) / "private")
        hooks = private / "empty-hooks"
        hooks.mkdir()
        inspector._run(
            ["git", *inspector._git_configuration(hooks), "ls-remote", "--refs", url],
            cwd=private, private_root=private, hooks=hooks, timeout=timeout,
        )


def _validate_source(
    mapping: RepositoryMapping,
    *,
    inspector: ReadOnlyRepositoryInspector,
    timeout_seconds: int,
) -> None:
    if mapping.source_path is None:
        return
    try:
        lexical = mapping.source_path.absolute()
        source = mapping.source_path.resolve(strict=True)
        if (
            source != lexical
            or not source.is_dir()
            or _has_link_or_reparse_ancestor(source)
            or _is_link_or_reparse(source)
            or not _source_owned_by_current_user(source)
        ):
            raise ValueError
        before = inspector.snapshot(source, timeout=float(timeout_seconds))
        origin = _read_origin(source, inspector)
        if WorktreeRepository._normalized_url(origin) != WorktreeRepository._normalized_url(
            mapping.repo_url
        ):
            raise ValueError
        if isinstance(inspector, ReadOnlyRepositoryInspector):
            _isolated_ls_remote(inspector, mapping.repo_url, timeout=float(timeout_seconds))
        else:
            inspector.ls_remote(source, mapping.repo_url, timeout=float(timeout_seconds))
        after = inspector.snapshot(source, timeout=float(timeout_seconds))
        if before != after or source != mapping.source_path.resolve(strict=True):
            raise ValueError
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise SetupValidationError("repository source is invalid") from None


def build_repository(
    *,
    key: str,
    project_id: str,
    iteration_id: str,
    repo_url: str,
    repo_name: str,
    base_branch: str = "main",
    source_path: Path | None = None,
    role: RepositoryRole = RepositoryRole.PRIMARY,
    depends_on: tuple[str, ...] = (),
    test_commands: tuple[str, ...] = (),
    lint_commands: tuple[str, ...] = (),
    build_commands: tuple[str, ...] = (),
    allowed_paths: tuple[str, ...] = (),
    repository_inspector: ReadOnlyRepositoryInspector | None = None,
    timeout_seconds: int = 10,
) -> RepositoryMapping:
    """Build the existing repository contract after strict setup validation."""

    try:
        key = _strict_string(key, key=True)
        project_id = _strict_string(project_id)
        iteration_id = _strict_string(iteration_id, wildcard=True)
        repo_name = _strict_string(repo_name, key=True)
        if type(repo_url) is not str or type(base_branch) is not str:
            _fail("repository draft is invalid")
        if source_path is not None and not isinstance(source_path, Path):
            _fail("repository draft is invalid")
        if type(role) is not RepositoryRole:
            _fail("repository draft is invalid")
        depends_on = _strict_strings(depends_on)
        test_commands = _validated_commands(test_commands)
        lint_commands = _validated_commands(lint_commands)
        build_commands = _validated_commands(build_commands)
        allowed_paths = _validated_allowed_paths(allowed_paths)
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            _fail("repository draft is invalid")
        mapping = RepositoryMapping.model_validate(
            {
                "key": key, "project_id": project_id, "iteration_id": iteration_id,
                "repo_url": repo_url, "repo_name": repo_name,
                "base_branch": base_branch, "source_path": source_path,
                "role": role, "depends_on": depends_on,
                "test_commands": test_commands, "lint_commands": lint_commands,
                "build_commands": build_commands, "allowed_paths": allowed_paths,
            }
        )
    except SetupValidationError:
        raise
    except (CommandArgvError, MirrorOriginMismatch, TypeError, ValueError, ValidationError):
        raise SetupValidationError("repository draft is invalid") from None
    _validate_source(
        mapping,
        inspector=repository_inspector or ReadOnlyRepositoryInspector(),
        timeout_seconds=timeout_seconds,
    )
    return mapping.model_copy(deep=True)


class RepositoryGroupDraftBuilder:
    """Accumulate immutable repository snapshots and build the existing group contract."""

    def __init__(
        self,
        *,
        key: str | None = None,
        project_id: str | None = None,
        iteration_id: str | None = None,
        integration_test_commands: tuple[str, ...] = (),
        repository_inspector: ReadOnlyRepositoryInspector | None = None,
        timeout_seconds: int = 10,
    ) -> None:
        try:
            self._key = None if key is None else _strict_string(key, key=True)
            self._project_id = None if project_id is None else _strict_string(project_id)
            self._iteration_id = (
                None if iteration_id is None else _strict_string(iteration_id, wildcard=True)
            )
            self._integration_commands = _validated_commands(integration_test_commands)
            if type(timeout_seconds) is not int or timeout_seconds <= 0:
                _fail("repository group is invalid")
        except SetupValidationError:
            raise SetupValidationError("repository group is invalid") from None
        self._inspector = repository_inspector or ReadOnlyRepositoryInspector()
        self._timeout_seconds = timeout_seconds
        self._repositories: dict[str, RepositoryMapping] = {}

    def add(self, repository: RepositoryMapping) -> RepositoryGroupDraftBuilder:
        if type(repository) is not RepositoryMapping or repository.key in self._repositories:
            raise SetupValidationError("repository group is invalid")
        try:
            copied = RepositoryMapping.model_validate(repository.model_dump(round_trip=True))
        except (TypeError, ValueError, ValidationError):
            raise SetupValidationError("repository group is invalid") from None
        self._repositories[copied.key] = copied
        return self

    def build(self, *, primary: str) -> RepositoryGroupMapping:
        try:
            primary = _strict_string(primary, key=True)
            if not self._repositories or primary not in self._repositories:
                raise ValueError
            first = next(iter(self._repositories.values()))
            project_id = self._project_id or first.project_id
            iteration_id = self._iteration_id or first.iteration_id
            key = self._key or primary
            if key in self._repositories:
                raise ValueError
            repositories: list[RepositoryMapping] = []
            for repository in self._repositories.values():
                copied = RepositoryMapping.model_validate(repository.model_dump(round_trip=True))
                copied = copied.validated_update(
                    role=(
                        RepositoryRole.PRIMARY
                        if copied.key == primary
                        else RepositoryRole.DEPENDENCY
                    )
                )
                _validate_source(
                    copied,
                    inspector=self._inspector,
                    timeout_seconds=self._timeout_seconds,
                )
                repositories.append(copied)
            group = RepositoryGroupMapping.model_validate(
                {
                    "key": key, "project_id": project_id, "iteration_id": iteration_id,
                    "primary_repository": primary, "repositories": tuple(repositories),
                    "integration_test_commands": self._integration_commands,
                }
            )
            return group.model_copy(deep=True)
        except SetupValidationError:
            raise
        except (CommandArgvError, TypeError, ValueError, ValidationError):
            raise SetupValidationError("repository group is invalid") from None


__all__ = ["RepositoryGroupDraftBuilder", "build_repository"]
