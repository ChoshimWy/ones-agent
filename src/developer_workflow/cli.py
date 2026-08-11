"""Safe, thin command-line adapter for the developer workflow orchestrator."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import unicodedata
from collections.abc import Sequence
from typing import Protocol, TextIO
from urllib.parse import urlsplit

from .config import DeveloperWorkflowConfig
from .contracts import DefectCandidate, WorkflowRun, WorkflowState
from .orchestrator import DeveloperWorkflowOrchestrator


class OrchestratorFactory(Protocol):
    def __call__(self, config: DeveloperWorkflowConfig) -> DeveloperWorkflowOrchestrator: ...


class DefectListClient(Protocol):
    async def list_candidates(
        self, project: str, iteration: str, assignee: str
    ) -> tuple[DefectCandidate, ...]: ...

    async def close(self) -> None: ...


class DefectListFactory(Protocol):
    def __call__(self, limit: int, page_size: int) -> DefectListClient: ...


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
    if run.publication.pr_url:
        _line(stdout, "pr_url", run.publication.pr_url)


def _show_candidates(candidates: Sequence[DefectCandidate], stdout: TextIO) -> None:
    for index, item in enumerate(candidates, start=1):
        fields = (item.key, item.uuid, item.priority, item.status, item.title)
        _write(stdout, f"{index}. {' | '.join(_safe_value(value, maximum=512) for value in fields)}\n")


def _candidate_output(candidate: DefectCandidate) -> dict[str, str]:
    return {
        "uuid": candidate.uuid,
        "key": candidate.key,
        "number": candidate.number,
        "title": candidate.title,
        "priority": candidate.priority,
        "status": candidate.status,
        "updated_at": candidate.updated_at,
    }


async def _read_defect_list(
    client: DefectListClient,
    project: str,
    iteration: str,
    assignee: str,
) -> tuple[DefectCandidate, ...]:
    try:
        return tuple(await client.list_candidates(project, iteration, assignee))
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
    client = factory(args.limit, args.page_size)
    candidates = asyncio.run(
        _read_defect_list(client, args.project, args.iteration, args.assignee)
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
            if len(run.repository_candidates) != 1:
                _write(stderr, "error: mapping key is required for multiple candidates\n")
                return 2, run
            selected = run.repository_candidates[0].key
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


def build_production_orchestrator(
    config: DeveloperWorkflowConfig,
) -> DeveloperWorkflowOrchestrator:
    """Build the real local service graph from config plus explicit secret env vars."""

    from config.settings import OnesSettings
    from src.services.ones_gateway import OnesGateway

    from .approval_rebuilder import WorkflowApprovalRebuilder
    from .codex_runner import CodexRunner
    from .defect_flow import DefectCandidateService, DefectFlow
    from .ones_comment import OnesCommenter
    from .pr_provider import HttpPullRequestClient, parse_repository_identity
    from .publisher import Publisher
    from .private_paths import prepare_private_roots
    from .repository import WorktreeRepository
    from .requirement_flow import RequirementFlow, SandboxCommandExecutor
    from .state_store import FileRunStore

    settings = OnesSettings()
    provider_token = os.environ.get("ONES_DEV_PROVIDER_TOKEN", "")
    provider_host = os.environ.get("ONES_DEV_PROVIDER_HOST", "").casefold()
    provider_api = os.environ.get("ONES_DEV_PROVIDER_API_URL", "")
    author_name = os.environ.get("ONES_DEV_GIT_AUTHOR_NAME", "")
    author_email = os.environ.get("ONES_DEV_GIT_AUTHOR_EMAIL", "")
    try:
        provider_url = urlsplit(provider_api.rstrip("/"))
        ones_url = urlsplit(settings.base_url.rstrip("/"))
    except ValueError:
        provider_url = ones_url = urlsplit("")
    credential_names = ("GIT_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND", "SSH_ASKPASS", "SSH_AUTH_SOCK")
    git_credential_values = {
        name: value
        for name in credential_names
        if (value := os.environ.get(f"ONES_DEV_{name}", ""))
    }
    if not (
        ones_url.scheme in {"http", "https"}
        and ones_url.hostname is not None
        and ones_url.username is None
        and ones_url.password is None
        and not ones_url.query
        and not ones_url.fragment
        and settings.team_id
        and settings.issue_type_id
        and _valid_runtime_text(settings.email)
        and _valid_runtime_text(settings.password)
        and _valid_comment_path_template(settings.comment_list_path_template)
        and _valid_runtime_text(provider_token)
        and _valid_runtime_text(provider_host)
        and provider_url.scheme == "https"
        and provider_url.hostname is not None
        and provider_url.hostname.casefold() == provider_host
        and provider_url.username is None
        and provider_url.password is None
        and not provider_url.query
        and not provider_url.fragment
        and _valid_runtime_text(author_name)
        and _valid_runtime_text(author_email)
        and all(_valid_runtime_text(value) for value in git_credential_values.values())
        and config.publishing.provider.value in {"github", "gitlab"}
    ):
        raise RuntimeError("production runtime configuration is incomplete")
    for mapping in config.repositories:
        parse_repository_identity(mapping.repo_url, provider_host)

    run_root, mirror_root, worktree_root = prepare_private_roots(
        (config.run_root, config.mirror_root, config.worktree_root)
    )

    def git_credentials() -> dict[str, str]:
        return dict(git_credential_values)

    def git_identity() -> dict[str, str]:
        return {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }

    store = FileRunStore(run_root)
    repository = WorktreeRepository(
        mirror_root,
        worktree_root,
        credential_env_provider=git_credentials,
        identity_env_provider=git_identity,
    )
    gateway = OnesGateway(settings=settings)
    codex = CodexRunner(run_root, repository)
    test_runner = SandboxCommandExecutor(
        permission_profile=config.sandbox_permission_profile
    )
    requirement_flow = RequirementFlow(
        store, gateway, config, repository, codex, test_runner
    )
    defect_flow = DefectFlow(store, config, repository, codex, test_runner)
    candidates = DefectCandidateService(gateway, settings.issue_type_id)
    pr_client = HttpPullRequestClient(
        provider=config.publishing.provider.value,
        provider_host=provider_host,
        api_base_url=provider_api,
        token_provider=lambda: os.environ.get("ONES_DEV_PROVIDER_TOKEN", ""),
    )
    commenter = OnesCommenter(gateway, store)
    publisher = Publisher(
        store,
        repository,
        WorkflowApprovalRebuilder(gateway, repository),
        pr_client,
        commenter,
        config.publishing.provider.value,
        provider_host,
    )
    return DeveloperWorkflowOrchestrator(
        store, requirement_flow, defect_flow, publisher, config, candidates
    )


class _ProductionDefectListClient:
    def __init__(self, gateway: object, candidates: object) -> None:
        self._gateway = gateway
        self._candidates = candidates

    async def list_candidates(
        self, project: str, iteration: str, assignee: str
    ) -> tuple[DefectCandidate, ...]:
        result = await self._candidates.list_candidates(project, iteration, assignee)
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


def main(
    argv: Sequence[str] | None = None,
    *,
    factory: OrchestratorFactory = build_production_orchestrator,
    defect_list_factory: DefectListFactory = build_production_defect_list_client,
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
