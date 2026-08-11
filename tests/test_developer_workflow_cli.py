from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path

import pytest

from src.developer_workflow.config import DeveloperWorkflowConfig
from src.developer_workflow.contracts import (
    ApprovalPackage,
    CommandResult,
    DefectCandidate,
    MultiRepositoryPublicationResult,
    PublicationResult,
    RepositoryApprovalEvidence,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryPublicationResult,
    RepositoryRole,
    WorkflowRun,
    WorkflowState,
    WorkflowType,
    utc_now,
)


class Terminal(io.StringIO):
    def __init__(self, value: str = "", *, tty: bool) -> None:
        super().__init__(value)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _mapping(key: str = "repo") -> RepositoryMapping:
    return RepositoryMapping(
        key=key,
        project_id="PROJ",
        iteration_id="ITER",
        repo_url="ssh://git@example.invalid/team/repo.git",
        repo_name="repo",
        base_branch="main",
        test_commands=("uv run pytest",),
        lint_commands=("uv run ruff check .",),
    )


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "run_root": str(tmp_path / "runs"),
                "worktree_root": str(tmp_path / "worktrees"),
                "mirror_root": str(tmp_path / "mirrors"),
                "sandbox_permission_profile": "managed-dev",
                "max_codex_attempts": 2,
                "repositories": [_mapping().model_dump(mode="json")],
                "publishing": {"provider": "github"},
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(run_type: WorkflowType = WorkflowType.REQUIREMENT) -> WorkflowRun:
    if run_type is WorkflowType.DEFECT:
        run = WorkflowRun.new_defect("PROJ", "ITER", "USER", "DEF-1")
    else:
        run = WorkflowRun.new(run_type, "REQ-1")
    return run.validated_update(
        state=WorkflowState.VALIDATING,
        project_id="PROJ",
        iteration_id="ITER",
        repository_candidates=(_mapping(),),
    )


def test_group_confirmation_display_is_topological_and_marks_primary() -> None:
    from src.developer_workflow.cli import _show_repositories

    dependency = _mapping("sdk").validated_update(
        repo_name="sdk", role=RepositoryRole.DEPENDENCY
    )
    primary = _mapping("app").validated_update(
        repo_name="app", role=RepositoryRole.PRIMARY, depends_on=("sdk",)
    )
    group = RepositoryGroupMapping(
        key="suite", project_id="PROJ", iteration_id="ITER",
        primary_repository="app", repositories=(dependency, primary),
    )
    run = _run().validated_update(
        repository_candidates=(), repository_group_candidates=(group,)
    )
    output = Terminal(tty=False)

    _show_repositories(run, output)

    rendered = output.getvalue()
    assert "repository group: suite" in rendered
    assert "primary repository: app" in rendered
    assert "source_path is read-only input" in rendered
    assert rendered.index("1. sdk") < rendered.index("2. app")


