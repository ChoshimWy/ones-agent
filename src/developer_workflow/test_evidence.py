"""Shared strict selection of the final verification command sequence."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .command_utils import display_argv, parse_command_argv
from .contracts import (
    CommandResult,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRunEvidence,
    RootCauseEvidence,
)


class FinalTestEvidenceError(ValueError):
    """Persisted results do not contain one exact final verification round."""


def defect_reproduction_argv(
    evidence: tuple[RootCauseEvidence, ...], mapping: RepositoryMapping,
) -> tuple[str, ...]:
    """Resolve the same focused runner for execution, review and publication."""
    bindings = {(item.reproduction_command, item.test_selector) for item in evidence}
    if len(bindings) != 1:
        raise FinalTestEvidenceError("defect reproduction evidence is inconsistent")
    base, selector = next(iter(bindings))
    argv = parse_command_argv(base)
    if base not in mapping.test_commands and (mapping.test_commands or tuple(part.casefold() for part in argv) not in {
        ("pytest",), ("python", "-m", "pytest"), ("python3", "-m", "pytest"),
        ("py", "-m", "pytest"), ("uv", "run", "pytest"),
    }):
        raise FinalTestEvidenceError("unconfigured defect test runner")
    pytest_tail: tuple[str, ...] | None = None
    executable_name = Path(argv[0]).name.casefold()
    if executable_name in {"pytest", "pytest.exe", "py.test", "py.test.exe"}:
        pytest_tail = argv[1:]
    elif (
        executable_name in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}
        and len(argv) >= 3
        and tuple(part.casefold() for part in argv[1:3]) == ("-m", "pytest")
    ):
        pytest_tail = argv[3:]
    elif (
        executable_name in {"uv", "uv.exe"}
        and len(argv) >= 3
        and tuple(part.casefold() for part in argv[1:3]) == ("run", "pytest")
    ):
        pytest_tail = argv[3:]
    if pytest_tail is not None and mapping.source_path:
        source = Path(mapping.source_path).resolve()
        for candidate in (source / ".venv/Scripts/python.exe", source / ".venv/bin/python"):
            try:
                executable = candidate.resolve(strict=True)
            except OSError:
                continue
            if executable.is_file() and executable.is_relative_to(source):
                argv = (str(executable), "-m", "pytest", *pytest_tail)
                break
    if selector in argv:
        if argv[-1] != selector or argv.count(selector) != 1:
            raise FinalTestEvidenceError(
                "configured defect selector position is ambiguous"
            )
        return argv
    return (*argv, selector)


def defect_verification_prefix(
    reproduction_argv: tuple[str, ...], changed_files: tuple[str, ...],
) -> tuple[tuple[str, ...], ...]:
    """Include sibling Python regression tests, without editing the frozen test."""
    base, selector = reproduction_argv[:-1], reproduction_argv[-1]
    frozen = selector.split("::", 1)[0]
    is_pytest = any(Path(arg).name.casefold() in {"pytest", "pytest.exe", "py.test", "py.test.exe"} for arg in base)
    additional = tuple(sorted(
        path for path in changed_files
        if path != frozen and PurePosixPath(path).suffix == ".py"
        and (PurePosixPath(path).name.startswith("test_") or PurePosixPath(path).name.endswith("_test.py"))
    ))
    if not is_pytest or not additional:
        return (reproduction_argv,)
    return (reproduction_argv, (*base, frozen, *additional))


def _select_tail(
    results: tuple[CommandResult, ...],
    commands: tuple[str, ...],
    argvs: tuple[tuple[str, ...], ...],
) -> tuple[CommandResult, ...]:
    if not commands or len(commands) != len(argvs) or len(results) < len(commands):
        raise FinalTestEvidenceError("final test evidence is incomplete")
    selected = results[-len(commands):]
    if tuple(item.command for item in selected) != commands or tuple(
        item.argv for item in selected
    ) != argvs:
        raise FinalTestEvidenceError("final test evidence command sequence changed")
    return selected


def select_requirement_final_tests(
    results: tuple[CommandResult, ...], mapping: RepositoryMapping
) -> tuple[CommandResult, ...]:
    commands = (*mapping.lint_commands, *mapping.build_commands, *mapping.test_commands)
    try:
        argvs = tuple(parse_command_argv(command) for command in commands)
    except ValueError:
        raise FinalTestEvidenceError("configured verification command is invalid") from None
    return _select_tail(results, commands, argvs)


def select_defect_final_tests(
    results: tuple[CommandResult, ...],
    mapping: RepositoryMapping,
    *,
    reproduction_command: str,
    reproduction_argv: tuple[str, ...],
    changed_files: tuple[str, ...] = (),
) -> tuple[CommandResult, ...]:
    prefix = defect_verification_prefix(reproduction_argv, changed_files)
    commands = (
        reproduction_command,
        *(display_argv(argv) for argv in prefix[1:]),
        *mapping.lint_commands,
        *mapping.build_commands,
        *(command for command in mapping.test_commands if command != reproduction_command),
    )
    try:
        argvs = (
            *prefix,
            *(parse_command_argv(command) for command in commands[len(prefix):]),
        )
    except ValueError:
        raise FinalTestEvidenceError("configured verification command is invalid") from None
    return _select_tail(results, commands, argvs)


def select_group_final_tests(
    evidence: tuple[RepositoryRunEvidence, ...],
    integration_results: tuple[CommandResult, ...],
    group: RepositoryGroupMapping,
    *,
    reproduction_evidence: tuple[RootCauseEvidence, ...] = (),
) -> tuple[tuple[str, CommandResult], ...]:
    """Select one exact configured round per repository and group integration."""

    keys = group.topological_keys()
    if tuple(item.repository_key for item in evidence) != keys:
        raise FinalTestEvidenceError("repository group evidence order changed")
    selected: list[tuple[str, CommandResult]] = []
    owners = {item.reproduction_file.repository_key for item in reproduction_evidence if item.reproduction_file is not None}
    if reproduction_evidence and (len(owners) != 1 or not owners.issubset(keys)):
        raise FinalTestEvidenceError("defect reproduction repository is inconsistent")
    for item in evidence:
        if reproduction_evidence:
            configured = (*item.mapping.lint_commands, *item.mapping.build_commands, *item.mapping.test_commands)
            prefix = (
                defect_verification_prefix(defect_reproduction_argv(reproduction_evidence, item.mapping), item.changed_files)
                if item.repository_key in owners else ()
            )
            commands = (*(display_argv(argv) for argv in prefix), *configured)
            argvs = (*prefix, *(parse_command_argv(command) for command in configured))
            if not commands:
                if item.changed_files or item.test_results:
                    raise FinalTestEvidenceError("changed repository has no verification commands")
                continue
            if len(item.test_results) != len(commands):
                raise FinalTestEvidenceError("defect final test evidence round is incomplete")
            selected.extend((item.repository_key, result) for result in _select_tail(item.test_results, commands, argvs))
            continue
        for result in select_requirement_final_tests(
            item.test_results, item.mapping
        ):
            selected.append((item.repository_key, result))
    commands = group.integration_test_commands
    try:
        argvs = tuple(parse_command_argv(command) for command in commands)
    except ValueError:
        raise FinalTestEvidenceError(
            "configured integration command is invalid"
        ) from None
    if commands:
        for result in _select_tail(integration_results, commands, argvs):
            selected.append((group.primary_repository, result))
    elif integration_results:
        raise FinalTestEvidenceError("unexpected integration test evidence")
    return tuple(selected)


__all__ = [
    "FinalTestEvidenceError",
    "defect_reproduction_argv",
    "defect_verification_prefix",
    "select_defect_final_tests",
    "select_group_final_tests",
    "select_requirement_final_tests",
]
