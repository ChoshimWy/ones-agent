"""Host platform paths, independent of repository and workflow configuration."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def user_data_directory() -> Path:
    if sys.platform == "darwin":
        # Use the OS account, not a task-controlled HOME value.
        import pwd

        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        if not home.is_absolute():
            raise OSError("user configuration directory is unavailable")
        return home / "Library" / "Application Support" / "ones-dev"
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local and Path(local).is_absolute() and "\x00" not in local:
            return Path(local) / "ones-dev"
        raise OSError("user configuration directory is unavailable")
    raise OSError("TUI currently supports Windows and macOS")