def test_group_waiting_approval_show_includes_per_repository_evidence() -> None:
    from src.developer_workflow.cli import _show_run

    dependency = _mapping("sdk").validated_update(
        repo_name="sdk", role=RepositoryRole.DEPENDENCY
    )
    primary = _mapping("app").validated_update(
        repo_name="app", role=RepositoryRole.PRIMARY, depends_on=("sdk",)
    )
    group = RepositoryGroupMapping(
        key="suite", project_id="PROJ", iteration_id="ITER",
        primary_repository="app", repositories=(dependency, primary),
    )
    repositories = tuple(
        RepositoryApprovalEvidence(
            repository_key=mapping.key, mapping=mapping,
            base_commit="a" * 40, head_commit="b" * 40,
            diff_hash=("c" if mapping.key == "sdk" else "d") * 64,
            diff_summary=f"changed {mapping.key}",
            branch=f"codex/REQ-1-{mapping.key}",
            changed_files=(f"src/{mapping.key}.py",), tests=(),
            tree_hash=("e" if mapping.key == "sdk" else "f") * 40,
            commit_message=f"fix({mapping.key}): change",
            pr_title=f"REQ-1 [{mapping.key}]", pr_body=f"Change {mapping.key}",
        )
        for mapping in group.repositories
    )
    approval = ApprovalPackage(
        work_item_id="REQ-1", repository_group=group,
        repositories=repositories, fingerprint="f" * 64,
    )
    run = _run().model_copy(update={
        "state": WorkflowState.WAITING_APPROVAL, "approval": approval,
        "repository_group": group, "repository_model_version": 2,
    })
    output = Terminal(tty=False)

    _show_run(run, output)

    rendered = output.getvalue()
    assert "repository evidence: sdk | dependency" in rendered
    assert "repository evidence: app | primary" in rendered
    assert "changed file: sdk:src/sdk.py" in rendered
    assert "approved tree: sdk | " + "e" * 40 in rendered
    assert "diff summary: app | changed app" in rendered

    marker = f"<!-- ones-dev-run:{run.run_id} -->"
    item = repositories[0]
    publication = MultiRepositoryPublicationResult(
        order=group.topological_keys(), comment_marker=marker,
        error="PR creation outcome is uncertain",
        repositories=(RepositoryPublicationResult(
            repository_key=item.repository_key,
            approved_fingerprint=approval.fingerprint,
            repo_url=item.mapping.repo_url, provider="github",
            provider_host="example.invalid", expected_parent=item.head_commit,
            expected_tree="e" * 40, commit_message=item.commit_message,
            remote_branch=item.branch,
            pr_marker=f"ones-dev-run:{run.run_id}:{item.repository_key}",
            pr_base=item.mapping.base_branch, pr_head=item.branch,
            pr_title=item.pr_title, pr_body=item.pr_body,
            comment_marker=marker, commit_hash="1" * 40,
            push_completed_at=utc_now(), error="PR creation outcome is uncertain",
        ),),
    )
    output = Terminal(tty=False)
    _show_run(run.model_copy(update={"group_publication": publication}), output)
    rendered = output.getvalue()
    assert "commit: sdk | " + "1" * 40 in rendered
    assert "push: sdk | " in rendered
    assert "repository error: sdk | PR creation outcome is uncertain" in rendered
    assert "group publication error: PR creation outcome is uncertain" in rendered


class Candidates:
    async def list_candidates(self, project: str, iteration: str, assignee: str):
        assert (project, iteration, assignee) == ("PROJ", "ITER", "USER")
        return (
            DefectCandidate(
                uuid="DEF-1",
                key="BUG-7",
                number="7",
                title="Crash",
                priority="High",
                status="Doing",
                updated_at="2026-08-11T00:00:00Z",
                snapshot_token="snapshot-1",
            ),
            DefectCandidate(
                uuid="DEF-2",
                key="BUG-8",
                number="8",
                title="Freeze",
                priority="Low",
                status="Open",
                updated_at="2026-08-11T00:00:01Z",
                snapshot_token="snapshot-1",
            ),
        )


class Orchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.defect_candidates = Candidates()

    def start_requirement(self, requirement_id: str) -> WorkflowRun:
        self.calls.append(("requirement", requirement_id))
        return _run()

    def start_defect(self, *args: str) -> WorkflowRun:
        self.calls.append(("defect", *args))
        return _run(WorkflowType.DEFECT)

    def confirm_repository(self, run_id: str, mapping: str) -> WorkflowRun:
        self.calls.append(("confirm", run_id, mapping))
        return _run().validated_update(
            run_id=run_id,
            state=WorkflowState.WAITING_APPROVAL,
            repository=_mapping(mapping),
        )

    def show(self, run_id: str) -> WorkflowRun:
        self.calls.append(("show", run_id))
        timestamp = utc_now()
        return WorkflowRun.new("requirement", "REQ-1").validated_update(
            run_id=run_id,
            state=WorkflowState.WAITING_APPROVAL,
            version=4,
            worktree_path="C:/safe/worktree",
            test_results=(
                CommandResult(
                    command="secret command must not print",
                    argv=("secret",),
                    exit_code=0,
                    summary="2 passed",
                    started_at=timestamp,
                    finished_at=timestamp,
                ),
            ),
            approval=ApprovalPackage(
                work_item_id="REQ-1",
                risks=("manual migration",),
                fingerprint="a" * 64,
            ),
            publication=PublicationResult(pr_url=""),
        )

    def resume(self, run_id: str) -> WorkflowRun:
        self.calls.append(("resume", run_id))
        return self.show(run_id)

    def revise(self, run_id: str, feedback: str, *, scope: str | None = None) -> WorkflowRun:
        self.calls.append(("revise", run_id, feedback, scope))
        return self.show(run_id)

    def approve(self, run_id: str, actor: str) -> WorkflowRun:
        self.calls.append(("approve", run_id, actor))
        return self.show(run_id)

    def cancel(self, run_id: str, actor: str) -> WorkflowRun:
        self.calls.append(("cancel", run_id, actor))
        return self.show(run_id).validated_update(state=WorkflowState.CANCELLED)


