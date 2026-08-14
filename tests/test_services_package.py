from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _without_git(script: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("GIT_PYTHON_GIT_EXECUTABLE", None)
    environment["GIT_PYTHON_REFRESH"] = "error"
    environment["PATH"] = ""
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def test_tui_import_does_not_load_git_without_git_executable() -> None:
    completed = _without_git(
        "import sys\n"
        "import src.developer_workflow.tui\n"
        "assert 'git' not in sys.modules\n"
        "print('tui-import-ok')\n"
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == "tui-import-ok\n"


def test_public_ones_gateway_load_does_not_load_execution_service() -> None:
    completed = _without_git(
        "import sys\n"
        "import src.services as services\n"
        "gateway = services.OnesGateway\n"
        "assert gateway.__name__ == 'OnesGateway'\n"
        "assert 'src.services.execution_service' not in sys.modules\n"
        "assert 'git' not in sys.modules\n"
        "print('gateway-ok')\n"
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == "gateway-ok\n"


def test_unknown_public_service_name_raises_standard_attribute_error() -> None:
    completed = _without_git(
        "import src.services as services\n"
        "try:\n"
        "    services.MissingService\n"
        "except AttributeError as error:\n"
        "    assert str(error) == \"module 'src.services' has no attribute 'MissingService'\"\n"
        "else:\n"
        "    raise AssertionError('AttributeError was not raised')\n"
        "print('attribute-error-ok')\n"
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == "attribute-error-ok\n"


def test_concurrent_public_service_load_returns_same_object() -> None:
    completed = _without_git(
        "from concurrent.futures import ThreadPoolExecutor\n"
        "import src.services as services\n"
        "with ThreadPoolExecutor(max_workers=16) as executor:\n"
        "    resolved = list(executor.map(lambda _: services.OnesGateway, range(64)))\n"
        "assert all(item is resolved[0] for item in resolved)\n"
        "assert services.__dict__['OnesGateway'] is resolved[0]\n"
        "print('concurrent-load-ok')\n"
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == "concurrent-load-ok\n"


def test_execution_service_preserves_git_initialization_failure() -> None:
    completed = _without_git(
        "import src.services as services\n"
        "services.ExecutionService\n"
    )

    assert completed.returncode != 0
    assert "Bad git executable" in completed.stderr
