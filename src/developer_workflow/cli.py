"""Safe, thin command-line adapter for the developer workflow orchestrator."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TextIO
from urllib.parse import urlsplit

from .config import DeveloperWorkflowConfig
from .contracts import DefectCandidate, WorkflowRun, WorkflowState
from .orchestrator import DeveloperWorkflowOrchestrator


class OrchestratorFactory(Protocol):
    def __call__(self, config: DeveloperWorkflowConfig) -> DeveloperWorkflowOrchestrator: ...


class DefectListClient(Protocol):
    async def list_candidates(
        self,
        project: str,
        iteration: str,
        assignee: str,
        *,
        status_ids: tuple[str, ...] | None = None,
    ) -> tuple[DefectCandidate, ...]: ...

    async def close(self) -> None: ...


class DefectListFactory(Protocol):
    def __call__(self, limit: int, page_size: int) -> DefectListClient: ...


class TuiRunner(Protocol):
    def __call__(self, controller: object, max_concurrency: int) -> None: ...


class SandboxProfileValidator(Protocol):
    def __call__(self, profile: str, environment: dict[str, str]) -> None: ...


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


class _SafeParser(argparse.ArgumentParser):
    """Argparse variant which never reflects an untrusted argv item in an error."""

    output: TextIO
    error_output: TextIO

    def _print_message(self, message: str | None, file: TextIO | None = None) -> None:
        if message:
            _write(self.output, message)

    def error(self, message: str) -> None:
        _write(self.error_output, "error: invalid command arguments\n")
        raise _ParserExit(2)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if message:
            _write(self.error_output if status else self.output, message)
        raise _ParserExit(status)


def _parser(stdout: TextIO, stderr: TextIO) -> _SafeParser:
    parser = _SafeParser(prog="ones-dev", description="ONES developer workflows")
    parser.output, parser.error_output = stdout, stderr
    subparsers = parser.add_subparsers(dest="command", required=True)

    def command(name: str) -> argparse.ArgumentParser:
        item = subparsers.add_parser(name)
        if isinstance(item, _SafeParser):
            item.output, item.error_output = stdout, stderr
        item.add_argument("--config", default="ones-dev.config.json")
        return item

    requirement = command("requirement")
    requirement.add_argument("requirement_id")
    requirement.add_argument("--mapping")

    defect = command("defect")
    defect.add_argument("--project", required=True)
    defect.add_argument("--iteration", required=True)
    defect.add_argument("--assignee", required=True)
    defect.add_argument("--select")
    defect.add_argument("--mapping")

    defects = subparsers.add_parser("defects")
    if isinstance(defects, _SafeParser):
        defects.output, defects.error_output = stdout, stderr
    defect_commands = defects.add_subparsers(dest="defects_command", required=True)
    defect_list = defect_commands.add_parser("list")
    if isinstance(defect_list, _SafeParser):
        defect_list.output, defect_list.error_output = stdout, stderr
    defect_list.add_argument("--project", required=True)
    defect_list.add_argument("--iteration", required=True)
    defect_list.add_argument("--assignee", required=True)
    defect_list.add_argument("--status")
    defect_list.add_argument("--format", choices=("table", "json"), default="table")
    defect_list.add_argument("--limit", type=int, default=5000)
    defect_list.add_argument("--page-size", type=int, default=200)

    for name in ("show", "resume"):
        item = command(name)
        item.add_argument("run_id")

    revise = command("revise")
    revise.add_argument("run_id")
    revise.add_argument("--feedback", required=True)
    revise.add_argument("--scope")

    approve = command("approve")
    approve.add_argument("run_id")
    approve.add_argument("--actor", required=True)

    cancel = command("cancel")
    cancel.add_argument("run_id")
    cancel.add_argument("--actor", required=True)
    command("tui")
    return parser


def _safe_value(value: object, *, maximum: int = 4096) -> str:
    text = str(value)
    result: list[str] = []
    for character in text[:maximum]:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs", "Zl", "Zp"} or character == "\x1b":
            result.append("?")
        else:
            result.append(character)
    return "".join(result)


def _write(stream: TextIO, text: str) -> None:
    encoding = getattr(stream, "encoding", None)
    if isinstance(encoding, str):
        try:
            text = text.encode(encoding, errors="replace").decode(encoding)
        except (LookupError, UnicodeError):
            text = text.encode("ascii", errors="replace").decode("ascii")
    stream.write(text)


def _line(stream: TextIO, label: str, value: object) -> None:
    _write(stream, f"{label}: {_safe_value(value)}\n")


def _show_run(run: WorkflowRun, stdout: TextIO) -> None:
    """Print only the explicit, secret-free terminal status contract."""

    _line(stdout, "run_id", run.run_id)
    _line(stdout, "state", run.state.value)
    if run.blocked_reason:
        _line(stdout, "blocked_reason", run.blocked_reason)
    if run.worktree_path:
        _line(stdout, "worktree", run.worktree_path)
    for result in run.test_results:
        _line(stdout, "tests", result.summary)
    risks = run.approval.risks if run.approval is not None else ()
    if run.review is not None:
        risks = (*risks, *run.review.risks)
    for risk in dict.fromkeys(risks):
        _line(stdout, "risks", risk)
    if run.approval is not None and run.approval.fingerprint:
        _line(stdout, "fingerprint", run.approval.fingerprint)
    if run.approval is not None and run.approval.repository_group is not None:
        _line(stdout, "repository group", run.approval.repository_group.key)
        for item in run.approval.repositories:
            role = item.mapping.role.value
            _line(
                stdout, "repository evidence",
                f"{item.repository_key} | {role} | "
                f"base {item.base_commit} | head {item.head_commit}",
            )
            _line(stdout, "diff", f"{item.repository_key} | {item.diff_hash}")
            if item.tree_hash:
                _line(stdout, "approved tree", f"{item.repository_key} | {item.tree_hash}")
            _line(stdout, "diff summary", f"{item.repository_key} | {item.diff_summary}")
            for path in item.changed_files:
                _line(stdout, "changed file", f"{item.repository_key}:{path}")
            for result in item.tests:
                _line(stdout, "repository test", f"{item.repository_key} | {result.summary}")
        for result in run.approval.integration_tests:
            _line(stdout, "integration test", result.summary)
    if run.publication.pr_url:
        _line(stdout, "pr_url", run.publication.pr_url)
    if run.group_publication is not None:
        by_key = {
            item.repository_key: item for item in run.group_publication.repositories
        }
        for key in run.group_publication.order:
            item = by_key.get(key)
            if item is None:
                _line(stdout, "repository", f"{key} | unchanged")
                continue
            status = (
                "pr-created" if item.pr_url else
                "pushed" if item.push_completed_at is not None else
                "committed" if item.commit_hash else "pending"
            )
            _line(stdout, "repository", f"{key} | {status}")
            if item.commit_hash:
                _line(stdout, "commit", f"{key} | {item.commit_hash}")
            if item.push_completed_at is not None:
                _line(
                    stdout, "push",
                    f"{key} | {item.push_completed_at.isoformat()}",
                )
            if item.pr_url:
                _line(stdout, "pr_url", item.pr_url)
            if item.error:
                _line(stdout, "repository error", f"{key} | {item.error}")
        if run.group_publication.error:
            _line(stdout, "group publication error", run.group_publication.error)


def _show_candidates(candidates: Sequence[DefectCandidate], stdout: TextIO) -> None:
    for index, item in enumerate(candidates, start=1):
        fields = (item.key, item.uuid, item.priority, item.status, item.title, item.status_id)
        _write(stdout, f"{index}. {' | '.join(_safe_value(value, maximum=512) for value in fields)}\n")


def _candidate_output(candidate: DefectCandidate) -> dict[str, str]:
    return {
        "uuid": candidate.uuid,
        "key": candidate.key,
        "number": candidate.number,
        "title": candidate.title,
        "priority": candidate.priority,
        "status": candidate.status,
        "status_id": candidate.status_id,
        "updated_at": candidate.updated_at,
    }


def _parse_status_ids(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    parts = value.split(",")
    if (
        not 1 <= len(parts) <= 128
        or len(parts) != len(set(parts))
        or any(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", part) is None for part in parts)
    ):
        raise ValueError("status ids are invalid")
    return tuple(parts)


async def _read_defect_list(
    client: DefectListClient,
    project: str,
    iteration: str,
    assignee: str,
    status_ids: tuple[str, ...] | None,
) -> tuple[DefectCandidate, ...]:
    try:
        if status_ids is None:
            result = await client.list_candidates(project, iteration, assignee)
        else:
            result = await client.list_candidates(
                project, iteration, assignee, status_ids=status_ids
            )
        return tuple(result)
    finally:
        await client.close()


def _execute_defect_list(
    args: argparse.Namespace,
    factory: DefectListFactory,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if not (
        1 <= args.limit <= 5000
        and 1 <= args.page_size <= 200
        and args.page_size <= args.limit
    ):
        _write(stderr, "error: invalid pagination bounds\n")
        return 2
    try:
        status_ids = _parse_status_ids(args.status)
    except ValueError:
        _write(stderr, "error: invalid status ids\n")
        return 2
    client = factory(args.limit, args.page_size)
    candidates = asyncio.run(
        _read_defect_list(
            client, args.project, args.iteration, args.assignee, status_ids
        )
    )
    if args.format == "json":
        payload = [_candidate_output(candidate) for candidate in candidates]
        _write(stdout, json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    elif candidates:
        _show_candidates(candidates, stdout)
    else:
        _write(stdout, "No open defects.\n")
    return 0


def _show_repositories(run: WorkflowRun, stdout: TextIO) -> None:
    for mapping in run.repository_candidates:
        fields = (mapping.key, mapping.repo_url, mapping.base_branch)
        _write(stdout, "repository: " + " | ".join(_safe_value(value) for value in fields) + "\n")
        for configured in (*mapping.lint_commands, *mapping.build_commands, *mapping.test_commands):
            _line(stdout, "command", configured)
    for group in run.repository_group_candidates:
        _line(stdout, "repository group", group.key)
        _line(stdout, "primary repository", group.primary_repository)
        _line(
            stdout, "local source policy",
            "source_path is read-only input; changes use isolated managed worktrees",
        )
        by_key = {item.key: item for item in group.repositories}
        for index, key in enumerate(group.topological_keys(), start=1):
            mapping = by_key[key]
            source = (
                str(mapping.source_path)
                if mapping.source_path is not None
                else "remote mirror"
            )
            _write(
                stdout,
                f"  {index}. {_safe_value(key)} | {_safe_value(mapping.role.value)} | "
                f"{_safe_value(mapping.base_branch)} | {_safe_value(source)} | "
                f"{_safe_value(mapping.repo_url)}\n",
            )


def _tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _read_line(stdin: TextIO) -> str | None:
    try:
        value = stdin.readline()
    except (EOFError, OSError):
        return None
    return None if value == "" else value.rstrip("\r\n")


def _confirm_mapping(
    orchestrator: DeveloperWorkflowOrchestrator,
    run: WorkflowRun,
    mapping_key: str | None,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> tuple[int, WorkflowRun]:
    if run.state is not WorkflowState.VALIDATING:
        return 0, run
    _show_repositories(run, stdout)
    selected = mapping_key
    if selected is None:
        if not _tty(stdin):
            _write(stderr, "error: --mapping is required when stdin is not a TTY\n")
            return 2, run
        _write(stdout, "mapping key (blank to reject): ")
        selected = _read_line(stdin)
        if selected is None or selected.casefold() in {"", "n", "no"}:
            return 0, run
        if selected.casefold() in {"y", "yes"}:
            candidates = (*run.repository_candidates, *run.repository_group_candidates)
            if len(candidates) != 1:
                _write(stderr, "error: mapping key is required for multiple candidates\n")
                return 2, run
            selected = candidates[0].key
    result = orchestrator.confirm_repository(run.run_id, selected)
    return 0, result


async def _list_candidates(
    orchestrator: DeveloperWorkflowOrchestrator,
    project: str,
    iteration: str,
    assignee: str,
) -> tuple[DefectCandidate, ...]:
    result = await orchestrator.defect_candidates.list_candidates(
        project, iteration, assignee
    )
    return tuple(result)


def _execute(
    args: argparse.Namespace,
    orchestrator: DeveloperWorkflowOrchestrator,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if args.command == "requirement":
        run = orchestrator.start_requirement(args.requirement_id)
        code, run = _confirm_mapping(
            orchestrator, run, args.mapping, stdin=stdin, stdout=stdout, stderr=stderr
        )
        if code == 0:
            _show_run(run, stdout)
        return code

    if args.command == "defect":
        candidates = asyncio.run(
            _list_candidates(orchestrator, args.project, args.iteration, args.assignee)
        )
        _show_candidates(candidates, stdout)
        if not candidates:
            _write(stderr, "error: no verified defect candidates\n")
            return 2
        interactive = _tty(stdin)
        selected_id = None if interactive else args.select
        if interactive:
            _write(stdout, "candidate number: ")
            raw = _read_line(stdin)
            if raw is None or not raw.isascii() or not raw.isdigit():
                _write(stderr, "error: candidate selection is invalid\n")
                return 2
            index = int(raw)
            if index < 1 or index > len(candidates):
                _write(stderr, "error: candidate selection is invalid\n")
                return 2
            selected_id = candidates[index - 1].uuid
        elif selected_id is None:
            _write(stderr, "error: --select is required when stdin is not a TTY\n")
            return 2
        matches = [item for item in candidates if item.uuid == selected_id]
        if len(matches) != 1:
            _write(stderr, "error: selected defect is not in the current snapshot\n")
            return 2
        selected = matches[0]
        run = orchestrator.start_defect(
            args.project,
            args.iteration,
            args.assignee,
            selected.snapshot_token,
            selected.uuid,
        )
        code, run = _confirm_mapping(
            orchestrator, run, args.mapping, stdin=stdin, stdout=stdout, stderr=stderr
        )
        if code == 0:
            _show_run(run, stdout)
        return code

    if args.command == "show":
        run = orchestrator.show(args.run_id)
    elif args.command == "resume":
        run = orchestrator.resume(args.run_id)
    elif args.command == "revise":
        run = orchestrator.revise(args.run_id, args.feedback, scope=args.scope)
    elif args.command == "approve":
        run = orchestrator.approve(args.run_id, args.actor)
    elif args.command == "cancel":
        run = orchestrator.cancel(args.run_id, args.actor)
    else:  # guarded by argparse
        raise RuntimeError("command dispatch is unavailable")
    _show_run(run, stdout)
    return 0


def _valid_comment_path_template(value: str | None) -> bool:
    if type(value) is not str or not value.startswith("/"):
        return False
    if (
        "?" in value
        or "#" in value
        or "\\" in value
        or "//" in value
        or any(part in {"", ".", ".."} for part in value[1:].split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    return set(re.findall(r"\{([^{}]+)\}", value)) == {"team_id", "item_id"}


def _valid_runtime_text(value: str) -> bool:
    return bool(value) and value == value.strip() and not any(
        ord(character) < 32 or ord(character) == 127 for character in value
    )


def _validate_sandbox_permission_profile(
    profile: str, environment: dict[str, str]
) -> None:
    """Prove the managed profile's sandbox capabilities before service startup."""

    from .requirement_flow import SandboxCommandExecutor, sandbox_preflight_command

    with tempfile.TemporaryDirectory(prefix="ones-dev-sandbox-preflight-") as raw:
        cwd = Path(raw).resolve(strict=True)
        executor = SandboxCommandExecutor(permission_profile=profile)
        completed = executor(
            sandbox_preflight_command(),
            cwd=cwd,
            env=dict(environment),
            timeout=20,
            max_output_bytes=64 * 1024,
        )
        if completed.returncode != 0:
            raise RuntimeError("sandbox profile is unavailable")


