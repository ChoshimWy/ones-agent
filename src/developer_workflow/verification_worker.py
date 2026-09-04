"""Standalone stdlib worker, install on an authorized local/SSH validation node.

One JSON request on stdin, one JSON response on stdout. No shell execution.
The SSH endpoint is a trusted execution boundary: only authorized controllers
may submit requests. Use a dedicated test account and trusted recipe programs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import sys
import tempfile
import time

MAX_BYTES = 64 * 1024 * 1024


def execution_environment() -> dict[str, str]:
    """Do not hand the controller/node account's API credentials to test code."""
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TMP", "TEMP", "TMPDIR",
               "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
               "LANG", "LC_ALL", "LANGUAGE", "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def safe_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or "\\" in value or ":" in value
            or any(p in {"", ".", ".."} or p.casefold() == ".git" or p.endswith((".", " "))
                   or re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", p)
                   for p in value.split("/"))
            or any(c in value for c in '<>"|?*')
            or any(ord(c) < 32 for c in value)):
        raise ValueError("unsafe bundle path")
    return path


def execute(request: dict) -> dict:
    if request.get("operation") == "probe":
        return {"protocol": 1, "system": platform.system().lower(), "architecture": platform.machine(),
                "python": platform.python_version()}
    system = {"darwin": "macos"}.get(platform.system().lower(), platform.system().lower())
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    if any((tag.startswith("os:") and tag != f"os:{system}")
           or (tag.startswith("arch:") and tag != f"arch:{machine}") for tag in request.get("capabilities", [])):
        raise ValueError("node platform does not match the requested verification")
    files = request["files"]
    manifest = {key: {"sha256": value["sha256"], "executable": bool(value.get("executable"))}
                for key, value in files.items()}
    if len(files) > 20000 or digest(manifest) != request["bundle_digest"]:
        raise ValueError("bundle manifest mismatch")
    recipe = request["recipe"]
    argv = recipe["argv"]
    if (not isinstance(argv, list) or not argv or len(argv) > 64
            or any(not isinstance(v, str) or not v or "\x00" in v for v in argv)):
        raise ValueError("invalid recipe")
    timeout = int(recipe["timeout_seconds"])
    if not 1 <= timeout <= 3600:
        raise ValueError("invalid timeout")
    # New private directory per attempt; never overwrite an existing checkout.
    root = Path(tempfile.mkdtemp(prefix="ones-verification-"))
    size = 0
    normalized_names: set[str] = set()
    for name, value in files.items():
        if name.casefold() in normalized_names:
            raise ValueError("case-colliding bundle paths")
        normalized_names.add(name.casefold())
        target = root.joinpath(*safe_path(name).parts)
        content = base64.b64decode(value["data"], validate=True)
        size += len(content)
        if size > MAX_BYTES or hashlib.sha256(content).hexdigest() != value["sha256"]:
            raise ValueError("bundle content mismatch")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
        if value.get("executable") and os.name != "nt":
            target.chmod(0o700)
    cwd = root.joinpath(*safe_path(recipe["repository_key"]).parts)
    if not cwd.is_dir():
        raise ValueError("recipe repository is absent")
    started = time.monotonic()
    output_path = root / "verification-output.log"
    status, code, failure_kind = "error", None, ""
    with output_path.open("wb") as output:
        process = subprocess.Popen(argv, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, shell=False, env=execution_environment(),
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        try:
            while process.poll() is None:
                if time.monotonic() - started > timeout or output_path.stat().st_size > 8 * 1024 * 1024:
                    raise TimeoutError("verification resource limit")
                time.sleep(0.05)
            code = process.returncode
            status = "passed" if code == 0 else "failed"
        except TimeoutError:
            failure_kind = "timeout_or_output_limit"
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import signal
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    with output_path.open("rb") as output:
        raw = output.read(8 * 1024 * 1024)
    if output_path.stat().st_size > 8 * 1024 * 1024:
        status, code = "error", None
        failure_kind = "output_limit"
    # A validator may create artifacts, but may not rewrite the code it claims to test.
    for name, expected in manifest.items():
        target = root.joinpath(*safe_path(name).parts)
        if (not target.is_file() or target.is_symlink()
                or not target.resolve().is_relative_to(root)
                or target.stat().st_size > MAX_BYTES
                or hashlib.sha256(target.read_bytes()).hexdigest() != expected["sha256"]
                or (os.name != "nt" and bool(target.stat().st_mode & 0o111) != expected["executable"])):
            status, code = "error", None
            failure_kind = "tested_source_changed"
            break
    return {"protocol": 1, "status": status, "exit_code": code,
            "failure_kind": failure_kind,
            "bundle_digest": request["bundle_digest"], "snapshot_digest": request["snapshot_digest"],
            "system": platform.system().lower(), "architecture": platform.machine(),
            "output": raw[-12000:].decode("utf-8", "replace"),
            "output_sha256": hashlib.sha256(raw).hexdigest(),
            "artifacts_directory": str(root)}


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(96 * 1024 * 1024 + 1)
        if len(raw) > 96 * 1024 * 1024:
            raise ValueError("request too large")
        result = execute(json.loads(raw))
    except Exception as error:
        code = "missing_runtime" if isinstance(error, FileNotFoundError) else "node_permission" if isinstance(error, PermissionError) else "invalid_request"
        if str(error) == "node platform does not match the requested verification":
            code = str(error)
        result = {"protocol": 1, "status": "error", "error_code": code}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