def invoke(tmp_path: Path, argv: list[str], *, stdin: Terminal | None = None):
    from src.developer_workflow.cli import main

    orchestrator = Orchestrator()
    output, error = Terminal(tty=False), Terminal(tty=False)
    code = main(
        [*argv, "--config", str(_config_file(tmp_path))],
        factory=lambda config: orchestrator,
        stdin=stdin or Terminal(tty=False),
        stdout=output,
        stderr=error,
    )
    return code, output.getvalue(), error.getvalue(), orchestrator


def test_module_is_importable_and_help_lists_exact_commands() -> None:
    from src.developer_workflow.cli import main

    output = Terminal(tty=False)
    code = main(["--help"], stdout=output, stderr=Terminal(tty=False))
    assert code == 0
    for command in ("requirement", "defect", "defects", "show", "resume", "revise", "approve", "cancel"):
        assert command in output.getvalue()


def test_tui_parser_accepts_custom_config_path() -> None:
    from src.developer_workflow.cli import _parser

    args = _parser(Terminal(tty=False), Terminal(tty=False)).parse_args(
        ["tui", "--config", "custom.json"]
    )

    assert args.command == "tui"
    assert args.config == "custom.json"


class DefectListClient:
    def __init__(self, candidates: tuple[DefectCandidate, ...] | None = None) -> None:
        self.candidates = candidates if candidates is not None else Candidates().list_candidates
        self.calls: list[tuple[object, ...]] = []
        self.closed = False

    async def list_candidates(
        self,
        project: str,
        iteration: str,
        assignee: str,
        *,
        status_ids: tuple[str, ...] | None = None,
    ) -> tuple[DefectCandidate, ...]:
        self.calls.append((project, iteration, assignee, status_ids))
        if callable(self.candidates):
            return await self.candidates(project, iteration, assignee)
        return self.candidates

    async def close(self) -> None:
        self.closed = True


def _invoke_defect_list(
    argv: list[str], client: DefectListClient
) -> tuple[int, str, str, list[tuple[int, int]]]:
    from src.developer_workflow.cli import main

    built: list[tuple[int, int]] = []

    def defect_list_factory(limit: int, page_size: int) -> DefectListClient:
        built.append((limit, page_size))
        return client

    output, error = Terminal(tty=False), Terminal(tty=False)
    code = main(
        argv,
        factory=lambda config: (_ for _ in ()).throw(AssertionError("workflow factory called")),
        defect_list_factory=defect_list_factory,
        stdout=output,
        stderr=error,
    )
    return code, output.getvalue(), error.getvalue(), built


def test_defects_list_is_read_only_table_with_bounded_pagination() -> None:
    client = DefectListClient()
    code, output, error, built = _invoke_defect_list(
        [
            "defects", "list", "--project", "PROJ", "--iteration", "ITER",
            "--assignee", "USER", "--limit", "40", "--page-size", "20",
        ],
        client,
    )
    assert code == 0 and error == ""
    assert built == [(40, 20)]
    assert client.calls == [("PROJ", "ITER", "USER", None)]
    assert client.closed is True
    assert "BUG-7 | DEF-1 | High | Doing | Crash" in output
    assert "snapshot-1" not in output


def test_defects_list_json_uses_an_explicit_output_allowlist() -> None:
    client = DefectListClient()
    code, output, error, _ = _invoke_defect_list(
        [
            "defects", "list", "--project", "PROJ", "--iteration", "ITER",
            "--assignee", "USER", "--format", "json",
        ],
        client,
    )
    assert code == 0 and error == ""
    payload = json.loads(output)
    assert payload[0] == {
        "uuid": "DEF-1",
        "key": "BUG-7",
        "number": "7",
        "title": "Crash",
        "priority": "High",
        "status": "Doing",
        "status_id": "",
        "updated_at": "2026-08-11T00:00:00Z",
    }
    assert "snapshot_token" not in output and "source" not in output


def test_defects_list_passes_exact_comma_separated_status_ids() -> None:
    client = DefectListClient()
    code, output, error, built = _invoke_defect_list(
        [
            "defects", "list", "--project", "PROJ", "--iteration", "ITER",
            "--assignee", "USER", "--status", "CKA6U955,WwhszYN8",
        ],
        client,
    )
    assert code == 0 and error == "" and output
    assert built == [(5000, 200)]
    assert client.calls == [
        ("PROJ", "ITER", "USER", ("CKA6U955", "WwhszYN8"))
    ]


