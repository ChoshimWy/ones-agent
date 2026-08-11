# ONES Multi-Repository Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend one approval-gated ONES requirement or defect run so it can safely analyze and modify an ordered repository group, test the repositories together, and publish one commit and PR per changed repository with resumable partial success.

**Architecture:** Add a versioned repository-group contract while retaining legacy single-repository fields for persisted-run compatibility. A focused `RepositoryGroupWorkspace` composes the existing `WorktreeRepository` for preparation, snapshots and identity gates; both flows consume the same group helpers, approval rebuilds one package from all repository evidence, and the publisher records immutable per-repository intent/facts under one operation lock. Single mappings are normalized to one-member groups at the configuration/orchestrator boundary.

**Tech Stack:** Python 3.11, Pydantic v2, Git CLI, pytest, existing `FileRunStore`, `WorktreeRepository`, `CodexRunner`, sandboxed command runner, GitHub/GitLab PR adapters, ONES gateway.

---

## File map and responsibilities

- Create `src/developer_workflow/repository_group.py`: group topology, workspace preparation/recovery, per-repository snapshot and qualified-path validation.
- Create `src/developer_workflow/group_evidence.py`: deterministic per-repository command selection, aggregate snapshots, coverage and final-test validation shared by both flows and approval rebuild.
- Create `tests/test_developer_workflow_repository_group.py`: real local Git coverage for local sources, multiple mirrors/worktrees, topology and path isolation.
- Create `tests/test_developer_workflow_multi_repository.py`: requirement/defect group-flow and approval evidence tests.
- Create `tests/test_developer_workflow_multi_publisher.py`: commit/push/PR/comment ordering, partial success and concurrent recovery.
- Create `tests/test_developer_workflow_multi_e2e.py`: production assembly with local bare remotes and fake ONES/Codex/PR/comment boundaries.
- Modify `src/developer_workflow/contracts.py`: group configuration, repository evidence, aggregate approval and publication contracts.
- Modify `src/developer_workflow/config.py`: load, validate and normalize `repository_groups` and legacy `repositories`.
- Modify `src/developer_workflow/repository.py`: optional read-only `source_path` mirror input and group-safe branch naming helpers.
- Modify `src/developer_workflow/codex_runner.py` and `src/developer_workflow/schemas/workflow-result.schema.json`: repository-qualified change claims and group workspace execution.
- Modify `src/developer_workflow/requirement_flow.py` and `src/developer_workflow/defect_flow.py`: group preparation, group claims, per-repository tests and integration tests.
- Modify `src/developer_workflow/approval.py` and `src/developer_workflow/approval_rebuilder.py`: validate and rebuild one aggregate approval package.
- Modify `src/developer_workflow/publisher.py`, `src/developer_workflow/ones_comment.py` and `src/developer_workflow/pr_provider.py`: ordered per-repository publication and one summary comment.
- Modify `src/developer_workflow/state_store.py`: immutable group configuration and monotonic per-repository publication facts.
- Modify `src/developer_workflow/orchestrator.py` and `src/developer_workflow/cli.py`: group selection, confirmation and display.
- Modify `src/developer_workflow/__init__.py`: export new public contracts and services.
- Modify `docs/examples/ones-dev.config.json` and `docs/ones_dev_cli.md`: document group configuration, local read-only sources and recovery.

### Task 1: Add strict repository-group contracts and configuration normalization

**Files:**
- Modify: `src/developer_workflow/contracts.py`
- Modify: `src/developer_workflow/config.py`
- Modify: `src/developer_workflow/__init__.py`
- Test: `tests/test_developer_workflow_contracts.py`
- Test: `tests/test_developer_workflow_config.py`

- [ ] **Step 1: Write failing contract tests**

Add tests that construct a two-repository group, reject duplicate keys, multiple primaries, missing dependencies, cycles, unsafe `source_path`, conflicting legacy/group keys, and verify deterministic topology and JSON round-trip:

```python
def test_repository_group_has_stable_topological_order(tmp_path: Path) -> None:
    sdk = _mapping(
        key="shared-sdk", repo_name="shared-sdk", role="dependency",
        source_path=tmp_path / "shared-sdk", depends_on=(),
    )
    app = _mapping(
        key="desktop-app", repo_name="desktop-app", role="primary",
        source_path=tmp_path / "desktop-app", depends_on=("shared-sdk",),
    )
    group = RepositoryGroupMapping(
        key="desktop-suite", project_id="project", iteration_id="iteration",
        primary_repository="desktop-app", repositories=(sdk, app),
        integration_test_commands=("uv run pytest tests/integration",),
    )
    assert group.topological_keys() == ("shared-sdk", "desktop-app")
    assert RepositoryGroupMapping.model_validate_json(group.model_dump_json()) == group


def test_repository_group_rejects_dependency_cycle() -> None:
    with pytest.raises(ValidationError, match="acyclic"):
        RepositoryGroupMapping(
            key="cycle", project_id="project", iteration_id="iteration",
            primary_repository="a",
            repositories=(
                _mapping(key="a", role="primary", depends_on=("b",)),
                _mapping(key="b", role="dependency", depends_on=("a",)),
            ),
        )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_developer_workflow_contracts.py tests/test_developer_workflow_config.py -q`

