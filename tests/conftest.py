"""Shared pytest fixtures."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest

TX_SCRIPT = Path(__file__).resolve().parent.parent / "tx-pane"


def _load_tx_module():
    # The tx-pane script has no .py extension; force the SourceFileLoader.
    loader = importlib.machinery.SourceFileLoader("tx_mod", str(TX_SCRIPT))
    spec = importlib.util.spec_from_loader("tx_mod", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tx_mod"] = mod
    loader.exec_module(mod)
    # The script itself is now a thin shim. Legacy tests reach in via
    # `tx_module.<name>` for helpers that live in tx_core submodules — re-
    # attach those names so the existing test suite keeps working without
    # mass-renaming the references.
    import tx_core.config  # noqa: F401
    import tx_core.constants  # noqa: F401
    import tx_core.log  # noqa: F401
    import tx_core.marker  # noqa: F401
    import tx_core.output  # noqa: F401
    import tx_core.proc  # noqa: F401
    import tx_core.render  # noqa: F401
    import tx_core.runner  # noqa: F401
    import tx_core.security  # noqa: F401
    import tx_core.state  # noqa: F401
    import tx_core.tmux  # noqa: F401
    import tx_core.util  # noqa: F401
    import tx_core.wait  # noqa: F401
    for src in (
        tx_core.constants,
        tx_core.config,
        tx_core.log,
        tx_core.marker,
        tx_core.output,
        tx_core.proc,
        tx_core.render,
        tx_core.runner,
        tx_core.security,
        tx_core.state,
        tx_core.tmux,
        tx_core.util,
        tx_core.wait,
    ):
        for name in dir(src):
            if name.startswith("__"):
                continue
            if not hasattr(mod, name):
                setattr(mod, name, getattr(src, name))
    return mod


@pytest.fixture(scope="session")
def tx_module():
    return _load_tx_module()


@pytest.fixture
def tx_home(tmp_path: Path) -> Iterator[Path]:
    home = tmp_path / "tx_home"
    home.mkdir()
    yield home


def patch_tx_paths(tx_module, tx_home: Path, monkeypatch) -> None:
    """Redirect every on-disk path constant to `tx_home`.

    Patches the binding in every module that imports the constant by name —
    `tx_module` (the script) plus each `tx_core.*` submodule that has been
    extracted so far. New submodules should be added here as they land.
    """
    import importlib

    paths = {
        "TX_DIR": tx_home,
        "CONFIG_PATH": tx_home / "config.toml",
        "OFFSETS_PATH": tx_home / "offsets.json",
        "LOGS_DIR": tx_home / "logs",
        "LOCK_PATH": tx_home / ".lock",
    }
    targets = [tx_module]
    for modname in ("tx_core.constants", "tx_core.config", "tx_core.state"):
        try:
            targets.append(importlib.import_module(modname))
        except ImportError:
            pass
    for target in targets:
        for name, val in paths.items():
            if hasattr(target, name):
                monkeypatch.setattr(target, name, val)


@pytest.fixture
def unique_session() -> str:
    return f"tx-test-{uuid.uuid4().hex[:8]}"


def _have_tmux() -> bool:
    return shutil.which("tmux") is not None


@pytest.fixture
def tx_runner(tx_home: Path, unique_session: str, tmp_path: Path) -> Iterator[callable]:
    """Returns a function that runs ./tx-pane <args> with isolated TX_PANE_HOME and a
    private tmux server. After the test, the tmux server is killed."""
    if not _have_tmux():
        pytest.skip("tmux not installed")

    # Use silence-mode idle detection so we don't depend on the user's PS1.
    cfg_path = tx_home / "config.toml"
    cfg_path.write_text(
        "[defaults]\n"
        f'tmux_session = "{unique_session}"\n'
        "max_lines = 200\n"
        "timeout = 10\n"
        'idle_method = "silence"\n'
        "idle_silence_ms = 400\n"
        "prompt_patterns = []\n"
        "strip = true\n"
        "\n[security]\n"
        'command_allowlist = "all"\n'
    )

    # tmux's unix sockets are subject to ~104-char path limits on macOS, so we
    # must keep TMUX_TMPDIR short. mkdtemp under /tmp gives us a short path.
    tmux_dir = Path(tempfile.mkdtemp(prefix="txtm-", dir="/tmp"))

    env = os.environ.copy()
    env["TX_PANE_HOME"] = str(tx_home)
    env["TMUX_TMPDIR"] = str(tmux_dir)

    # Subprocess-aware coverage wrapping: when TX_PANE_COV_WRAP=1 (set by the
    # `./run-coverage` harness), invoke tx-pane via `uv run --script --with
    # coverage` so the ephemeral PEP-723 venv has `coverage` available,
    # and inject the sitecustomize that calls `coverage.process_startup()`
    # before tx-pane starts executing.
    if os.environ.get("TX_PANE_COV_WRAP") == "1":
        cov_site = Path(__file__).resolve().parent.parent / ".coverage-site"
        coveragerc = Path(__file__).resolve().parent.parent / ".coveragerc"
        env["COVERAGE_PROCESS_START"] = str(coveragerc)
        env["PYTHONPATH"] = (
            str(cov_site)
            + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        )
        _tx_cmd_prefix = ["uv", "run", "--script", "--with", "coverage"]
    else:
        _tx_cmd_prefix = []

    def run(*args: str, check: bool = False, timeout: float = 30.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*_tx_cmd_prefix, str(TX_SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    # Expose the fixture's env + tx-script path so tests that need to spawn
    # tx-pane via subprocess.Popen (concurrency tests) can use the same isolated
    # TX_PANE_HOME / TMUX_TMPDIR pair.
    run.env = env  # type: ignore[attr-defined]
    run.tx_script = str(TX_SCRIPT)  # type: ignore[attr-defined]

    try:
        yield run
    finally:
        # Kill our private tmux server (use the same env so it finds the socket).
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
        shutil.rmtree(tmux_dir, ignore_errors=True)


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
