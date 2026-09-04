"""Migrate a reviewed patch to new worktrees; never rewrite the old checkout."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .contracts import PreparedWorktree, RepositoryRunEvidence, RepositorySnapshot
from .repository import RepositoryBoundaryError, RepositoryCommandError, WorktreeRepository


class BaselineMigrationError(RuntimeError):
    """A migration cannot be completed without losing or misbinding evidence."""


def _git(repo: WorktreeRepository, path: Path, args: list[str], *,
         env: dict[str, str] | None = None, data: bytes | None = None,
         check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", "-C", str(path), *args], cwd=path,
                            env=env or repo._git_environment(), input=data,
                            capture_output=True, timeout=120, check=False)
    if check and result.returncode:
        raise RepositoryCommandError("baseline migration Git operation failed")
    return result


def transfer(repo: WorktreeRepository, source: RepositoryRunEvidence,
             destination: PreparedWorktree) -> tuple[RepositorySnapshot, tuple[str, ...]]:
    """Use a temporary source index to include binary/untracked files in a patch.

    The source index, HEAD and files are never modified. The destination must be
    a new, clean, verified worktree. Conflicts remain there, not in the source.
    """
    old, mapping, expected = source.prepared_worktree, source.mapping, source.tested_snapshot
    repo.assert_head_unchanged(old)
    if expected is None or repo.snapshot(old, mapping) != expected:
        raise BaselineMigrationError("baseline migration source evidence changed")
    repo.assert_head_unchanged(destination)
    if not repo.snapshot(destination, mapping).is_clean:
        raise BaselineMigrationError("baseline migration destination is not clean")
    # Verify identity before passing worktree paths to subprocesses.
    old_path, _ = repo._validate_identity(old, mapping)
    new_path, _ = repo._validate_identity(destination, mapping)
    if old_path == new_path:
        raise BaselineMigrationError("baseline migration cannot overwrite source")
    if not expected.changed_files:
        return repo.snapshot(destination, mapping), ()
    descriptor, index = tempfile.mkstemp(prefix="baseline-index-", dir=old.mirror_path)
    os.close(descriptor)
    os.unlink(index)
    env = repo._git_environment()
    env["GIT_INDEX_FILE"] = index
    try:
        _git(repo, old_path, ["read-tree", old.head_commit], env=env)
        _git(repo, old_path, ["add", "-A", "--", *expected.changed_files], env=env)
        patch = _git(repo, old_path, ["diff", "--cached", "--binary", "--full-index",
                                    "--no-ext-diff", "--no-textconv", old.head_commit, "--"], env=env).stdout
        if len(patch) > repo.max_snapshot_bytes:
            raise RepositoryBoundaryError("baseline patch exceeds snapshot limit")
    finally:
        Path(index).unlink(missing_ok=True)
        Path(index + ".lock").unlink(missing_ok=True)
    if repo.snapshot(old, mapping) != expected:
        raise BaselineMigrationError("baseline migration source evidence changed")
    applied = _git(repo, new_path, ["apply", "--3way", "--index", "--whitespace=nowarn"], data=patch, check=False)
    conflict_bytes = _git(repo, new_path, ["diff", "--name-only", "--diff-filter=U", "-z"]).stdout
    conflicts = tuple(value.decode("utf-8", "strict") for value in conflict_bytes.split(b"\0") if value)
    for path in conflicts:
        RepositorySnapshot._validate_repository_path(path)
        if path not in expected.changed_files:
            raise BaselineMigrationError("baseline conflict outside original repair")
        conflict_file = repo.resolve_repository_path(destination, mapping, path)
        if not conflict_file.is_file():
            raise BaselineMigrationError("baseline conflict requires manual resolution")
        with conflict_file.open("rb") as stream:
            if b"\0" in stream.read(8192):
                raise BaselineMigrationError("baseline conflict requires manual resolution")
    if applied.returncode and not conflicts:
        raise BaselineMigrationError("baseline patch could not be migrated")
    # Reset only the index in our newly created destination; keep merged files
    # and conflict markers for the existing repair stage. Never reset source.
    _git(repo, new_path, ["reset", "--mixed", destination.head_commit])
    after = repo.snapshot(destination, mapping)
    if not set(after.changed_files) <= set(expected.changed_files):
        raise BaselineMigrationError("baseline migration changed unrelated paths")
    if repo.snapshot(old, mapping) != expected:
        raise BaselineMigrationError("baseline migration source evidence changed")
    return after, conflicts
