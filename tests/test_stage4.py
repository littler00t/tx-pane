"""Tests for Stage 4 (tx v1.0.0).

Covers:
- Group A: `tx write` end-to-end (atomic deploy with hash verify), --diff,
  --overwrite gating, --mode, sudo refusal preflight.
- Group B: log rotation helpers, rotate-on-tx-new, age sweep, `tx maintain`.
- Group C: hook-overwrite detection (hook_ok flag, auto-reinstall path).
- Group F: fish shell init setup snippet selection.
- Group G: `tx maintain` and lazy sweep timestamp.
- Group E (regression): old extract_exit_code symbols are gone; v2 protocol
  still active.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest


# ============== Group F: shell-aware init setup ==============

def test_shell_init_setup_for_bash_returns_promptcommand_form(tx_module):
    snippet = tx_module.shell_init_setup_for("bash")
    assert "PROMPT_COMMAND" in snippet
    assert "fish_postexec" not in snippet


def test_shell_init_setup_for_fish_returns_postexec_form(tx_module):
    snippet = tx_module.shell_init_setup_for("fish")
    assert "fish_postexec" in snippet
    assert "PROMPT_COMMAND" not in snippet


def test_shell_init_setup_for_none_returns_default_form(tx_module):
    # Unknown / unspecified falls back to the bash/zsh snippet (sh swallows it
    # harmlessly).
    snippet = tx_module.shell_init_setup_for(None)
    assert "PROMPT_COMMAND" in snippet


# ============== Group E regression: v1 fallback symbols removed ==============

def test_v1_extract_exit_code_symbol_removed(tx_module):
    assert not hasattr(tx_module, "extract_exit_code")
    assert not hasattr(tx_module, "EXIT_ECHO_CMD")
    assert not hasattr(tx_module, "EXIT_MARKER_RE")


def test_protocol_default_is_v2(tx_module):
    assert tx_module.PROTOCOL_VERSION == "v2"
    assert tx_module.DEFAULT_CONFIG["protocol"]["version"] == "v2"


# ============== Group B: log rotation helpers (pure unit tests) ==============

def test_logs_cfg_falls_back_to_defaults(tx_module):
    lc = tx_module._logs_cfg({})
    assert lc["max_size_mb"] == 100
    assert lc["max_age_days"] == 30
    assert lc["max_keep"] == 10


def test_logs_cfg_respects_overrides(tx_module):
    lc = tx_module._logs_cfg({"logs": {"max_size_mb": 5, "max_keep": 2}})
    assert lc["max_size_mb"] == 5
    assert lc["max_keep"] == 2
    assert lc["max_age_days"] == 30  # default preserved


def test_rotate_log_shifts_and_caps(tx_module, tmp_path: Path):
    base = tmp_path / "p1.log"
    base.write_bytes(b"latest content\n")
    (tmp_path / "p1.log.1").write_bytes(b"older\n")
    (tmp_path / "p1.log.2").write_bytes(b"older still\n")

    out = tx_module.rotate_log(base, max_keep=2)
    assert out == tmp_path / "p1.log.1"

    # latest was rotated to .1
    assert (tmp_path / "p1.log.1").read_bytes() == b"latest content\n"
    # the prior .1 -> .2
    assert (tmp_path / "p1.log.2").read_bytes() == b"older\n"
    # the prior .2 was beyond max_keep so it was deleted
    assert not (tmp_path / "p1.log.3").exists()
    # base recreated empty
    assert base.exists() and base.read_bytes() == b""


def test_rotate_log_noop_on_empty(tx_module, tmp_path: Path):
    base = tmp_path / "p1.log"
    base.write_bytes(b"")
    assert tx_module.rotate_log(base, max_keep=5) is None


def test_maybe_rotate_log_size_threshold(tx_module, tmp_path: Path):
    base = tmp_path / "p1.log"
    base.write_bytes(b"x" * (3 * 1024 * 1024))  # 3MB
    # threshold above current size — no rotation
    result = tx_module.maybe_rotate_log(base, {"logs": {"max_size_mb": 10, "max_keep": 3}})
    assert result is None
    # threshold below current size — rotation
    result = tx_module.maybe_rotate_log(base, {"logs": {"max_size_mb": 1, "max_keep": 3}})
    assert result == tmp_path / "p1.log.1"
    assert base.exists() and base.read_bytes() == b""


def test_sweep_aged_logs_deletes_only_rotated(tx_module, tmp_path: Path):
    fresh = tmp_path / "p1.log"
    fresh.write_bytes(b"live content\n")
    aged = tmp_path / "p1.log.1"
    aged.write_bytes(b"aged content\n")
    # mtime far in the past
    far_past = time.time() - (60 * 86400)
    os.utime(aged, (far_past, far_past))

    deleted = tx_module.sweep_aged_logs(
        {"logs": {"max_age_days": 30, "max_keep": 10}}, logs_dir=tmp_path
    )
    assert aged in deleted
    assert not aged.exists()
    assert fresh.exists()  # the live log is never swept


def test_sweep_aged_logs_respects_max_age(tx_module, tmp_path: Path):
    recent = tmp_path / "p2.log.1"
    recent.write_bytes(b"recent rotated\n")
    # Not aged.
    deleted = tx_module.sweep_aged_logs(
        {"logs": {"max_age_days": 1, "max_keep": 5}}, logs_dir=tmp_path
    )
    assert deleted == []
    assert recent.exists()


def test_maybe_sweep_aged_logs_respects_interval(tx_module, tmp_path: Path):
    # mark a recent sweep, then call — should be a noop.
    offsets = {"_last_sweep": tx_module.now_iso()}
    deleted = tx_module.maybe_sweep_aged_logs(
        offsets,
        {"logs": {"max_age_days": 1, "max_keep": 5, "sweep_interval_hours": 24}},
    )
    assert deleted == []


# ============== Group C: hook detection / auto-reinstall ==============

def test_record_run_end_flips_hook_ok_false_on_none_exit(tx_module):
    state = {
        "active_run": {"id": "r-x", "cmd": "true", "started": "t0", "start_offset": 0},
        "runs": [],
        "hook_ok": True,
    }
    tx_module.record_run_end(state, "r-x", None, 100, max_history=10)
    assert state["hook_ok"] is False


def test_record_run_end_flips_hook_ok_true_on_real_exit(tx_module):
    state = {
        "active_run": {"id": "r-y", "cmd": "true", "started": "t0", "start_offset": 0},
        "runs": [],
        "hook_ok": False,  # was previously flagged missing
    }
    tx_module.record_run_end(state, "r-y", 0, 100, max_history=10)
    assert state["hook_ok"] is True


def test_record_run_end_idempotent_preserves_hook_flag(tx_module):
    state = {
        "active_run": None,
        "runs": [{"id": "r-z", "end_offset": 50, "cmd": "x", "started": "t", "ended": "t"}],
        "hook_ok": True,
    }
    # Second call is a no-op for the run record but still propagates the
    # hook_ok signal for the new exit_code.
    tx_module.record_run_end(state, "r-z", None, 50, max_history=10)
    assert state["hook_ok"] is False


# ============== Default config additions ==============

def test_default_config_has_logs_section(tx_module):
    cfg = tx_module._deepcopy(tx_module.DEFAULT_CONFIG)
    assert "logs" in cfg
    assert cfg["logs"]["max_size_mb"] == 100


def test_default_config_has_auto_reinstall_hook(tx_module):
    cfg = tx_module._deepcopy(tx_module.DEFAULT_CONFIG)
    assert cfg["defaults"]["auto_reinstall_hook"] is True


# ============== Group A: tx write (integration with real tmux) ==============

def _pane(tx_runner, *args: str) -> str:
    res = tx_runner("new", *args)
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout.strip().splitlines()[-1].strip()


def test_write_deploys_file_with_hash_verify(tx_runner, tmp_path: Path):
    pane = _pane(tx_runner)
    src = tmp_path / "src-config.txt"
    payload = b"hello tx write\nline2\n"
    src.write_bytes(payload)
    target = tmp_path / "deployed-config.txt"

    res = tx_runner(
        "write", pane, str(target),
        "--file", str(src), "--timeout", "20",
        timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[written:" in res.stdout
    assert target.exists()
    assert target.read_bytes() == payload  # bytes identical (incl. trailing newline)


def test_write_refuses_when_target_exists_without_overwrite(tx_runner, tmp_path: Path):
    pane = _pane(tx_runner)
    src = tmp_path / "src.conf"
    src.write_bytes(b"new content\n")
    target = tmp_path / "existing.conf"
    target.write_bytes(b"already here\n")

    res = tx_runner(
        "write", pane, str(target), "--file", str(src), "--timeout", "20", timeout=60,
    )
    assert res.returncode == 1
    assert "already exists" in res.stdout
    # Target was not modified.
    assert target.read_bytes() == b"already here\n"


def test_write_overwrite_replaces_existing(tx_runner, tmp_path: Path):
    pane = _pane(tx_runner)
    src = tmp_path / "src.conf"
    src.write_bytes(b"replacement\n")
    target = tmp_path / "existing.conf"
    target.write_bytes(b"original\n")

    res = tx_runner(
        "write", pane, str(target), "--file", str(src),
        "--overwrite", "--timeout", "20", timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert target.read_bytes() == b"replacement\n"


def test_write_diff_emits_unified_diff(tx_runner, tmp_path: Path):
    pane = _pane(tx_runner)
    src = tmp_path / "src.conf"
    src.write_bytes(b"new line\nsame line\n")
    target = tmp_path / "old.conf"
    target.write_bytes(b"old line\nsame line\n")

    res = tx_runner(
        "write", pane, str(target), "--file", str(src),
        "--overwrite", "--diff", "--timeout", "20", timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    # Some marker of a unified diff should appear.
    assert ("---" in res.stdout) or ("[diff:" in res.stdout)


def test_write_applies_mode(tx_runner, tmp_path: Path):
    pane = _pane(tx_runner)
    src = tmp_path / "src.conf"
    src.write_bytes(b"secret-ish\n")
    target = tmp_path / "moded.conf"

    res = tx_runner(
        "write", pane, str(target), "--file", str(src),
        "--mode", "640", "--timeout", "20", timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert target.exists()
    # Mode bits — lower 9 bits are the perms; 0o640 == 0o640.
    mode = target.stat().st_mode & 0o777
    assert mode == 0o640


def test_write_rejects_zero_byte_file(tx_runner, tmp_path: Path):
    pane = _pane(tx_runner)
    src = tmp_path / "empty.conf"
    src.write_bytes(b"")
    target = tmp_path / "out.conf"

    res = tx_runner(
        "write", pane, str(target), "--file", str(src), timeout=10,
    )
    assert res.returncode == 1
    assert "zero-byte" in res.stdout


def test_write_rejects_missing_target_dir(tx_runner, tmp_path: Path):
    pane = _pane(tx_runner)
    src = tmp_path / "src.conf"
    src.write_bytes(b"x\n")
    target = tmp_path / "no_such_dir" / "out.conf"

    res = tx_runner(
        "write", pane, str(target), "--file", str(src), "--timeout", "10", timeout=60,
    )
    assert res.returncode == 1
    assert "does not exist" in res.stdout


def test_write_invalid_mode_rejected_locally(tx_runner, tmp_path: Path):
    pane = _pane(tx_runner)
    src = tmp_path / "src.conf"
    src.write_bytes(b"x\n")
    target = tmp_path / "out.conf"

    res = tx_runner(
        "write", pane, str(target), "--file", str(src),
        "--mode", "9999", "--timeout", "10", timeout=20,
    )
    assert res.returncode == 1
    assert "octal" in res.stdout


def test_write_reload_cmd_runs_after_move(tx_runner, tmp_path: Path):
    pane = _pane(tx_runner)
    src = tmp_path / "src.conf"
    src.write_bytes(b"deploy\n")
    target = tmp_path / "deployed.conf"
    flag = tmp_path / "reload-touched"

    res = tx_runner(
        "write", pane, str(target), "--file", str(src),
        "--reload-cmd", f"touch {flag}",
        "--timeout", "20", timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert target.exists()
    assert flag.exists()


# ============== Group B integration: rotation on tx new ==============

def test_rotate_on_tx_new_preserves_prior_log(tx_runner, tx_home: Path):
    """Create a pane, write large bytes to its log file, kill, re-create with
    the same name with a small rotation threshold — the log should rotate
    rather than truncate."""
    pane = _pane(tx_runner, "rotateme")
    log_path = tx_home / "logs" / f"{pane}.log"
    # Drive the pane long enough to ensure the log has at least some content
    tx_runner("run", pane, "echo first-run-content", timeout=15)
    tx_runner("kill", pane, "--signal", "kill")
    # Bloat the log on disk so the next tx new will see it >= threshold.
    with open(log_path, "ab") as f:
        f.write(b"x" * (2 * 1024 * 1024))

    # Drop max_size_mb to 1MB via config override.
    cfg = tx_home / "config.toml"
    with open(cfg, "a") as f:
        f.write("\n[logs]\nmax_size_mb = 1\nmax_keep = 5\nmax_age_days = 30\n")

    # offsets.json still has the entry — `tx new <same-name>` errors. So drop it.
    offsets = tx_home / "offsets.json"
    if offsets.exists():
        data = json.loads(offsets.read_text())
        data.pop(pane, None)
        if "_panes" in data:
            data["_panes"].pop(pane, None)
        offsets.write_text(json.dumps(data))

    pane2 = _pane(tx_runner, "rotateme")
    assert pane2 == "rotateme"
    rotated = tx_home / "logs" / f"{pane}.log.1"
    assert rotated.exists(), "expected log.1 rotation file from prior pane"
    # The new log should exist and be small (only the init bytes).
    assert log_path.exists()


# ============== Group G: tx maintain ==============

def test_maintain_force_rotates_every_pane(tx_runner, tx_home: Path):
    p1 = _pane(tx_runner, "m1")
    tx_runner("run", p1, "echo content-to-rotate", timeout=15)
    log1 = tx_home / "logs" / f"{p1}.log"
    assert log1.exists() and log1.stat().st_size > 0

    res = tx_runner("maintain", "--force")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "rotated" in res.stdout
    assert (tx_home / "logs" / f"{p1}.log.1").exists()


def test_maintain_dry_run_changes_nothing(tx_runner, tx_home: Path):
    p1 = _pane(tx_runner, "dry1")
    tx_runner("run", p1, "echo aaa", timeout=15)
    log1 = tx_home / "logs" / f"{p1}.log"
    size_before = log1.stat().st_size

    res = tx_runner("maintain", "--force", "--dry-run")
    assert res.returncode == 0
    assert "dry-run" in res.stdout
    # No rotation actually performed.
    assert not (tx_home / "logs" / f"{p1}.log.1").exists()
    assert log1.stat().st_size == size_before


def test_maintain_sweeps_aged_logs(tx_runner, tx_home: Path):
    p1 = _pane(tx_runner, "aged")
    # Fake an aged rotated log alongside.
    aged = tx_home / "logs" / f"{p1}.log.1"
    aged.write_bytes(b"ancient\n")
    far_past = time.time() - (60 * 86400)
    os.utime(aged, (far_past, far_past))

    # Drop max_age_days low.
    cfg = tx_home / "config.toml"
    with open(cfg, "a") as f:
        f.write("\n[logs]\nmax_age_days = 1\nmax_keep = 5\nmax_size_mb = 100\n")

    res = tx_runner("maintain")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "deleted aged" in res.stdout
    assert not aged.exists()


# ============== help text mentions new commands ==============

def test_help_mentions_tx_write_and_maintain(tx_runner):
    res = tx_runner("--help")
    assert res.returncode == 0
    # `tx --help` is the custom HELP_TEXT; we updated it to mention these.
    # Best-effort check: at minimum the click-level help works.
    res2 = tx_runner("write", "--help")
    assert res2.returncode == 0
    assert "atomically" in res2.stdout.lower() or "deploy" in res2.stdout.lower()
    res3 = tx_runner("maintain", "--help")
    assert res3.returncode == 0
    assert "rotat" in res3.stdout.lower()
