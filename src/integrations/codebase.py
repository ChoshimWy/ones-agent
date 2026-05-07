"""代码仓库读取 - 本地路径 / 远程 Git"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".next", ".nuxt", "target", "bin", "obj"}
CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".vue", ".html", ".css", ".scss", ".sql", ".sh", ".yaml", ".yml", ".json", ".xml"}

_PROJECT_ROOT = Path(__file__).parent.parent.parent


class Codebase:
    def __init__(self, path: str | None = None, repo_url: str | None = None, branch: str = "main"):
        self.path = Path(path) if path else None
        self.repo_url = repo_url
        self.branch = branch

        if not self.path and repo_url:
            self.path = self._clone(repo_url, branch)

    @staticmethod
    def _clone(url: str, branch: str = "main") -> Path:
        name = url.rstrip("/").split("/")[-1].removesuffix(".git")
        dest = _PROJECT_ROOT / "data" / "repos" / name
        if dest.exists():
            subprocess.run(["git", "fetch", "--depth=1"], cwd=str(dest), capture_output=True)
            subprocess.run(["git", "checkout", branch], cwd=str(dest), capture_output=True)
            subprocess.run(["git", "pull"], cwd=str(dest), capture_output=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth=1", "--branch", branch, url, str(dest)],
                capture_output=True,
            )
        return dest

    def tree(self, max_depth: int = 3) -> str:
        if not self.path or not self.path.exists():
            return ""
        lines: list[str] = []
        base_len = len(str(self.path))
        for root, dirs, files in os.walk(self.path):
            rel = root[base_len:].replace("\\", "/")
            depth = rel.count("/")
            if depth >= max_depth:
                dirs.clear()
                continue
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            if rel:
                lines.append(f"{'  ' * depth}{Path(rel).name}/")
            for f in sorted(files):
                lines.append(f"{'  ' * (depth + 1)}{f}")
        return "\n".join(lines[:500])

    def read_file(self, rel_path: str) -> str | None:
        if not self.path:
            return None
        full = self.path / rel_path
        try:
            return full.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError):
            return None

    def search_keywords(self, keywords: list[str], max_files: int = 10) -> dict[str, str]:
        if not self.path or not self.path.exists():
            return {}
        results: dict[str, str] = {}
        kw_lower = [k.lower() for k in keywords]

        for root, dirs, files in os.walk(self.path):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for f in files:
                if len(results) >= max_files:
                    return results
                if Path(f).suffix not in CODE_EXTS:
                    continue
                full = Path(root) / f
                rel = str(full.relative_to(self.path)).replace("\\", "/")
                if any(k in f.lower() for k in kw_lower):
                    content = full.read_text(encoding="utf-8", errors="replace")[:8000]
                    results[rel] = content
                    continue
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        for i, line in enumerate(fh):
                            if i > 500:
                                break
                            if any(k in line.lower() for k in kw_lower):
                                content = full.read_text(encoding="utf-8", errors="replace")[:8000]
                                results[rel] = content
                                break
                except (OSError, PermissionError):
                    continue
        return results

    def get_context_for_defect(self, defect: dict, max_files: int = 5) -> dict[str, str]:
        name = defect.get("name", "")
        desc = defect.get("description", "")
        text = f"{name} {desc}".lower()
        words = [w for w in text.replace("/", " ").replace("-", " ").replace("_", " ").split()
                 if len(w) > 2 and w not in {"the", "and", "for", "not", "but", "with", "has", "are", "was", "all"}]
        return self.search_keywords(words[:10], max_files=max_files) if words else {}
