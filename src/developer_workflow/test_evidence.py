"""Shared strict selection of the final verification command sequence."""

from __future__ import annotations

from .command_utils import parse_command_argv
from .contracts import (
    CommandResult,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRunEvidence,
)


class FinalTestEvidenceError(ValueError):
    """Persisted results do not contain one exact final verification round."""


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
) -> tuple[CommandResult, ...]:
    commands = (
        reproduction_command,
        *mapping.lint_commands,
        *mapping.build_commands,
        *(command for command in mapping.test_commands if command != reproduction_command),
    )
    try:
        argvs = (
            reproduction_argv,
            *(parse_command_argv(command) for command in commands[1:]),
        )
    except ValueError:
        raise FinalTestEvidenceError("configured verification command is invalid") from None
    return _select_tail(results, commands, argvs)


def select_group_final_tests(
    evidence: tuple[RepositoryRunEvidence, ...],
    integration_results: tuple[CommandResult, ...],
    group: RepositoryGroupMapping,
) -> tuple[tuple[str, CommandResult], ...]:
    """Select one exact configured round per repository and group integration."""

    keys = group.topological_keys()
    if tuple(item.repository_key for item in evidence) != keys:
        raise FinalTestEvidenceError("repository group evidence order changed")
    selected: list[tuple[str, CommandResult]] = []
    for item in evidence:
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
    "select_defect_final_tests",
    "select_group_final_tests",
    "select_requirement_final_tests",
]