Expected: collection or assertion failure because `RepositoryRole`, `RepositoryGroupMapping`, `RepositoryRunEvidence` and group normalization do not exist.

- [ ] **Step 3: Implement strict group contracts**

Add exact models with strict validation and no second source of topology truth:

```python
class RepositoryRole(str, Enum):
    PRIMARY = "primary"
    DEPENDENCY = "dependency"


class RepositoryMapping(WorkflowModel):
    key: str
    project_id: str
    iteration_id: str
    repo_url: str
    repo_name: str
    base_branch: str = "main"
    source_path: Path | None = None
    role: RepositoryRole = RepositoryRole.PRIMARY
    depends_on: tuple[str, ...] = Field(default_factory=tuple)
    test_commands: tuple[str, ...] = Field(default_factory=tuple)
    lint_commands: tuple[str, ...] = Field(default_factory=tuple)
    build_commands: tuple[str, ...] = Field(default_factory=tuple)
    allowed_paths: tuple[str, ...] = Field(default_factory=tuple)


class RepositoryGroupMapping(WorkflowModel):
    key: str
    project_id: str
    iteration_id: str
    primary_repository: str
    repositories: tuple[RepositoryMapping, ...] = Field(min_length=1)
    integration_test_commands: tuple[str, ...] = Field(default_factory=tuple)

    def topological_keys(self) -> tuple[str, ...]:
        order = {item.key: index for index, item in enumerate(self.repositories)}
        indegree = {item.key: 0 for item in self.repositories}
        children = {item.key: [] for item in self.repositories}
        for item in self.repositories:
            for dependency in item.depends_on:
                indegree[item.key] += 1
                children[dependency].append(item.key)
        ready = sorted((key for key, count in indegree.items() if count == 0), key=order.get)
        result: list[str] = []
        while ready:
            key = ready.pop(0)
            result.append(key)
            for child in sorted(children[key], key=order.get):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
                    ready.sort(key=order.get)
        if len(result) != len(self.repositories):
            raise ValueError("repository dependencies must be acyclic")
        return tuple(result)
```

Add `RepositoryRunEvidence` with `repository_key`, `mapping`, `prepared_worktree`, `tested_snapshot`, `test_results`, and `changed_files`; add `RepositoryChangeClaim(repository_key, path)`. Lock the new `WorkflowRun` field names here so every later task uses the same interface:

```python
class WorkflowRun(WorkflowModel):
    repository_model_version: StrictInt = 1
    repository_group: RepositoryGroupMapping | None = None
    repository_evidence: tuple[RepositoryRunEvidence, ...] = Field(default_factory=tuple)
    integration_test_results: tuple[CommandResult, ...] = Field(default_factory=tuple)
    group_publication: MultiRepositoryPublicationResult | None = None
```

Define `MultiRepositoryPublicationResult` initially as an empty-intent compatibility contract, then complete its facts in Task 6. Preserve all existing singular fields for decoding historical runs, but reject a run that mixes non-equivalent singular and group facts. New runs use model version 2; decoded historical runs retain version 1 and are normalized in memory without rewriting their JSON.

- [ ] **Step 4: Normalize legacy configuration**

Change `DeveloperWorkflowConfig` to accept both sources and expose only groups to new orchestration:

```python
class DeveloperWorkflowConfig(WorkflowModel):
    run_root: Path
    worktree_root: Path
    mirror_root: Path
    sandbox_permission_profile: str
    max_codex_attempts: int = Field(ge=1, le=10)
    repositories: tuple[RepositoryMapping, ...] = Field(default_factory=tuple)
    repository_groups: tuple[RepositoryGroupMapping, ...] = Field(default_factory=tuple)
    publishing: PublishingConfig

    def normalized_groups(self) -> tuple[RepositoryGroupMapping, ...]:
        legacy = tuple(
            RepositoryGroupMapping(
                key=item.key, project_id=item.project_id,
                iteration_id=item.iteration_id, primary_repository=item.key,
                repositories=(item.validated_update(role=RepositoryRole.PRIMARY, depends_on=()),),
            )
            for item in self.repositories
        )
        return (*legacy, *self.repository_groups)
```