@pytest.mark.parametrize(
    "value",
    ["", ",CKA6U955", "CKA6U955,", "CKA6U955,,WwhszYN8", "CKA6U955,CKA6U955", "待处理", "CKA6U955, WwhszYN8"],
)
def test_defects_list_rejects_malformed_status_ids_before_building_client(
    value: str,
) -> None:
    client = DefectListClient()
    code, _, error, built = _invoke_defect_list(
        [
            "defects", "list", "--project", "PROJ", "--iteration", "ITER",
            "--assignee", "USER", "--status", value,
        ],
        client,
    )
    assert code == 2 and "invalid" in error
    assert built == [] and client.calls == []


def test_defects_list_empty_result_is_success() -> None:
    client = DefectListClient(())
    code, output, error, _ = _invoke_defect_list(
        ["defects", "list", "--project", "PROJ", "--iteration", "ITER", "--assignee", "USER"],
        client,
    )
    assert code == 0 and error == "" and output == "No open defects.\n"
    assert client.closed is True


@pytest.mark.parametrize(
    "extra",
    [
        ["--limit", "0"],
        ["--limit", "5001"],
        ["--page-size", "0"],
        ["--page-size", "201"],
        ["--limit", "10", "--page-size", "11"],
    ],
)
def test_defects_list_rejects_invalid_pagination_before_building_client(
    extra: list[str],
) -> None:
    client = DefectListClient()
    code, _, error, built = _invoke_defect_list(
        [
            "defects", "list", "--project", "PROJ", "--iteration", "ITER",
            "--assignee", "USER", *extra,
        ],
        client,
    )
    assert code == 2 and "invalid" in error
    assert built == [] and client.calls == [] and client.closed is False


def test_defects_list_redacts_read_failure_and_closes_client() -> None:
    class ExplodingClient(DefectListClient):
        async def list_candidates(self, project: str, iteration: str, assignee: str):
            raise RuntimeError("password=hunter2\x1b[31m")

    client = ExplodingClient(())
    code, output, error, _ = _invoke_defect_list(
        ["defects", "list", "--project", "PROJ", "--iteration", "ITER", "--assignee", "USER"],
        client,
    )
    assert code == 1 and output == ""
    assert "hunter2" not in error and "\x1b" not in error
    assert client.closed is True