def build_production_orchestrator(
    config: DeveloperWorkflowConfig,
    *,
    sandbox_profile_validator: SandboxProfileValidator = (
        _validate_sandbox_permission_profile
    ),
) -> DeveloperWorkflowOrchestrator:
    """Build the real local service graph from config plus explicit secret env vars."""
    from config.settings import OnesSettings

    from .runtime_bootstrap import RuntimeBootstrapper, legacy_runtime_inputs

    environment = dict(os.environ)
    active, secrets = legacy_runtime_inputs(
        config, environment, ones_settings=OnesSettings()
    )
    bootstrapper = RuntimeBootstrapper(
        ambient_environment=lambda: environment,
        sandbox_profile_validator=sandbox_profile_validator,
    )
    return bootstrapper.build(active, secrets).orchestrator


class _ProductionDefectListClient:
    def __init__(self, gateway: object, candidates: object) -> None:
        self._gateway = gateway
        self._candidates = candidates

    async def list_candidates(
        self,
        project: str,
        iteration: str,
        assignee: str,
        *,
        status_ids: tuple[str, ...] | None = None,
    ) -> tuple[DefectCandidate, ...]:
        result = await self._candidates.list_candidates(
            project, iteration, assignee, status_ids=status_ids
        )
        return tuple(result)

    async def close(self) -> None:
        await self._gateway.close()