Validation must reject duplicate group keys, duplicate `(project_id, iteration_id)` selectors, and a key reused by legacy and group configuration. Replace `resolve_mapping_key` with `resolve_group_key`, retaining the former as a one-member compatibility wrapper.

- [ ] **Step 5: Run focused tests and commit**

Run: `uv run pytest tests/test_developer_workflow_contracts.py tests/test_developer_workflow_config.py -q`

Expected: PASS.

Commit:

```powershell
git add src/developer_workflow/contracts.py src/developer_workflow/config.py src/developer_workflow/__init__.py tests/test_developer_workflow_contracts.py tests/test_developer_workflow_config.py
git commit -m "feat(workflow): add repository group contracts"
```

### Task 2: Build the isolated multi-repository workspace service

**Files:**
- Create: `src/developer_workflow/repository_group.py`
- Modify: `src/developer_workflow/repository.py`
- Create: `tests/test_developer_workflow_repository_group.py`
- Modify: `tests/test_developer_workflow_repository.py`

- [ ] **Step 1: Write real-Git RED tests**

Create local source repositories and bare remotes. Assert topology order, fixed sibling directory names, no mutation of source HEAD/index/status, recovery after prepare-before-save crash, qualified path containment, and rejection of source/remote identity mismatch:

```python
def test_group_prepare_uses_local_sources_without_mutating_them(git_group) -> None:
    before = {key: git_group.source_facts(key) for key in ("shared-sdk", "desktop-app")}
    prepared = git_group.service.prepare_group("run-1", git_group.mapping, "codex/bug-1")
    assert tuple(item.repository_key for item in prepared) == ("shared-sdk", "desktop-app")
    assert prepared[0].prepared.path.parent == prepared[1].prepared.path.parent
    assert prepared[0].prepared.path.name == "shared-sdk"
    assert prepared[1].prepared.path.name == "desktop-app"
    assert {key: git_group.source_facts(key) for key in before} == before


def test_qualified_path_cannot_cross_repository(group_workspace) -> None:
    with pytest.raises(RepositoryGroupError, match="unsafe repository-qualified path"):
        group_workspace.resolve_claim(RepositoryChangeClaim(
            repository_key="desktop-app", path="../shared-sdk/src/x.py"
        ))
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_developer_workflow_repository_group.py -q`

Expected: FAIL because `RepositoryGroupWorkspace` is missing.

- [ ] **Step 3: Extend mirror preparation with a read-only source**

Update `WorktreeRepository._ensure_mirror()` so the clone source is `mapping.source_path` when present, while `origin` is reset and verified against `mapping.repo_url`. Before and after clone/fetch, record the source repository HEAD, common-dir identity, index identity and porcelain status; reject any change. Never run a Git command with `cwd=source_path` that can write.

Add an explicit branch helper:

```python
def repository_branch(run_id: str, work_item_id: str, repository_key: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]", "-", work_item_id).strip(".-")
    return validate_git_ref_name(f"codex/{token}-{repository_key}-{run_id[:8]}")
```

- [ ] **Step 4: Implement `RepositoryGroupWorkspace`**

```python
@dataclass(frozen=True, slots=True)
class PreparedRepository:
    repository_key: str
    mapping: RepositoryMapping
    prepared: PreparedWorktree


@dataclass(slots=True)
class RepositoryGroupWorkspace:
    repository: WorktreeRepository

    def prepare_group(
        self, run_id: str, group: RepositoryGroupMapping, work_item_id: str
    ) -> tuple[PreparedRepository, ...]:
        prepared: list[PreparedRepository] = []
        by_key = {item.key: item for item in group.repositories}
        for key in group.topological_keys():
            mapping = by_key[key]
            branch = repository_branch(run_id, work_item_id, key)
            worktree = self.repository.recover(run_id, mapping, branch)
            if worktree is None:
                worktree = self.repository.prepare(run_id, mapping, branch)
            prepared.append(PreparedRepository(key, mapping, worktree))
        self._assert_sibling_layout(tuple(prepared))
        return tuple(prepared)

    def snapshots(
        self, prepared: tuple[PreparedRepository, ...]
    ) -> dict[str, RepositorySnapshot]:
        return {
            item.repository_key: self.repository.snapshot(item.prepared, item.mapping)
            for item in prepared
        }
```

Implement `resolve_claim()` by exact repository-key lookup followed by existing POSIX path, containment, symlink/reparse and `allowed_paths` checks. Do not join an unvalidated key or path into a filesystem path.

- [ ] **Step 5: Run repository tests and commit**

Run: `uv run pytest tests/test_developer_workflow_repository_group.py tests/test_developer_workflow_repository.py -q`

Expected: PASS, with only existing platform-gated skips.

Commit:

```powershell
git add src/developer_workflow/repository.py src/developer_workflow/repository_group.py tests/test_developer_workflow_repository.py tests/test_developer_workflow_repository_group.py
git commit -m "feat(workflow): prepare isolated repository groups"
```

