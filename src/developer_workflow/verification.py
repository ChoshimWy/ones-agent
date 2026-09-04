"""Dynamic verification planning and explicit, version-bound node execution."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
import stat
import subprocess
import sys

from .contracts import WorkflowRun, WorkflowType
from .verification_models import MISSING_BASELINE_DESCRIPTION
from .verification_models import VerificationNeed, VerificationNode, VerificationRecord, VerificationTask
from .verification_worker import digest, safe_path, MAX_BYTES, execution_environment


VERIFICATION_WAITING = "waiting for verification environment"
VERIFICATION_READY = "verification execution requires confirmation"
VERIFICATION_FAILED = "external verification did not pass"
VERIFICATION_RUNNING = "external verification is running"
VERIFICATION_REASONS = {VERIFICATION_WAITING, VERIFICATION_READY, VERIFICATION_FAILED, VERIFICATION_RUNNING,
                        "AI review requires external validation"}

_SAFE_ERRORS = {
    "bundle contains a protected file": "验证包包含受保护的环境/凭据文件；未发送到节点。",
    "bundle contains a private key": "验证包包含私钥；未发送到节点。",
    "bundle contains a link": "验证包包含符号链接或目录连接；未发送到节点。",
    "verification bundle exceeds limit": "验证包超过 64 MiB 或 20,000 个文件的限制。",
    "verification checkout changed since tests": "源码已在测试后改变，请重新测试并审查。",
    "verification node platform mismatch": "节点实际操作系统不匹配验证需求。",
    "verification node architecture mismatch": "节点实际架构不匹配验证需求。",
    "verification response evidence mismatch": "节点响应与输入包/源码快照不一致，不能接受结果。",
    "node platform does not match the requested verification": "节点操作系统或架构不匹配，未执行脚本。",
    "missing_runtime": "节点缺少 Python、验证脚本或配置的测试解释器。",
    "node_permission": "节点文件或进程权限不足。",
    "host_key": "SSH 主机指纹校验失败，请先人工核验 known_hosts。",
    "ssh_auth": "SSH 密钥认证失败，请检查专用测试账号配置。",
    "ssh_connect": "SSH 节点不可达，请检查地址、端口和网络。",
}


def failure_message(error: Exception) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return "节点连接或验证执行超时；未记录通过，请检查节点日志。"
    return _SAFE_ERRORS.get(str(error), "节点执行失败，未记录通过；请检查连接、测试解释器和节点日志。")


def public_text(value: str, maximum: int = 16000) -> str:
    """Bounded plain text for logs/UI, with common credential forms removed."""
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[redacted private key]", value, flags=re.DOTALL)
    value = re.sub(r"(?i)(token|password|secret|authorization|api[_-]?key)([\s\"']*[:=][\s\"']*)[^\s,;\"']+", r"\1=[redacted]", value)
    value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [redacted]", value)
    return "".join(c if c == "\n" or unicodedata.category(c) not in {"Cc", "Cf", "Cs", "Zl", "Zp"} else " " for c in value)[:maximum]


def snapshot_digest(run: WorkflowRun) -> str:
    if run.repository_group is not None:
        snapshots = {item.repository_key: item.tested_snapshot.model_dump(mode="json")
                     for item in run.repository_evidence if item.tested_snapshot is not None}
        if set(snapshots) != {item.key for item in run.repository_group.repositories}:
            raise ValueError("missing tested repository snapshot")
    else:
        if run.tested_snapshot is None or run.repository is None:
            raise ValueError("missing tested repository snapshot")
        snapshots = {run.repository.key: run.tested_snapshot.model_dump(mode="json")}
    return digest(snapshots)


def requirements(run: WorkflowRun) -> tuple[VerificationNeed, ...]:
    if run.review is None:
        return ()
    needs = list(run.review.verification_needs)
    if run.type is WorkflowType.DEFECT and not run.verification_only and not run.pre_fix_test_results:
        needs.append(VerificationNeed(description=MISSING_BASELINE_DESCRIPTION,
            acceptance="优先在隔离的修复前版本运行同一复现用例，记录提交 SHA、失败输出和修复后结果；无法补验时由 PR 审核人明确评估证据缺口，不得宣称已证明修复有效。"))
    described = {item.description for item in needs}
    # Legacy free text is never silently dropped, guessed into a platform, or waived.
    needs.extend(VerificationNeed(description=text) for text in run.review.review_external_validation
                 if text not in described)
    # A re-review must not make an outstanding check disappear merely by omitting it.
    # Explicit replan clears the old plan; ordinary repair/review retains its obligations.
    described = {item.description for item in needs}
    needs.extend(task.need for task in run.verification_plan if task.need.description not in described)
    return tuple({digest(need.model_dump(mode="json")): need for need in needs}.values())


def plan(run: WorkflowRun, nodes: tuple[VerificationNode, ...]) -> tuple[VerificationTask, ...]:
    needs = requirements(run)
    if not needs:
        return ()
    snapshot = snapshot_digest(run)
    tasks = []
    repository_keys = {item.repository_key for item in run.repository_evidence} if run.repository_group else {run.repository.key}
    for need in needs:
        key = digest(need.model_dump(mode="json"))[:24]
        matches = [(node, recipe) for node in nodes if node.enabled
                   for recipe in node.recipes if recipe.repository_key in repository_keys
                   and need.capabilities and need.acceptance.strip()
                   and set(need.capabilities) <= set(node.capabilities)
                   and set(need.capabilities) <= set(recipe.capabilities)]
        node, recipe = matches[0] if matches else (None, None)
        recipe_hash = digest({"node": node.model_dump(mode="json"), "recipe": recipe.model_dump(mode="json")}) if node else ""
        status = "ready" if matches else "waiting_environment" if need.capabilities else "manual"
        record = next((r for r in reversed(run.verification_records)
                       if r.task_key == key and r.snapshot_digest == snapshot
                       and ((r.node_key == "manual" and r.actor.strip()) or
                            (node and r.node_key == node.key and r.recipe_digest == recipe_hash))), None)
        if record:
            status = record.status
        if need.description == MISSING_BASELINE_DESCRIPTION and not run.pre_fix_test_results and status == "passed":
            # A manual attestation cannot manufacture a historical test execution.
            status = "manual"
        tasks.append(VerificationTask(key=key, need=need, snapshot_digest=snapshot, status=status,
            node_key=node.key if node else "", recipe_key=recipe.key if recipe else "", recipe_digest=recipe_hash))
    return tuple(tasks)


def pending_reason(tasks: tuple[VerificationTask, ...]) -> str:
    if all(item.status == "passed" for item in tasks):
        return ""
    if any(item.status in {"failed", "error"} for item in tasks):
        return VERIFICATION_FAILED
    return VERIFICATION_READY if any(item.status == "ready" for item in tasks) else VERIFICATION_WAITING


def records_for_approval(run: WorkflowRun) -> tuple[VerificationRecord, ...]:
    return tuple(next(r for r in reversed(run.verification_records)
                      if r.task_key == task.key and r.snapshot_digest == task.snapshot_digest and r.status == "passed"
                      and (r.node_key == "manual" or (r.node_key == task.node_key and r.recipe_digest == task.recipe_digest)))
                 for task in run.verification_plan if task.status == "passed")


def export_bundle(run: WorkflowRun) -> tuple[dict, str]:
    prepared = [(item.repository_key, item.prepared_worktree.path) for item in run.repository_evidence]
    if run.repository_group is None:
        prepared = [(run.repository.key, run.worktree_path)]
    files, size = {}, 0
    for key, path in prepared:
        safe_path(key)
        root = Path(path).resolve(strict=True)
        output = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, check=True, timeout=30).stdout
        stages = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=root, capture_output=True,
                                check=True, timeout=30).stdout.decode("utf-8")
        executable_paths = {row.split("\t", 1)[1] for row in stages.split("\x00")
                            if row.startswith("100755 ") and "\t" in row}
        for name in dict.fromkeys(output.decode("utf-8").split("\x00")):
            if not name:
                continue
            relative = safe_path(name)
            # Secrets and local environment files are not test artifacts.
            if any(part.casefold() in {".ssh", ".env", ".aws", ".codex"}
                   or part.casefold().startswith(".env.") or part.casefold().endswith((".pem", ".key")) for part in relative.parts):
                raise ValueError("bundle contains a protected file")
            target = root.joinpath(*relative.parts)
            ancestors = [target, *(parent for parent in target.parents if parent.is_relative_to(root))]
            for parent in ancestors:
                try:
                    info = parent.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("bundle contains a link")
            if not target.exists():  # Tracked deletion is represented by absence.
                continue
            if not target.is_file() or not target.resolve().is_relative_to(root):
                raise ValueError("bundle contains a non-file")
            if target.stat().st_size + size > MAX_BYTES or len(files) >= 20000:
                raise ValueError("verification bundle exceeds limit")
            raw = target.read_bytes()
            if b"-----BEGIN " in raw and b"PRIVATE KEY-----" in raw:
                raise ValueError("bundle contains a private key")
            size += len(raw)
            files[f"{key}/{name}"] = {"sha256": hashlib.sha256(raw).hexdigest(),
                "data": base64.b64encode(raw).decode("ascii"),
                "executable": name in executable_paths or bool(target.stat().st_mode & 0o111)}
    return files, digest({key: {"sha256": value["sha256"], "executable": value["executable"]}
                          for key, value in files.items()})


def worker_command(node: VerificationNode) -> list[str]:
    if node.transport == "local":
        return [sys.executable, str(Path(__file__).with_name("verification_worker.py"))]
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
            "--", node.ssh_alias, " ".join(node.worker_argv)]


def assert_current(run: WorkflowRun, repository: object) -> None:
    items = [(item.prepared_worktree, item.mapping, item.tested_snapshot) for item in run.repository_evidence]
    if run.repository_group is None:
        prepared = run.prepared_worktree
        if prepared is None:
            raise ValueError("verification checkout is missing")
        items = [(prepared, run.repository, run.tested_snapshot)]
    for prepared, mapping, expected in items:
        repository.assert_head_unchanged(prepared)
        if repository.snapshot(prepared, mapping) != expected:
            raise ValueError("verification checkout changed since tests")


def invoke(node: VerificationNode, request: dict, timeout: int) -> dict:
    # Bound the protocol response; the worker itself spools/limits recipe output.
    import tempfile
    env = execution_environment()
    if node.transport == "ssh" and "SSH_AUTH_SOCK" in os.environ:
        env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        completed = subprocess.run(worker_command(node), input=json.dumps(request).encode(),
            stdout=stdout, stderr=stderr, timeout=timeout, check=False, env=env)
        if completed.returncode:
            stderr.seek(0)
            diagnostic = stderr.read(8192).lower()
            for token, reason in ((b"host key verification failed", "host_key"),
                                  (b"permission denied", "ssh_auth"), (b"connection refused", "ssh_connect"),
                                  (b"could not resolve hostname", "ssh_connect"), (b"connection timed out", "ssh_connect")):
                if token in diagnostic:
                    raise ValueError(reason)
            raise ValueError("verification node unavailable")
        stdout.seek(0)
        raw = stdout.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError("verification response exceeds limit")
        answer = json.loads(raw)
        if answer.get("protocol") != 1:
            raise ValueError("verification protocol mismatch")
        if answer.get("error_code"):
            code = answer["error_code"]
            raise ValueError(code if code in _SAFE_ERRORS else "verification worker failed")
        return answer


def execute(run: WorkflowRun, task: VerificationTask, node: VerificationNode, actor: str) -> VerificationRecord:
    recipe = next(item for item in node.recipes if item.key == task.recipe_key)
    files, bundle = export_bundle(run)
    answer = invoke(node, {"operation": "execute", "files": files, "bundle_digest": bundle,
        "snapshot_digest": task.snapshot_digest, "capabilities": task.need.capabilities,
        "recipe": recipe.model_dump(mode="json")}, recipe.timeout_seconds + 30)
    if answer.get("bundle_digest") != bundle or answer.get("snapshot_digest") != task.snapshot_digest:
        raise ValueError("verification response evidence mismatch")
    system = {"darwin": "macos"}.get(answer.get("system"), answer.get("system"))
    if any(tag.startswith("os:") and tag != f"os:{system}" for tag in task.need.capabilities):
        raise ValueError("verification node platform mismatch")
    machine = str(answer.get("architecture", "")).lower()
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    if any(tag.startswith("arch:") and tag != f"arch:{architecture}" for tag in task.need.capabilities):
        raise ValueError("verification node architecture mismatch")
    if answer.get("status") == "passed" and answer.get("exit_code") != 0:
        raise ValueError("verification exit evidence mismatch")
    evidence = public_text(json.dumps({k: answer.get(k) for k in
        ("system", "architecture", "output", "failure_kind", "artifacts_directory")}, ensure_ascii=False))
    return VerificationRecord(task_key=task.key, snapshot_digest=task.snapshot_digest, bundle_digest=bundle,
        node_key=node.key, recipe_key=recipe.key, recipe_digest=task.recipe_digest, status=answer["status"],
        exit_code=answer.get("exit_code"), actor=actor, evidence=evidence,
        output_sha256=answer["output_sha256"], occurred_at=datetime.now(timezone.utc).isoformat())