def test_read_only_defect_list_factory_requires_only_supported_ones_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.cli import build_production_defect_list_client

    values = {
        "ONES_BASE_URL": "https://ones.invalid",
        "ONES_TEAM_ID": "TEAM",
        "ONES_ISSUE_TYPE_ID": "BUG",
        "ONES_EMAIL": "developer@example.invalid",
        "ONES_PASSWORD": "runtime-password",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in (
        "ONES_COMMENT_LIST_PATH_TEMPLATE",
        "ONES_DEV_PROVIDER_TOKEN",
        "ONES_DEV_PROVIDER_HOST",
        "ONES_DEV_PROVIDER_API_URL",
        "ONES_DEV_GIT_AUTHOR_NAME",
        "ONES_DEV_GIT_AUTHOR_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)

    client = build_production_defect_list_client(25, 10)
    assert client._candidates.candidate_limit == 25
    assert client._candidates.page_size == 10
    asyncio.run(client.close())


def test_read_only_defect_list_factory_rejects_token_only_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.cli import build_production_defect_list_client

    monkeypatch.setenv("ONES_BASE_URL", "https://ones.invalid")
    monkeypatch.setenv("ONES_TEAM_ID", "TEAM")
    monkeypatch.setenv("ONES_ISSUE_TYPE_ID", "BUG")
    monkeypatch.setenv("ONES_API_TOKEN", "unsupported-token")
    monkeypatch.setenv("ONES_EMAIL", "")
    monkeypatch.setenv("ONES_PASSWORD", "")
    with pytest.raises(RuntimeError, match="read-only runtime configuration"):
        build_production_defect_list_client(25, 10)


def test_requirement_non_tty_requires_mapping_without_confirming(tmp_path: Path) -> None:
    code, output, error, orchestrator = invoke(tmp_path, ["requirement", "REQ-1"])
    assert code != 0
    assert "repo | ssh://git@example.invalid/team/repo.git | main" in output
    assert "uv run pytest" in output
    assert [call[0] for call in orchestrator.calls] == ["requirement"]
    assert "mapping" in error


def test_requirement_non_tty_confirms_only_explicit_mapping(tmp_path: Path) -> None:
    code, _, _, orchestrator = invoke(
        tmp_path, ["requirement", "REQ-1", "--mapping", "repo"]
    )
    assert code == 0
    assert [call[0] for call in orchestrator.calls] == ["requirement", "confirm"]


def test_requirement_tty_rejection_leaves_validating(tmp_path: Path) -> None:
    code, output, _, orchestrator = invoke(
        tmp_path, ["requirement", "REQ-1"], stdin=Terminal("n\n", tty=True)
    )
    assert code == 0
    assert "state: VALIDATING" in output
    assert output.count("state: VALIDATING") == 1
    assert [call[0] for call in orchestrator.calls] == ["requirement"]


def test_defect_non_tty_requires_exact_select_and_uses_fresh_snapshot(tmp_path: Path) -> None:
    code, output, error, orchestrator = invoke(
        tmp_path, ["defect", "--project", "PROJ", "--iteration", "ITER", "--assignee", "USER"]
    )
    assert code != 0
    assert "1. BUG-7 | DEF-1 | High | Doing | Crash" in output
    assert "2. BUG-8 | DEF-2 | Low | Open | Freeze" in output
    assert not orchestrator.calls
    assert "--select" in error

    code, _, _, orchestrator = invoke(
        tmp_path,
        [
            "defect", "--project", "PROJ", "--iteration", "ITER", "--assignee", "USER",
            "--select", "DEF-2", "--mapping", "repo",
        ],
    )
    assert code == 0
    assert orchestrator.calls[0] == (
        "defect", "PROJ", "ITER", "USER", "snapshot-1", "DEF-2"
    )
    assert orchestrator.calls[1][0] == "confirm"


def test_defect_tty_uses_exact_number_not_default_first(tmp_path: Path) -> None:
    code, _, _, orchestrator = invoke(
        tmp_path,
        ["defect", "--project", "PROJ", "--iteration", "ITER", "--assignee", "USER", "--mapping", "repo"],
        stdin=Terminal("2\n", tty=True),
    )
    assert code == 0
    assert orchestrator.calls[0][-1] == "DEF-2"


def test_defect_tty_selection_is_the_prompted_number_even_if_select_is_present(
    tmp_path: Path,
) -> None:
    code, _, _, orchestrator = invoke(
        tmp_path,
        [
            "defect", "--project", "PROJ", "--iteration", "ITER", "--assignee", "USER",
            "--select", "DEF-1", "--mapping", "repo",
        ],
        stdin=Terminal("2\n", tty=True),
    )
    assert code == 0
    assert orchestrator.calls[0][-1] == "DEF-2"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["show", "RUN"], ("show", "RUN")),
        (["resume", "RUN"], ("resume", "RUN")),
        (["revise", "RUN", "--feedback", "fix", "--scope", "implementation"], ("revise", "RUN", "fix", "implementation")),
        (["approve", "RUN", "--actor", "alice"], ("approve", "RUN", "alice")),
        (["cancel", "RUN", "--actor", "alice"], ("cancel", "RUN", "alice")),
    ],
)
def test_existing_run_commands_are_thin_orchestrator_calls(
    tmp_path: Path, argv: list[str], expected: tuple[object, ...]
) -> None:
    code, output, _, orchestrator = invoke(tmp_path, argv)
    assert code == 0
    assert expected in orchestrator.calls
    assert "run_id: RUN" in output and "state:" in output


def test_output_is_allowlisted_and_does_not_dump_sensitive_fields(tmp_path: Path) -> None:
    code, output, _, _ = invoke(tmp_path, ["show", "RUN"])
    assert code == 0
    assert "tests: 2 passed" in output
    assert "risks: manual migration" in output
    assert f"fingerprint: {'a' * 64}" in output
    assert "secret command" not in output
    assert "description" not in output and "model_dump" not in output


def test_errors_are_redacted_and_terminal_controls_are_never_emitted(tmp_path: Path) -> None:
    from src.developer_workflow.cli import main

    class ExplodingFactory:
        def __call__(self, config: DeveloperWorkflowConfig):
            raise RuntimeError("token=secret\x1b[31m")

    output, error = Terminal(tty=False), Terminal(tty=False)
    code = main(
        ["show", "RUN", "--config", str(_config_file(tmp_path))],
        factory=ExplodingFactory(),
        stdout=output,
        stderr=error,
    )
    assert code != 0
    assert "secret" not in error.getvalue()
    assert "\x1b" not in output.getvalue() + error.getvalue()