### Task 3: Add repository-qualified Codex output and group sandbox execution

**Files:**
- Modify: `src/developer_workflow/contracts.py`
- Modify: `src/developer_workflow/codex_runner.py`
- Modify: `src/developer_workflow/schemas/workflow-result.schema.json`
- Modify: `tests/test_developer_workflow_codex_runner.py`
- Test: `tests/test_developer_workflow_multi_repository.py`

- [ ] **Step 1: Write failing schema and runner tests**

```python
def test_group_run_requires_exact_repository_qualified_claims(group_runner) -> None:
    result = group_runner.run_group(
        run_id="run-1", prompt="fix lifecycle bug",
        group=group_runner.group, prepared=group_runner.prepared,
    )
    assert result.repository_changes == (
        RepositoryChangeClaim(repository_key="shared-sdk", path="src/shortcut.py"),
        RepositoryChangeClaim(repository_key="desktop-app", path="src/window.py"),
    )


def test_group_run_rejects_claim_for_unconfigured_repository(group_runner) -> None:
    group_runner.executor.stdout = _group_payload("other", "src/x.py")
    with pytest.raises(CodexOutputError, match="repository change claims"):
        group_runner.run_group(
            run_id="run-1", prompt="fix", group=group_runner.group,
            prepared=group_runner.prepared,
        )
```

Also assert that a group invocation uses the common workspace cwd, retains the existing sandbox capability probe, and checks all repository identities before and after Codex.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_developer_workflow_codex_runner.py -q`

Expected: FAIL because the schema and runner only support `changed_files` in one repository.

- [ ] **Step 3: Extend the strict output schema**

Add required `repository_changes` entries with `additionalProperties: false`, safe repository keys and POSIX paths. Retain `changed_files` for a single-repository call, but enforce exactly one mode in `CodexResult`:

```python
class RepositoryChangeClaim(WorkflowModel):
    repository_key: str
    path: str

    @field_validator("repository_key")
    @classmethod
    def safe_key(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9._-]+", value) is None:
            raise ValueError("repository key must be a safe ASCII segment")
        return value

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        RepositorySnapshot._validate_repository_path(value)
        return value
```

- [ ] **Step 4: Implement `CodexRunner.run_group()`**

The method must receive the frozen group and prepared contexts, invoke Codex once from the common parent, take pre/post snapshots of every repository, compare the aggregate actual claims with `repository_changes`, scan changed content in every repository for secrets, and execute a final identity/HEAD guard for every worktree. Sort comparisons by `(topology_index, path)` but preserve model output order in persisted evidence.

- [ ] **Step 5: Run runner regression and commit**

Run: `uv run pytest tests/test_developer_workflow_codex_runner.py tests/test_developer_workflow_multi_repository.py -q`

Expected: PASS.

Commit:

```powershell
git add src/developer_workflow/contracts.py src/developer_workflow/codex_runner.py src/developer_workflow/schemas/workflow-result.schema.json tests/test_developer_workflow_codex_runner.py tests/test_developer_workflow_multi_repository.py
git commit -m "feat(workflow): validate multi-repository Codex changes"
```

### Task 4: Execute requirement and defect flows across the repository group

**Files:**
- Create: `src/developer_workflow/group_evidence.py`
- Modify: `src/developer_workflow/requirement_flow.py`
- Modify: `src/developer_workflow/defect_flow.py`
- Modify: `src/developer_workflow/test_evidence.py`
- Test: `tests/test_developer_workflow_multi_repository.py`
- Modify: `tests/test_developer_workflow_requirement.py`
- Modify: `tests/test_developer_workflow_defect.py`

- [ ] **Step 1: Write RED workflow tests**

Cover: all repositories read-only; primary-only change; dependency-only change; two-repository change; unsafe cross-repository claim; per-repository test failure; integration-test failure; post-test mutation in either repository; and defect reproduction test residing in one configured repository.

```python
def test_requirement_tests_repositories_then_group_integration(multi_requirement) -> None:
    completed = multi_requirement.execute_to_approval()
    assert multi_requirement.commands == [
        ("shared-sdk", ("uv", "run", "pytest")),
        ("desktop-app", ("uv", "run", "pytest")),
        ("desktop-app", ("uv", "run", "pytest", "tests/integration")),
    ]
    assert completed.state is WorkflowState.WAITING_APPROVAL
    assert tuple(item.repository_key for item in completed.repository_evidence) == (
        "shared-sdk", "desktop-app",
    )


