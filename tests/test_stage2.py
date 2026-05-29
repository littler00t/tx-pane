"""Integration tests for Stage 2 commands and behaviours (v0.3.0)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

import pytest


def _pane(tx_runner, *args: str) -> str:
    res = tx_runner("new", *args)
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout.strip().splitlines()[-1].strip()


# ----- log-path / log -----

def test_log_path_prints_path(tx_runner, tx_home):
    pane = _pane(tx_runner)
    res = tx_runner("log-path", pane)
    assert res.returncode == 0
    expected = str(tx_home / "logs" / f"{pane}.log")
    assert res.stdout.strip() == expected


def test_log_tail_returns_last_lines(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "seq 1 5", timeout=15)
    # --tail returns the last N lines including prompt artifacts, so widen
    # the slice so the numeric output is captured.
    res = tx_runner("log", pane, "--tail", "10")
    assert res.returncode == 0
    assert "\n5\n" in ("\n" + res.stdout + "\n")
    assert "\n4\n" in ("\n" + res.stdout + "\n")


def test_log_head_returns_first_lines(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "seq 1 5", timeout=15)
    res = tx_runner("log", pane, "--head", "20")
    assert res.returncode == 0
    assert "1" in res.stdout.split()


def test_log_does_not_advance_tail_offset(tx_runner, tx_home):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo from-log-test", timeout=15)
    offsets_before = json.loads((tx_home / "offsets.json").read_text())
    before_off = offsets_before[pane].get("tail_offset")
    tx_runner("log", pane)
    offsets_after = json.loads((tx_home / "offsets.json").read_text())
    assert offsets_after[pane].get("tail_offset") == before_off


def test_log_since_run(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "echo anchor-line", timeout=15)
    anchor = res.stdout.strip().splitlines()[-1].strip()
    tx_runner("wait-run", pane, anchor, "--timeout", "10", timeout=15)
    tx_runner("run", pane, "echo after-anchor", timeout=15)
    res2 = tx_runner("log", pane, "--since-run", anchor)
    assert res2.returncode == 0
    assert "after-anchor" in res2.stdout
    assert "anchor-line" not in res2.stdout


# ----- output --last / --since-run -----

def test_output_last_returns_most_recent(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo first-out", timeout=15)
    tx_runner("run", pane, "echo second-out", timeout=15)
    res = tx_runner("output", pane, "--last")
    assert res.returncode == 0
    assert "second-out" in res.stdout
    assert "first-out" not in res.stdout


def test_output_since_run_returns_following(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "echo anchor", timeout=15)
    anchor = res.stdout.strip().splitlines()[-1].strip()
    tx_runner("wait-run", pane, anchor, "--timeout", "10", timeout=15)
    tx_runner("run", pane, "echo follow-1", timeout=15)
    tx_runner("run", pane, "echo follow-2", timeout=15)
    res2 = tx_runner("output", pane, "--since-run", anchor)
    assert res2.returncode == 0
    assert "follow-1" in res2.stdout
    assert "follow-2" in res2.stdout


def test_output_requires_one_selector(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("output", pane)
    assert res.returncode == 1
    assert "exactly one" in res.stdout


# ----- mark / bookmarks -----

def test_mark_saves_bookmark(tx_runner, tx_home):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo before-mark", timeout=15)
    res = tx_runner("mark", pane, "pre-stop")
    assert res.returncode == 0
    offsets = json.loads((tx_home / "offsets.json").read_text())
    assert "pre-stop" in offsets[pane].get("bookmarks", {})


def test_mark_invalid_name_rejected(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("mark", pane, "bad name")
    assert res.returncode == 1
    assert "invalid" in res.stdout


def test_tail_from_bookmark_returns_following(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo first", timeout=15)
    tx_runner("mark", pane, "pin")
    tx_runner("run", pane, "echo after-pin", timeout=15)
    res = tx_runner("tail", pane, "--from", "pin")
    assert res.returncode == 0
    assert "after-pin" in res.stdout


def test_dump_from_bookmark(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo before-d", timeout=15)
    tx_runner("mark", pane, "ds")
    tx_runner("run", pane, "echo after-d", timeout=15)
    res = tx_runner("dump", pane, "--from", "ds")
    assert res.returncode == 0
    assert "after-d" in res.stdout
    assert "before-d" not in res.stdout


def test_reset_to_bookmark_rewinds(tx_runner, tx_home):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo set-mark", timeout=15)
    tx_runner("mark", pane, "back")
    tx_runner("run", pane, "echo subsequent", timeout=15)
    res = tx_runner("reset", pane, "--to", "back")
    assert res.returncode == 0
    offsets = json.loads((tx_home / "offsets.json").read_text())
    bm = int(offsets[pane]["bookmarks"]["back"])
    assert int(offsets[pane]["tail_offset"]) == bm


def test_unknown_bookmark_errors(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("tail", pane, "--from", "nope")
    assert res.returncode == 1
    assert "bookmark 'nope' not found" in res.stdout


# ----- tail --all -----

def test_tail_all_drains_buffer(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "seq 1 30 ; echo END_OF_ALL_TEST", "--max", "5", timeout=15)
    res = tx_runner("tail", pane, "--all", "--max", "5")
    assert res.returncode == 0
    assert "[end of output]" in res.stdout


# ----- new --cwd -----

def test_new_cwd_starts_in_directory(tx_runner, tx_home, tmp_path):
    sub = tmp_path / "cwd-target"
    sub.mkdir()
    pane = _pane(tx_runner, "--cwd", str(sub))
    # The shell should have cd'd; verify with pwd via a run.
    res = tx_runner("run", pane, "pwd", timeout=15)
    # Resolve symlinks because macOS /private/var vs /var.
    assert os.path.realpath(str(sub)) in os.path.realpath(res.stdout)


def test_new_cwd_invalid_directory_rejected(tx_runner):
    res = tx_runner("new", "--cwd", "/nonexistent/here/please")
    assert res.returncode == 1
    assert "not a directory" in res.stdout


# ----- kill --signal -----

def test_kill_signal_kill(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("kill", pane, "--signal", "kill")
    assert res.returncode == 0
    assert "signal=kill" in res.stdout


def test_kill_signal_term_default(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("kill", pane)
    assert res.returncode == 0
    assert "signal=term" in res.stdout


def test_kill_signal_invalid(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("kill", pane, "--signal", "bogus")
    assert res.returncode != 0


# ----- handoff / resume -----

def test_handoff_refuses_subsequent_run(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("handoff", pane)
    assert res.returncode == 0, res.stdout + res.stderr
    res2 = tx_runner("run", pane, "echo blocked", timeout=15)
    assert res2.returncode == 1
    assert "paused" in res2.stdout


def test_resume_restores_control(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("handoff", pane)
    res = tx_runner("resume", pane)
    assert res.returncode == 0
    # After resume, run should work again.
    res2 = tx_runner("run", pane, "echo post-resume", timeout=15)
    assert res2.returncode == 0
    assert "post-resume" in res2.stdout


def test_resume_without_handoff_errors(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("resume", pane)
    assert res.returncode == 1
    assert "not paused" in res.stdout


def test_handoff_paused_visible_in_status(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("handoff", pane)
    res = tx_runner("status", pane)
    assert res.returncode == 0
    assert "status=paused" in res.stdout


# ----- send-secret -----

def test_send_secret_redacts_in_log(tx_runner, tx_home):
    pane = _pane(tx_runner)
    # Start a `read` command so the pane has a stdin consumer.
    tx_runner("exec", pane, "read SECRET_VAR", timeout=15)
    # Give the read command a moment to actually be waiting on stdin.
    time.sleep(0.3)
    # Use the tx_runner binding so TX_PANE_HOME and TMUX_TMPDIR are correct.
    # We need a custom invocation that pipes stdin in.
    import os as _os
    env = {**_os.environ, "TX_PANE_HOME": str(tx_home)}
    # Inherit TMUX_TMPDIR from the runner's env. Simplest: spawn under bash.
    # Hack: peek at the tx_runner's closure to grab env. Not pretty; just
    # re-derive via the runner's first call by checking marker on log.
    # Instead, use Popen with --enter so the read completes.
    from conftest import TX_SCRIPT
    # Grab tx_home-aware env from the tx_runner closure (it's stored on .__closure__).
    closure = tx_runner.__closure__
    tx_env = None
    for cell in closure or []:
        val = cell.cell_contents
        if isinstance(val, dict) and "TX_PANE_HOME" in val:
            tx_env = val
            break
    assert tx_env is not None, "could not extract tx_runner env"
    proc = subprocess.run(
        [str(TX_SCRIPT), "send-secret", pane, "--enter"],
        input="hunter2\n",
        env=tx_env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "redacted" in proc.stdout
    # Log should contain redacted placeholder, not 'hunter2'.
    log_text = (tx_home / "logs" / f"{pane}.log").read_text(errors="replace")
    assert "hunter2" not in log_text
    assert "[redacted: send-secret" in log_text


def test_send_secret_requires_stdin(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("send-secret", pane, timeout=5)
    # When called via subprocess.run with capture_output=True, stdin is a
    # closed/empty pipe (not a tty). The code reads empty stdin and errors.
    assert res.returncode == 1
    assert ("stdin" in res.stdout) or ("empty" in res.stdout)


# ----- info -----

def test_info_emits_expected_fields(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("info", pane)
    assert res.returncode == 0, res.stdout + res.stderr
    for key in ("pane:", "state:", "shell:", "log_path:", "buffer_bytes:", "tail_offset:"):
        assert key in res.stdout
    assert f"pane:" in res.stdout
    # `created` only appears when set via tx-pane new — should be present here.
    assert "created:" in res.stdout


def test_info_attached_field_present(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("info", pane)
    assert "user_attached:" in res.stdout


def test_status_includes_attached_field(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("status", pane)
    assert "attached=" in res.stdout


# ----- --on-timeout -----

def test_on_timeout_cancel_recovers(tx_runner):
    pane = _pane(tx_runner)
    # Run a 10s sleep with 1s timeout + --on-timeout cancel.
    res = tx_runner(
        "run", pane, "sleep 10", "--timeout", "1", "--on-timeout", "cancel", timeout=20
    )
    # Cancel should produce an [exit:N] line — exit 130 typical for sigint.
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[exit:" in res.stdout
    assert "cancelled" in res.stdout


def test_on_timeout_report_default(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner(
        "exec", pane, "sleep 10", timeout=10
    )
    rid = res.stdout.strip().splitlines()[-1].strip()
    res2 = tx_runner("wait-run", pane, rid, "--timeout", "1", timeout=10)
    # Default = report; output should be a timeout, exit 0 from tx-pane, run still active
    assert res2.returncode == 0
    assert "[timeout:" in res2.stdout
    # Clean up the lingering run.
    tx_runner("kill-run", pane, rid)