def test_output_replaces_characters_not_supported_by_terminal_encoding(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.cli import main

    raw = io.BytesIO()
    output = io.TextIOWrapper(raw, encoding="gbk", errors="strict")
    orchestrator = Orchestrator()
    orchestrator.show = lambda run_id: Orchestrator.show(orchestrator, run_id).validated_update(
        blocked_reason="emoji \U0001f680"
    )
    code = main(
        ["show", "RUN", "--config", str(_config_file(tmp_path))],
        factory=lambda config: orchestrator,
        stdout=output,
        stderr=Terminal(tty=False),
    )
    output.flush()
    assert code == 0
    assert b"emoji" in raw.getvalue()


def test_default_config_path_is_ones_dev_config_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.developer_workflow.cli import main

    seen: list[object] = []

    def load(path: object):
        seen.append(path)
        raise ValueError("stop")

    monkeypatch.setattr(DeveloperWorkflowConfig, "load", load)
    assert main(["show", "RUN"], stderr=Terminal(tty=False)) != 0
    assert seen == ["ones-dev.config.json"]


def test_production_factory_fails_closed_when_runtime_secrets_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow.cli import build_production_orchestrator

    for name in (
        "ONES_EMAIL", "ONES_PASSWORD", "ONES_API_TOKEN", "ONES_TEAM_ID",
        "ONES_ISSUE_TYPE_ID", "ONES_DEV_PROVIDER_TOKEN", "ONES_DEV_PROVIDER_HOST",
        "ONES_DEV_PROVIDER_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="production runtime configuration is incomplete"):
        build_production_orchestrator(DeveloperWorkflowConfig.load(_config_file(tmp_path)))


def test_production_factory_rejects_unused_api_token_auth_before_creating_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow.cli import build_production_orchestrator

    monkeypatch.setenv("ONES_API_TOKEN", "unsupported-token")
    monkeypatch.delenv("ONES_EMAIL", raising=False)
    monkeypatch.delenv("ONES_PASSWORD", raising=False)
    _set_complete_non_ones_runtime(monkeypatch)
    config = DeveloperWorkflowConfig.load(_config_file(tmp_path))
    with pytest.raises(RuntimeError, match="production runtime configuration is incomplete"):
        build_production_orchestrator(config)
    assert not config.run_root.exists()
    assert not config.mirror_root.exists()
    assert not config.worktree_root.exists()


def test_production_factory_requires_valid_comment_list_path_before_creating_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow.cli import build_production_orchestrator

    monkeypatch.setenv("ONES_EMAIL", "developer@example.invalid")
    monkeypatch.setenv("ONES_PASSWORD", "runtime-password")
    monkeypatch.delenv("ONES_COMMENT_LIST_PATH_TEMPLATE", raising=False)
    _set_complete_non_ones_runtime(monkeypatch)
    config = DeveloperWorkflowConfig.load(_config_file(tmp_path))
    with pytest.raises(RuntimeError, match="production runtime configuration is incomplete"):
        build_production_orchestrator(config)
    assert not config.run_root.exists()
    assert not config.mirror_root.exists()
    assert not config.worktree_root.exists()


def test_production_factory_rejects_provider_url_before_creating_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow.cli import build_production_orchestrator

    _set_complete_non_ones_runtime(monkeypatch)
    monkeypatch.setenv("ONES_EMAIL", "developer@example.invalid")
    monkeypatch.setenv("ONES_PASSWORD", "runtime-password")
    monkeypatch.setenv(
        "ONES_COMMENT_LIST_PATH_TEMPLATE",
        "/project/api/project/team/{team_id}/task/{item_id}/comments",
    )
    monkeypatch.setenv("ONES_DEV_PROVIDER_API_URL", "http://example.invalid/api")
    config = DeveloperWorkflowConfig.load(_config_file(tmp_path))
    with pytest.raises(RuntimeError, match="production runtime configuration is incomplete"):
        build_production_orchestrator(config)
    assert not config.run_root.exists()
    assert not config.mirror_root.exists()
    assert not config.worktree_root.exists()


def _set_complete_non_ones_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "ONES_BASE_URL": "https://ones.invalid",
        "ONES_TEAM_ID": "TEAM",
        "ONES_ISSUE_TYPE_ID": "BUG",
        "ONES_DEV_PROVIDER_TOKEN": "runtime-only-provider-token",
        "ONES_DEV_PROVIDER_HOST": "example.invalid",
        "ONES_DEV_PROVIDER_API_URL": "https://example.invalid/api/v3",
        "ONES_DEV_GIT_AUTHOR_NAME": "ONES Dev",
        "ONES_DEV_GIT_AUTHOR_EMAIL": "ones-dev@example.invalid",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_production_factory_builds_the_real_service_graph_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow.cli import build_production_orchestrator
    from src.developer_workflow.publisher import Publisher
    from src.developer_workflow.requirement_flow import RequirementFlow
    from src.developer_workflow.defect_flow import DefectFlow

    _set_complete_non_ones_runtime(monkeypatch)
    monkeypatch.setenv("ONES_EMAIL", "developer@example.invalid")
    monkeypatch.setenv("ONES_PASSWORD", "runtime-password")
    monkeypatch.setenv(
        "ONES_COMMENT_LIST_PATH_TEMPLATE",
        "/project/api/project/team/{team_id}/task/{item_id}/comments",
    )

    orchestrator = build_production_orchestrator(
        DeveloperWorkflowConfig.load(_config_file(tmp_path))
    )

    assert isinstance(orchestrator.requirement_flow, RequirementFlow)
    assert isinstance(orchestrator.defect_flow, DefectFlow)
    assert isinstance(orchestrator.publisher, Publisher)
    assert orchestrator.requirement_flow.store is orchestrator.store
    assert orchestrator.defect_flow.repository is orchestrator.requirement_flow.repository


@pytest.mark.parametrize(
    "command",
    [
        "tool --password hunter2",
        "tool --token=hunter2",
        "tool --api-key hunter2",
        "tool CLIENT_SECRET=hunter2",
        "tool https://user:hunter2@example.invalid/path",
        "tool 'https://example.invalid/path?access_token=hunter2'",
        "tool -H 'Authorization: Bearer hunter2'",
        "tool --cookie session=hunter2",
        "tool -phunter2",
        "curl -u user:hunter2 https://example.invalid",
        "curl --user user:hunter2 https://example.invalid",
        "curl -uuser:hunter2 https://example.invalid",
        "curl --proxy-user proxy:hunter2 https://example.invalid",
        "curl --netrc https://example.invalid",
        "curl --cert C:/secrets/client.pem https://example.invalid",
        "curl --key=C:/secrets/client.key https://example.invalid",
        "curl --proxy-cert C:/secrets/proxy.pem https://example.invalid",
        "curl --proxy-key=C:/secrets/proxy.key https://example.invalid",
        "curl -ualice https://example.invalid",
        "uv run curl -ualice https://example.invalid",
        "curl -bsession https://example.invalid",
        "curl -cC:/secrets/cookies https://example.invalid",
        "curl -EC:/secrets/client.pem https://example.invalid",
        "env AWS_SECRET_ACCESS_KEY=hunter2 tool",
        "AWS_SECRET_ACCESS_KEY=hunter2 tool",
        "tool -p hunter2",
        "tool --pass hunter2",
        "tool --netrc-file C:/secrets",
    ],
)
def test_repository_commands_reject_credential_bearing_argv_without_echo(
    command: str,
) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as caught:
        _mapping().validated_update(test_commands=(command,))
    assert "hunter2" not in str(caught.value)


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest -p no:warnings tests/test_api.py",
        "python -m pytest --maxfail=1",
        "ruff check src tests",
        "npm run test -- --runInBand",
        "curl --fail --silent https://example.invalid/health",
        "uv run curl --fail --silent https://example.invalid/health",
        "uv run pytest -p no:warnings tests/test_api.py",
        "python -u script.py",
    ],
)
def test_repository_commands_keep_normal_test_arguments(command: str) -> None:
    assert _mapping().validated_update(test_commands=(command,)).test_commands == (command,)