def test_snapshot_change_in_read_only_dependency_blocks_approval(multi_requirement) -> None:
    multi_requirement.mutate_after_tests("shared-sdk", "README.md")
    blocked = multi_requirement.execute_to_approval()
    assert blocked.state is WorkflowState.BLOCKED
    assert blocked.approval is None
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_developer_workflow_multi_repository.py -q`

Expected: FAIL because both flows still require singular repository/prepared/tested fields.

- [ ] **Step 3: Implement shared group evidence helpers**

```python
def aggregate_claims(result: CodexResult) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for claim in result.repository_changes:
        grouped.setdefault(claim.repository_key, []).append(claim.path)
    return {key: tuple(paths) for key, paths in grouped.items()}


def assert_group_snapshots_equal(
    expected: tuple[RepositoryRunEvidence, ...],
    actual: dict[str, RepositorySnapshot],
) -> None:
    expected_by_key = {item.repository_key: item.tested_snapshot for item in expected}
    if expected_by_key != actual:
        raise GroupEvidenceError("repository group differs from tested evidence")
```

Add command selection that returns `(repository_key, CommandResult)` in topology order and group integration results after repository results. Reject duplicate canonical argv across a repository's lint/build/test sets and between group integration commands where ambiguity would break approval reconstruction.

- [ ] **Step 4: Integrate group preparation and implementation into both flows**

At `PREPARING_REPO`, persist the full prepared group in one CAS save before entering `IMPLEMENTING`. On resume, recover every context and compare the frozen mapping, branch, base and HEAD.

At `IMPLEMENTING`, use `CodexRunner.run_group()` and validate each repository's claims independently. Requirement coverage files and defect root-cause/supporting paths must use `RepositoryChangeClaim`, not an unqualified string.

For defect reproduction, bind the focused test to a repository key and run it in that repository only. Preserve the existing pre-fix failure, reproduction hash, repair, revision and final-pass semantics.

- [ ] **Step 5: Implement tests and review gates**

Run each repository's configured lint/build/test in topology order, then group integration commands in the primary worktree. Persist a tested snapshot for every repository. Before/after testing analysis, AI review and approval packaging, re-snapshot every repository and compare the complete group.

- [ ] **Step 6: Run flow regressions and commit**

Run: `uv run pytest tests/test_developer_workflow_multi_repository.py tests/test_developer_workflow_requirement.py tests/test_developer_workflow_defect.py -q`

Expected: PASS with existing environment-gated skips only.

Commit:

```powershell
git add src/developer_workflow/group_evidence.py src/developer_workflow/requirement_flow.py src/developer_workflow/defect_flow.py src/developer_workflow/test_evidence.py tests/test_developer_workflow_multi_repository.py tests/test_developer_workflow_requirement.py tests/test_developer_workflow_defect.py
git commit -m "feat(workflow): run changes and tests across repository groups"
```

### Task 5: Bind one approval fingerprint to every repository

**Files:**
- Modify: `src/developer_workflow/contracts.py`
- Modify: `src/developer_workflow/approval.py`
- Modify: `src/developer_workflow/approval_rebuilder.py`
- Modify: `tests/test_developer_workflow_approval.py`
- Modify: `tests/test_developer_workflow_approval_rebuilder.py`
- Test: `tests/test_developer_workflow_multi_repository.py`

- [ ] **Step 1: Write approval drift RED tests**

Parameterize drift of repository base, HEAD, patch, test result, dependency order, commit message, PR title/body and integration test result. Every mutation must make rebuild or fingerprint verification fail before publication intent creation.

```python
@pytest.mark.parametrize("repository_key", ["shared-sdk", "desktop-app"])
def test_any_repository_diff_drift_invalidates_group_approval(
    approved_group_run, rebuilder, repository_key
) -> None:
    rebuilder.snapshots[repository_key] = _changed_snapshot(repository_key)
    with pytest.raises(ApprovalRebuildError, match="tested evidence"):
        rebuilder.rebuild(approved_group_run)
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_developer_workflow_approval.py tests/test_developer_workflow_approval_rebuilder.py -q`

Expected: FAIL because approval contains one repository only.

- [ ] **Step 3: Add aggregate approval models and strict validation**

```python
class RepositoryApprovalEvidence(WorkflowModel):
    repository_key: str
    mapping: RepositoryMapping
    base_commit: str
    head_commit: str
    diff_hash: str
    diff_summary: str
    branch: str
    changed_files: tuple[str, ...]
    tests: tuple[CommandResult, ...]
    commit_message: str = ""
    pr_title: str = ""
    pr_body: str = ""


class ApprovalPackage(WorkflowModel):
    repository_group: RepositoryGroupMapping | None = None
    repositories: tuple[RepositoryApprovalEvidence, ...] = Field(default_factory=tuple)
    integration_tests: tuple[CommandResult, ...] = Field(default_factory=tuple)