def build_production_defect_list_client(
    limit: int, page_size: int
) -> DefectListClient:
    """Build the minimal ONES-only graph used by the read-only list command."""

    from config.settings import OnesSettings
    from src.services.ones_gateway import OnesGateway

    from .defect_flow import DefectCandidateService

    settings = OnesSettings()
    try:
        ones_url = urlsplit(settings.base_url.rstrip("/"))
    except ValueError:
        ones_url = urlsplit("")
    if not (
        ones_url.scheme in {"http", "https"}
        and ones_url.hostname is not None
        and ones_url.username is None
        and ones_url.password is None
        and not ones_url.query
        and not ones_url.fragment
        and _valid_runtime_text(settings.team_id)
        and _valid_runtime_text(settings.issue_type_id)
        and _valid_runtime_text(settings.email)
        and _valid_runtime_text(settings.password)
    ):
        raise RuntimeError("ONES read-only runtime configuration is incomplete")
    gateway = OnesGateway(settings=settings)
    candidates = DefectCandidateService(
        gateway,
        settings.issue_type_id,
        candidate_limit=limit,
        page_size=page_size,
    )
    return _ProductionDefectListClient(gateway, candidates)


def _execute_tui(
    config: DeveloperWorkflowConfig,
    factory: OrchestratorFactory,
    tui_runner: TuiRunner,
) -> int:
    """Assemble the TUI from the same validated production workflow graph."""

    from .tui import RunIndex, TuiController

    orchestrator = factory(config)
    controller = TuiController(orchestrator, RunIndex(orchestrator.store))
    try:
        tui_runner(controller, config.tui_max_concurrency)
    except BaseException:
        try:
            controller.close()
        except BaseException:
            pass
        raise
    controller.close()
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    factory: OrchestratorFactory = build_production_orchestrator,
    defect_list_factory: DefectListFactory = build_production_defect_list_client,
    tui_runner: TuiRunner | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = _parser(stdout, stderr)
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except _ParserExit as error:
        return error.status
    try:
        if args.command == "defects":
            return _execute_defect_list(
                args, defect_list_factory, stdout=stdout, stderr=stderr
            )
        config = DeveloperWorkflowConfig.load(args.config)
        if args.command == "tui":
            if tui_runner is None:
                from .tui import run_tui

                tui_runner = run_tui
            return _execute_tui(config, factory, tui_runner)
        orchestrator = factory(config)
        return _execute(
            args, orchestrator, stdin=stdin, stdout=stdout, stderr=stderr
        )
    except (KeyboardInterrupt, SystemExit):
        _write(stderr, "error: command interrupted safely\n")
        return 130
    except Exception:
        _write(stderr, "error: command failed safely\n")
        return 1


__all__ = [
    "build_production_defect_list_client",
    "build_production_orchestrator",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