def test_packaging_declares_root_entry_modules_and_workflow_schema() -> None:
    import tomllib

    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    setuptools = data["tool"]["setuptools"]
    assert {"server", "main"}.issubset(set(setuptools["py-modules"]))
    assert "schemas/*.json" in setuptools["package-data"]["src.developer_workflow"]
    assert setuptools["packages"]["find"]["include"] == ["src*", "config*"]


def test_private_roots_are_created_with_verified_private_access(tmp_path: Path) -> None:
    from src.developer_workflow.private_paths import prepare_private_roots

    roots = tuple(tmp_path / name for name in ("runs", "mirrors", "worktrees"))
    assert prepare_private_roots(roots) == tuple(path.resolve() for path in roots)
    assert all(path.is_dir() for path in roots)
    if os.name != "nt":
        assert all(path.stat().st_mode & 0o077 == 0 for path in roots)


def test_private_roots_reject_existing_open_directory(tmp_path: Path) -> None:
    from src.developer_workflow.private_paths import PrivatePathError, prepare_private_roots

    roots = tuple(tmp_path / name for name in ("runs", "mirrors", "worktrees"))
    roots[0].mkdir(mode=0o777)
    if os.name != "nt":
        roots[0].chmod(0o755)
    with pytest.raises(PrivatePathError, match="private workflow root is unsafe"):
        prepare_private_roots(roots)
    assert not roots[1].exists() and not roots[2].exists()


