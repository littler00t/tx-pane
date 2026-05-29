"""End-to-end tests that exercise tx against a real tmux server."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest


def _pane_id_from(result: subprocess.CompletedProcess) -> str:
    """Extract the pane id from a `tx new` invocation."""
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip().splitlines()[-1].strip()


def test_new_returns_auto_id(tx_runner, tx_home):
    res = tx_runner("new")
    pane = _pane_id_from(res)
    assert pane == "p1"
    offsets = json.loads((tx_home / "offsets.json").read_text())
    assert pane in offsets
    # tail_offset is set past the v2 init prelude so subsequent reads don't
    # surface the SHELL_INIT_SETUP line. Just verify it's a non-negative int.
    assert offsets[pane]["tail_offset"] >= 0


def test_new_with_name(tx_runner, tx_home):
    res = tx_runner("new", "server")
    pane = _pane_id_from(res)
    assert pane == "server"


def test_new_duplicate_name_errors(tx_runner):
    tx_runner("new", "first")
    res = tx_runner("new", "first")
    assert res.returncode == 1
    assert "already exists" in res.stdout


def test_run_echo_returns_output(tx_runner):
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner("run", pane, "echo hello-world", timeout=15)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "hello-world" in res.stdout


def test_run_captures_exit_code(tx_runner):
    pane = _pane_id_from(tx_runner("new"))
    # Use a command guaranteed to produce a non-zero exit.
    res = tx_runner("run", pane, "false", timeout=15)
    assert res.returncode == 0
    # Exit code line should appear.
    assert "[exit:" in res.stdout


def test_run_truncation_and_continue(tx_runner):
    pane = _pane_id_from(tx_runner("new"))
    # seq prints lots of lines quickly.
    res = tx_runner("run", "--raw", pane, "seq 1 50", "--max", "10", timeout=15)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[truncated:" in res.stdout
    # Some early numbers should appear.
    lines = res.stdout.splitlines()
    assert any(l.strip() == "1" for l in lines)
    # Continue.
    res2 = tx_runner("tail", pane, "--max", "10", "--continue")
    assert res2.returncode == 0
    assert "[truncated:" in res2.stdout or "[end of output]" in res2.stdout


def test_dump_returns_full_buffer(tx_runner):
    pane = _pane_id_from(tx_runner("new"))
    tx_runner("run", pane, "echo first-line", timeout=15)
    res = tx_runner("dump", pane)
    assert res.returncode == 0
    assert "first-line" in res.stdout


def test_send_then_key_enter_executes(tx_runner):
    pane = _pane_id_from(tx_runner("new"))
    # Wait for the shell to be ready.
    time.sleep(0.5)
    tx_runner("send", pane, "echo via-key-enter")
    tx_runner("key", pane, "Enter")
    time.sleep(1.0)
    res = tx_runner("tail", pane, timeout=15)
    assert "via-key-enter" in res.stdout


def test_wait_match(tx_runner):
    pane = _pane_id_from(tx_runner("new"))
    # Schedule output that arrives after a moment.
    tx_runner("send", pane, "sleep 0.3 && echo TARGET_MARKER")
    tx_runner("key", pane, "Enter")
    res = tx_runner("wait", pane, "TARGET_MARKER", "--timeout", "5", timeout=10)
    assert res.returncode == 0
    assert "TARGET_MARKER" in res.stdout
    assert "[timeout:" not in res.stdout


def test_wait_timeout(tx_runner):
    pane = _pane_id_from(tx_runner("new"))
    # Nothing produces this pattern.
    res = tx_runner("wait", pane, "WILL_NEVER_APPEAR", "--timeout", "1", timeout=10)
    assert res.returncode == 0
    assert "[timeout:" in res.stdout


def test_ls_lists_managed_panes(tx_runner):
    p1 = _pane_id_from(tx_runner("new"))
    p2 = _pane_id_from(tx_runner("new", "named"))
    res = tx_runner("ls")
    assert res.returncode == 0
    assert p1 in res.stdout
    assert p2 in res.stdout
    assert "ID" in res.stdout
    assert "STATUS" in res.stdout


def test_ls_format_tsv(tx_runner):
    tx_runner("new")
    tx_runner("new", "longer-name-here")
    res = tx_runner("ls", "--format", "tsv")
    assert res.returncode == 0
    # No header in TSV, each line is TAB-separated.
    for line in res.stdout.strip().splitlines():
        assert "\t" in line


def test_ls_format_json(tx_runner):
    tx_runner("new")
    tx_runner("new", "second")
    res = tx_runner("ls", "--format", "json")
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert isinstance(data, list)
    assert len(data) == 2
    assert all("id" in row and "status" in row for row in data)


def test_kill_removes_pane(tx_runner, tx_home):
    pane = _pane_id_from(tx_runner("new"))
    log_path = tx_home / "logs" / f"{pane}.log"
    assert log_path.exists()
    res = tx_runner("kill", pane)
    assert res.returncode == 0
    assert "[killed:" in res.stdout
    offsets = json.loads((tx_home / "offsets.json").read_text())
    assert pane not in offsets
    # Log file is preserved.
    assert log_path.exists()


def test_reset_advances_offset(tx_runner, tx_home):
    pane = _pane_id_from(tx_runner("new"))
    tx_runner("run", pane, "echo something", timeout=15)
    res = tx_runner("reset", pane)
    assert res.returncode == 0
    assert "[reset:" in res.stdout
    # A subsequent tail should produce no output (nothing new).
    res2 = tx_runner("tail", pane)
    assert res2.returncode == 0
    assert res2.stdout.strip() == ""


def _force_pane_dead(offsets_path: Path, pane: str) -> None:
    """Rewrite offsets so `pane`'s tmux_id points at a non-existent tmux pane.

    Used by the restart tests to simulate a dead pane without actually
    killing it through the tmux server (which would race with the test).
    """
    offsets = json.loads(offsets_path.read_text())
    offsets[pane]["tmux_id"] = "%999999"
    offsets.setdefault("_panes", {})[pane] = "%999999"
    offsets_path.write_text(json.dumps(offsets))


def test_restart_revives_dead_pane(tx_runner, tx_home):
    """`tx restart` on a dead pane allocates a fresh tmux pane and writes a
    divider into the existing log so prior output is preserved."""
    pane = _pane_id_from(tx_runner("new"))
    log_path = tx_home / "logs" / f"{pane}.log"
    # Drop a sentinel byte in the log so we can verify the original file
    # is preserved (append mode) across the restart.
    log_path.write_bytes(b"pre-restart sentinel\n")
    pre_size = log_path.stat().st_size

    _force_pane_dead(tx_home / "offsets.json", pane)

    res = tx_runner("restart", pane, timeout=15)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[restarted:" in res.stdout

    # tmux_id changed and log is bigger (divider + init-snippet echo).
    offsets = json.loads((tx_home / "offsets.json").read_text())
    assert offsets[pane]["tmux_id"] != "%999999"
    assert offsets[pane]["active_run"] is None
    assert offsets[pane]["hook_ok"] is True
    assert log_path.stat().st_size > pre_size

    # Confirm the divider line is in the log somewhere.
    body = log_path.read_text(errors="replace")
    assert "--- tx restart at " in body
    assert "pre-restart sentinel" in body


def test_restart_refuses_on_live_pane(tx_runner):
    """The command exists specifically for dead panes; a live one is an error."""
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner("restart", pane)
    assert res.returncode == 1
    assert "alive" in res.stdout
    assert "dead panes" in res.stdout


def test_allowlist_blocks_disallowed(tx_runner, tx_home):
    cfg = (tx_home / "config.toml").read_text()
    cfg = cfg.replace('command_allowlist = "all"', 'command_allowlist = ["echo"]')
    (tx_home / "config.toml").write_text(cfg)
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner("run", pane, "ls -la")
    assert res.returncode == 1
    assert "not in command_allowlist" in res.stdout


def test_allowlist_permits_allowed(tx_runner, tx_home):
    cfg = (tx_home / "config.toml").read_text()
    cfg = cfg.replace('command_allowlist = "all"', 'command_allowlist = ["echo"]')
    (tx_home / "config.toml").write_text(cfg)
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner("run", pane, "echo allowed-fine", timeout=15)
    assert res.returncode == 0
    assert "allowed-fine" in res.stdout


def test_send_disallowed_refuses_and_does_not_send(tx_runner, tx_home):
    cfg = (tx_home / "config.toml").read_text()
    cfg = cfg.replace('command_allowlist = "all"', 'command_allowlist = ["echo"]')
    (tx_home / "config.toml").write_text(cfg)
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner("send", pane, "ls -la")
    assert res.returncode == 1
    assert "not in command_allowlist" in res.stdout
    tx_runner("key", pane, "Enter")
    time.sleep(0.5)
    tail = tx_runner("tail", pane, timeout=15)
    assert "ls -la" not in tail.stdout


def test_send_confirm_match_refuses_and_does_not_send(tx_runner, tx_home):
    cfg = (tx_home / "config.toml").read_text()
    cfg += '\nconfirm_patterns = ["echo confirm-blocked"]\n'
    (tx_home / "config.toml").write_text(cfg)
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner("send", pane, "echo confirm-blocked")
    assert res.returncode == 1
    assert "confirmation required" in res.stdout or "confirm_pattern" in res.stdout
    tx_runner("key", pane, "Enter")
    time.sleep(0.5)
    tail = tx_runner("tail", pane, timeout=15)
    assert "confirm-blocked" not in tail.stdout
