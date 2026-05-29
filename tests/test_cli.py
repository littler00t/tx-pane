"""Tests for the CLI surface (help, config, error messages) without tmux."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TX_SCRIPT = Path(__file__).resolve().parent.parent / "tx-pane"


def _run_tx(env: dict[str, str], *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(TX_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch):
    home = tmp_path / "tx_home"
    home.mkdir()
    env = os.environ.copy()
    env["TX_PANE_HOME"] = str(home)
    return env, home


def test_help_root_exits_zero(isolated_env):
    env, _ = isolated_env
    res = _run_tx(env, "--help")
    assert res.returncode == 0
    assert "PANE LIFECYCLE" in res.stdout
    assert "READING OUTPUT" in res.stdout
    assert "tx-pane tail" in res.stdout


def test_subcommand_help(isolated_env):
    env, _ = isolated_env
    for sub in ["new", "ls", "run", "send", "key", "tail", "dump", "wait", "reset", "kill", "config"]:
        res = _run_tx(env, sub, "--help")
        assert res.returncode == 0, f"{sub} --help exit code {res.returncode}: {res.stderr}"


def test_sudo_help_shows_yes(isolated_env):
    env, _ = isolated_env
    res = _run_tx(env, "sudo", "--help")
    assert res.returncode == 0
    assert "--yes" in res.stdout


def test_config_creates_default(isolated_env):
    env, home = isolated_env
    res = _run_tx(env, "config")
    assert res.returncode == 0
    assert 'tmux_session = "tx-pane"' in res.stdout
    assert "max_lines = 200" in res.stdout
    assert (home / "config.toml").exists()


def test_pane_not_found_error(isolated_env):
    env, _ = isolated_env
    res = _run_tx(env, "tail", "nonexistent")
    assert res.returncode == 1
    assert "[error:" in res.stdout
    assert "not found" in res.stdout


def test_continue_without_pending_errors(isolated_env, tmp_path):
    env, home = isolated_env
    # Manually seed a pane state with no pending.
    offsets = {
        "_next_id": 2,
        "_panes": {"p1": "%99"},
        "p1": {"tmux_id": "%99", "tail_offset": 0, "continue_offset": None, "status": "idle"},
    }
    (home / "offsets.json").write_text(json.dumps(offsets))
    (home / "logs").mkdir()
    (home / "logs" / "p1.log").write_text("some content\n")
    res = _run_tx(env, "tail", "p1", "--continue")
    assert res.returncode == 1
    assert "no truncation in progress" in res.stdout


def test_ls_empty(isolated_env):
    env, _ = isolated_env
    res = _run_tx(env, "ls")
    assert res.returncode == 0
    assert "[empty:" in res.stdout