def test_private_root_preflight_does_not_create_earlier_root_before_late_rejection(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.private_paths import PrivatePathError, prepare_private_roots

    roots = tuple(tmp_path / name for name in ("runs", "mirrors", "worktrees"))
    roots[1].mkdir(mode=0o777)
    if os.name != "nt":
        roots[1].chmod(0o755)
    with pytest.raises(PrivatePathError, match="private workflow root is unsafe"):
        prepare_private_roots(roots)
    assert not roots[0].exists() and not roots[2].exists()


def test_private_roots_reject_nesting_before_creation(tmp_path: Path) -> None:
    from src.developer_workflow.private_paths import PrivatePathError, prepare_private_roots

    roots = (tmp_path / "root", tmp_path / "root" / "mirror", tmp_path / "worktree")
    with pytest.raises(PrivatePathError, match="must not overlap"):
        prepare_private_roots(roots)
    assert not any(path.exists() for path in roots)


def test_private_roots_reject_symlink_before_other_root_creation(tmp_path: Path) -> None:
    from src.developer_workflow.private_paths import PrivatePathError, prepare_private_roots

    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    roots = (linked, tmp_path / "mirror", tmp_path / "worktree")
    with pytest.raises(PrivatePathError, match="private workflow root is unsafe"):
        prepare_private_roots(roots)
    assert not roots[1].exists() and not roots[2].exists()


def test_private_roots_reject_symlinked_parent_without_creating_child(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.private_paths import PrivatePathError, prepare_private_roots

    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    roots = (linked / "runs", tmp_path / "mirror", tmp_path / "worktree")
    with pytest.raises(PrivatePathError, match="private workflow root is unsafe"):
        prepare_private_roots(roots)
    assert not (target / "runs").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL protection flag")
def test_existing_windows_root_requires_protected_non_inheriting_dacl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import private_paths

    roots = tuple(tmp_path / name for name in ("runs", "mirrors", "worktrees"))
    for root in roots:
        root.mkdir()
    monkeypatch.setattr(private_paths, "_current_user_sid", lambda: "S-1-current")
    monkeypatch.setattr(
        private_paths,
        "_windows_descriptor",
        lambda path: (
            "S-1-current",
            (("S-1-current", 0x1F01FF, 0x03, 0),),
            False,
        ),
    )
    with pytest.raises(private_paths.PrivatePathError, match="private workflow root is unsafe"):
        private_paths.prepare_private_roots(roots)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL ACE contract")
def test_windows_dacl_rejects_inherited_or_underprivileged_trusted_ace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import private_paths

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        private_paths,
        "_windows_descriptor",
        lambda path: (
            "S-1-current",
            (("S-1-current", 0x00000001, 0x13, 0),),
            True,
        ),
    )
    with pytest.raises(private_paths.PrivatePathError, match="private workflow root is unsafe"):
        private_paths._verify_windows(root, "S-1-current")


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL ACE contract")
def test_windows_dacl_accepts_protected_full_control_trusted_aces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow import private_paths

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        private_paths,
        "_windows_descriptor",
        lambda path: (
            "S-1-current",
            (
                ("S-1-current", 0x1F01FF, 0x03, 0),
                ("S-1-5-18", 0x1F01FF, 0x03, 0),
                ("S-1-5-32-544", 0x1F01FF, 0x03, 0),
            ),
            True,
        ),
    )
    private_paths._verify_windows(root, "S-1-current")


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL propagation flags")
@pytest.mark.parametrize("flags", [0x00, 0x01, 0x02])
def test_windows_dacl_requires_object_and_container_inheritance_for_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: int
) -> None:
    from src.developer_workflow import private_paths

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(
        private_paths,
        "_windows_descriptor",
        lambda path: (
            "S-1-current",
            (("S-1-current", 0x1F01FF, flags, 0),),
            True,
        ),
    )
    with pytest.raises(private_paths.PrivatePathError, match="private workflow root is unsafe"):
        private_paths._verify_windows(root, "S-1-current")