```

Require repository evidence order to exactly equal `group.topological_keys()`. Require publication text only for repositories with non-empty changed files. The existing singular fields remain valid only for historical one-repository packages and must be normalized before fingerprinting new approvals.

- [ ] **Step 4: Rebuild all live evidence**

Update `WorkflowApprovalRebuilder.rebuild()` to assert the remote base, HEAD and snapshot of every repository before and after snapshot collection. Select final per-repository tests and integration tests using `group_evidence.py`; rebuild repository-specific commit/PR text from the persisted human-approved package; then call `validate_for_approval()` once on the aggregate package.

- [ ] **Step 5: Run approval tests and commit**

Run: `uv run pytest tests/test_developer_workflow_approval.py tests/test_developer_workflow_approval_rebuilder.py tests/test_developer_workflow_multi_repository.py -q`

Expected: PASS.

Commit:

```powershell
git add src/developer_workflow/contracts.py src/developer_workflow/approval.py src/developer_workflow/approval_rebuilder.py tests/test_developer_workflow_approval.py tests/test_developer_workflow_approval_rebuilder.py tests/test_developer_workflow_multi_repository.py
git commit -m "feat(workflow): approve aggregate repository evidence"
```

### Task 6: Publish each changed repository with resumable partial success

**Files:**
- Modify: `src/developer_workflow/contracts.py`
- Modify: `src/developer_workflow/publisher.py`
- Modify: `src/developer_workflow/ones_comment.py`
- Modify: `src/developer_workflow/state_store.py`
- Create: `tests/test_developer_workflow_multi_publisher.py`
- Modify: `tests/test_developer_workflow_publisher.py`
- Modify: `tests/test_developer_workflow_state_store.py`

- [ ] **Step 1: Write publication RED tests**

Test that all local commit intents/commits complete before the first push, remote publication follows topology, unchanged repositories are skipped, a second-repository PR failure preserves the first PR and enters `PARTIAL_SUCCESS`, resume only continues unfinished facts, conflicts block, concurrent stores create each PR once, and the summary comment happens once after all PRs.

```python
def test_group_publication_resumes_only_unfinished_repository(group_publisher) -> None:
    group_publisher.pr.fail_once_for = "desktop-app"
    partial = group_publisher.publish(group_publisher.approved_run)
    assert partial.state is WorkflowState.PARTIAL_SUCCESS
    assert group_publisher.pr.created == ["shared-sdk"]

    completed = group_publisher.publish(partial)
    assert completed.state is WorkflowState.COMPLETED
    assert group_publisher.git.commit_calls == ["shared-sdk", "desktop-app"]
    assert group_publisher.git.push_calls == ["shared-sdk", "desktop-app"]
    assert group_publisher.pr.created == ["shared-sdk", "desktop-app"]
    assert group_publisher.comment.calls == 1
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_developer_workflow_multi_publisher.py -q`

Expected: FAIL because `PublicationResult` and `Publisher` are singular.

- [ ] **Step 3: Add immutable per-repository publication facts**

```python
class RepositoryPublicationResult(WorkflowModel):
    repository_key: str
    approved_fingerprint: str
    repo_url: str
    expected_parent: str
    expected_tree: str
    commit_message: str
    remote_branch: str
    commit_hash: str = ""
    push_completed_at: datetime | None = None
    pr_marker: str
    pr_base: str
    pr_head: str
    pr_title: str
    pr_body: str
    pr_url: str = ""
    error: str = ""


class MultiRepositoryPublicationResult(WorkflowModel):
    order: tuple[str, ...]
    repositories: tuple[RepositoryPublicationResult, ...]
    comment_marker: str
    comment_id: str = ""
    error: str = ""
```

State-store validation must enforce immutable order and intent, monotonic facts (`commit -> push -> PR`), no facts for unchanged repositories, and `COMPLETED` only when all changed repositories have PR URLs and the summary comment has an ID.

- [ ] **Step 4: Refactor publisher into prepare-all then publish-in-order**

Under the existing run-level OS publish lock:

1. Reload the authoritative run.
2. Rebuild and verify aggregate approval.
3. Persist every repository intent in one CAS save.
4. Prepare and persist every local commit before any push.
5. Iterate topology order and ensure push then PR for each repository.
6. On an error after any remote success, persist the repository-safe error and transition to `PARTIAL_SUCCESS`.
7. On resume, query persisted/factual commit, branch and PR markers before writing.
8. After all PRs, create one marker-based ONES summary comment and transition `COMPLETED`.

Do not overload `PARTIAL_SUCCESS` as comment-only; `Publisher.publish()` must resume repository facts and then comment facts from the same state.

- [ ] **Step 5: Format the aggregate ONES comment**

Use only persisted safe repository keys, commit hashes and validated HTTPS PR URLs:

```text
ONES AI 开发运行 <run-id> 已发布：
- shared-sdk: <commit> <pr-url>
- desktop-app: <commit> <pr-url>
```

Keep the stable marker outside user-controlled text and continue to prohibit ONES status mutation.

- [ ] **Step 6: Run publication/state tests and commit**

Run: `uv run pytest tests/test_developer_workflow_multi_publisher.py tests/test_developer_workflow_publisher.py tests/test_developer_workflow_state_store.py -q`

Expected: PASS.

Commit:

```powershell
git add src/developer_workflow/contracts.py src/developer_workflow/publisher.py src/developer_workflow/ones_comment.py src/developer_workflow/state_store.py tests/test_developer_workflow_multi_publisher.py tests/test_developer_workflow_publisher.py tests/test_developer_workflow_state_store.py
git commit -m "feat(workflow): publish repository groups incrementally"
```

### Task 7: Wire repository groups through orchestrator, CLI and production factory

**Files:**
- Modify: `src/developer_workflow/orchestrator.py`
- Modify: `src/developer_workflow/cli.py`
- Modify: `src/developer_workflow/__init__.py`
- Modify: `tests/test_developer_workflow_orchestrator.py`
- Modify: `tests/test_developer_workflow_cli.py`

- [ ] **Step 1: Write CLI/orchestrator RED tests**

Assert exact group confirmation, rejection of a group not shown in persisted candidates, deterministic display, one-member legacy behavior, multi-repository `show`, and `PARTIAL_SUCCESS` resume through publisher.

```python
def test_cli_confirms_one_group_and_displays_publish_order(cli) -> None:
    result = cli.run(
        "defects", "start", "--project", "p", "--iteration", "i",
        "--assignee", "a", "--select", "DEF-1", "--mapping", "desktop-suite",
    )
    assert result.exit_code == 0
    assert "主仓库: desktop-app" in result.stdout
    assert "1. shared-sdk" in result.stdout
    assert "2. desktop-app" in result.stdout
    assert "本地源码只读" in result.stdout
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_developer_workflow_orchestrator.py tests/test_developer_workflow_cli.py -q`

Expected: FAIL because selection and display are singular.

- [ ] **Step 3: Change orchestration to persist candidate groups**

Replace mapping candidates for new runs with frozen `RepositoryGroupMapping` candidates. `confirm_repository()` must compare the selected group with both the persisted candidate and current config before saving. Run the full create/reload/execute sequence under the existing run operation lock. `_flow_for()` continues to select requirement vs defect, not single vs group; flows inspect the normalized group.

- [ ] **Step 4: Update CLI display and production assembly**

`--mapping` remains one key. Display main repository, topology, role, base branch, local source, remote URL and per-repository progress. Do not display configured command strings that fail existing credential-argv checks. Build one `RepositoryGroupWorkspace` and inject it into both flows, approval rebuilder and publisher.

- [ ] **Step 5: Run CLI/orchestrator regressions and commit**

Run: `uv run pytest tests/test_developer_workflow_orchestrator.py tests/test_developer_workflow_cli.py -q`

Expected: PASS.

Run: `uv run ones-dev --help`

Expected: exit 0 and the existing seven commands remain available.

Commit:

```powershell
git add src/developer_workflow/orchestrator.py src/developer_workflow/cli.py src/developer_workflow/__init__.py tests/test_developer_workflow_orchestrator.py tests/test_developer_workflow_cli.py
git commit -m "feat(workflow): expose repository groups in the CLI"
```

### Task 8: Document configuration and add production-shaped multi-repository E2E

**Files:**
- Create: `tests/test_developer_workflow_multi_e2e.py`
- Modify: `tests/test_developer_workflow_security.py`
- Modify: `docs/examples/ones-dev.config.json`
- Modify: `docs/ones_dev_cli.md`

- [ ] **Step 1: Write the E2E test before documentation changes**

Build two local source repositories and two bare remotes. Use production `FileRunStore`, `RepositoryGroupWorkspace`, requirement/defect flow, approval rebuilder, publisher and orchestrator; fake only ONES/Codex/PR/comment. Use real sandboxed configured commands. Assert source repositories unchanged, both isolated worktrees modified, integration tests run last, one approval, two local commits, topology-ordered pushes, two PRs, one ONES comment, zero status writes and final `COMPLETED`.

Add a second test that fails PR creation for the primary repository, verifies dependency PR preservation and `PARTIAL_SUCCESS`, reconstructs services from disk, resumes, and reaches `COMPLETED` without duplicated remote facts.

- [ ] **Step 2: Verify E2E RED**

Run: `uv run pytest tests/test_developer_workflow_multi_e2e.py -q`

Expected: FAIL at the first missing or inconsistent production assembly boundary.

- [ ] **Step 3: Update example configuration**

Add a secret-free `repository_groups` example containing `shared-sdk` and `desktop-app`, with placeholder absolute `source_path` values, valid remote HTTPS URLs, dependency order and integration commands. Retain one legacy mapping example and explain that duplicate selector/key pairs are rejected.

- [ ] **Step 4: Update Chinese CLI operations documentation**

Document:

- `source_path` is optional and strictly read-only.
- `repo_url` remains mandatory and authoritative for remote base/push/PR.
- `--mapping` selects the group.
- confirmation output and stable sibling layout.
- one approval across all repositories.
- topology publication and `PARTIAL_SUCCESS` recovery.
- `show`, `approve`, `resume` examples.
- no automatic PR rollback and no ONES status write.

- [ ] **Step 5: Run E2E/security tests and commit**

Run: `uv run pytest tests/test_developer_workflow_multi_e2e.py tests/test_developer_workflow_security.py -q`

Expected: PASS with environment-gated sandbox tests skipped only when their documented capability is absent.

Commit:

```powershell
git add tests/test_developer_workflow_multi_e2e.py tests/test_developer_workflow_security.py docs/examples/ones-dev.config.json docs/ones_dev_cli.md
git commit -m "test(workflow): cover multi-repository delivery end to end"
```

### Task 9: Run full compatibility, packaging and safety verification

**Files:**
- Modify only files required to fix failures caused by Tasks 1-8; do not broaden scope.

- [ ] **Step 1: Run fast developer-workflow tests**

Run:

```powershell
uv run pytest tests/test_developer_workflow_config.py tests/test_developer_workflow_contracts.py tests/test_developer_workflow_state_store.py tests/test_developer_workflow_codex_runner.py tests/test_developer_workflow_approval.py tests/test_developer_workflow_approval_rebuilder.py tests/test_developer_workflow_requirement.py tests/test_developer_workflow_defect.py tests/test_developer_workflow_orchestrator.py tests/test_developer_workflow_cli.py tests/test_developer_workflow_publisher.py tests/test_developer_workflow_multi_repository.py tests/test_developer_workflow_multi_publisher.py -q
```

Expected: all pass; only documented environment-gated skips.

- [ ] **Step 2: Run real Git and E2E tests separately with a writable basetemp**

Run:

```powershell
uv run pytest tests/test_developer_workflow_repository.py tests/test_developer_workflow_repository_group.py tests/test_developer_workflow_multi_e2e.py --basetemp .tmp/multi-repository-final -q
```

Expected: all pass; only platform-specific symlink/sandbox skips.

- [ ] **Step 3: Run ONES and legacy regression tests**

Run:

```powershell
uv run pytest tests/test_ones.py tests/test_ones_gateway.py tests/test_phase2.py tests/test_developer_workflow_e2e.py tests/test_developer_workflow_security.py -q
```

Expected: all pass; LAN tests remain opt-in and no real external writes occur.

- [ ] **Step 4: Verify package, schema and source cleanliness**

Run:

```powershell
uv lock --check --offline
uv run ones-dev --help
uv run python -m compileall -q src config main.py server.py
git diff --check
git status --short
```

Expected: lock check succeeds, CLI lists seven commands, compileall and diff check exit 0, and status contains only intentional task files plus pre-existing unrelated untracked files.

- [ ] **Step 5: Review security invariants explicitly**

Confirm from tests and diff that:

- no command writes to `source_path`;
- no unconfigured repository can enter a run;
- every qualified path passes exact key and repository-relative validation;
- every repository is snapshot/HEAD checked after tests and review;
- one approval fingerprint covers all repositories;
- all commits exist before the first push;
- remote writes are topology ordered and idempotently recovered;
- one summary comment is the only ONES business write;
- single-repository persisted runs still decode and resume.

- [ ] **Step 6: Commit final compatibility fixes**

If Tasks 1-8 already pass without compatibility edits, skip this commit. Otherwise stage only the verified fixes and their regression tests:

```powershell
git add src/developer_workflow tests docs/examples/ones-dev.config.json docs/ones_dev_cli.md
git commit -m "fix(workflow): preserve single-repository compatibility"
```

## Self-review checklist

- Spec sections 1-4 are covered by Tasks 1-2 and 7.
- Runtime model, workspace isolation and qualified paths are covered by Tasks 1-4.
- Per-repository and integration tests are covered by Task 4.
- Aggregate approval and live rebuild are covered by Task 5.
- Topology publication, partial success and recovery are covered by Task 6.
- CLI, compatibility and local-source guidance are covered by Tasks 7-8.
- Full safety, packaging and legacy verification are covered by Task 9.
- The plan contains no deferred implementation placeholders; every task has an explicit RED, implementation boundary, GREEN command and commit.
