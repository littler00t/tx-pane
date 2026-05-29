"""End-to-end regression tests for the compaction stage.

Compaction ships default-on in terse mode. The invariants below verify
that default calls compact, --raw preserves the pre-compaction baseline,
and the TX_PANE_NO_COMPACT env-var escape hatch works at the subprocess boundary.

Plug into the existing pytest+tmux infrastructure in conftest.py.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest


def _pane_id_from(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip().splitlines()[-1].strip()


def _strip_exit_line(out: str) -> str:
    """Drop the `[exit:N]` header that cmd_run prepends — it's
    structural, not part of the run's body output."""
    lines = out.splitlines()
    if lines and lines[0].startswith("[exit:"):
        lines = lines[1:]
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Default behavior is terse
# ---------------------------------------------------------------------

def test_default_run_compacts_simple_echo(tx_runner):
    """Without flags, `tx-pane run` uses the terse default."""
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner("run", pane, "echo hello-world", timeout=15)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "hello-world" in res.stdout
    assert "[tx-pane:degraded" not in res.stdout


def test_default_run_strips_banner(tx_runner):
    """Without flags, banner lines are compacted by default."""
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner(
        "run", pane,
        "printf 'Reading package lists... Done\\nfoo\\n'",
        timeout=15,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Reading package lists... Done" not in _body_lines(res.stdout)
    assert "foo" in res.stdout


# ---------------------------------------------------------------------
# --terse activates L1+L2
# ---------------------------------------------------------------------

def test_terse_strips_apt_banner(tx_runner):
    """L1 strips the banner *output line*, not the literal characters
    inside the echoed wrap-command. The shell echoes the typed command,
    which contains 'Reading package lists' as printf args; we check
    that the banner doesn't appear as a standalone output line."""
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner(
        "run", "--terse", pane,
        "printf 'Reading package lists... Done\\nReal content here\\n'",
        timeout=15,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    body_lines = [l for l in res.stdout.splitlines()
                  if not l.startswith("___tx_run_id=")
                  and not l.startswith("[exit:")]
    # L1 stripped the banner output line
    assert "Reading package lists... Done" not in body_lines
    # Body is preserved
    assert "Real content here" in res.stdout


def test_terse_collapses_blank_runs(tx_runner):
    pane = _pane_id_from(tx_runner("new"))
    # printf with multiple blank lines between content
    res = tx_runner(
        "run", "--terse", pane,
        "printf 'a\\n\\n\\n\\n\\nb\\n'",
        timeout=15,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    body = _strip_exit_line(res.stdout)
    # Should not contain 3+ consecutive newlines in the body
    assert "\n\n\n" not in body, f"L2 did not tighten blank runs: {body!r}"
    assert "a" in body and "b" in body


# ---------------------------------------------------------------------
# --raw forces baseline behavior
# ---------------------------------------------------------------------

def _body_lines(stdout: str) -> list[str]:
    return [l for l in stdout.splitlines()
            if not l.startswith("___tx_run_id=")
            and not l.startswith("[exit:")]


def test_raw_flag_preserves_banner(tx_runner):
    """--raw bypasses every compaction layer (escape hatch)."""
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner(
        "run", "--raw", pane,
        "printf 'Reading package lists... Done\\nfoo\\n'",
        timeout=15,
    )
    assert res.returncode == 0
    assert "Reading package lists... Done" in _body_lines(res.stdout)


def test_raw_beats_terse_when_both_given(tx_runner):
    """`--raw --terse` resolves to raw (escape hatch wins)."""
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner(
        "run", "--raw", "--terse", pane,
        "printf 'Reading package lists... Done\\nfoo\\n'",
        timeout=15,
    )
    assert res.returncode == 0
    # Banner survives in the body because --raw wins
    assert "Reading package lists... Done" in _body_lines(res.stdout)


# ---------------------------------------------------------------------
# TX_PANE_NO_COMPACT=1 env var is the global kill switch
# ---------------------------------------------------------------------

def test_tx_no_compact_env_disables_terse(tx_runner):
    pane = _pane_id_from(tx_runner("new"))
    tx_runner.env["TX_PANE_NO_COMPACT"] = "1"
    try:
        res = tx_runner(
            "run", "--terse", pane,
            "printf 'Reading package lists... Done\\nfoo\\n'",
            timeout=15,
        )
    finally:
        tx_runner.env.pop("TX_PANE_NO_COMPACT", None)
    assert res.returncode == 0
    # Even with --terse, env-var kill switch keeps the banner output line
    assert "Reading package lists... Done" in _body_lines(res.stdout)


# ---------------------------------------------------------------------
# Compaction flags appear in --help (P1 surface contract)
# ---------------------------------------------------------------------

def test_output_command_help_shows_compaction_flags(tx_runner):
    commands = ("run", "output", "wait-run", "stream", "tail", "dump", "wait", "log", "grep")
    flags = (
        "--raw", "--terse", "--token-budget",
        "--no-strip-banners", "--no-collapse-repeats", "--no-normalize",
    )
    for command in commands:
        res = tx_runner(command, "--help", timeout=15)
        assert res.returncode == 0
        for flag in flags:
            assert flag in res.stdout, f"{flag} missing from `tx-pane {command} --help`"


def test_wait_run_default_compacts_and_raw_preserves_banner(tx_runner):
    pane = _pane_id_from(tx_runner("new"))

    res = tx_runner(
        "exec", pane,
        "printf 'Reading package lists... Done\\nwait-run-content\\n'",
        timeout=15,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    run_id = res.stdout.strip()
    compacted = tx_runner("wait-run", pane, run_id, timeout=15)
    assert compacted.returncode == 0, compacted.stdout + compacted.stderr
    assert "wait-run-content" in compacted.stdout
    assert "Reading package lists... Done" not in _body_lines(compacted.stdout)

    res2 = tx_runner(
        "exec", pane,
        "printf 'Reading package lists... Done\\nwait-run-raw\\n'",
        timeout=15,
    )
    assert res2.returncode == 0, res2.stdout + res2.stderr
    raw_run_id = res2.stdout.strip()
    raw = tx_runner("wait-run", "--raw", pane, raw_run_id, timeout=15)
    assert raw.returncode == 0, raw.stdout + raw.stderr
    assert "Reading package lists... Done" in _body_lines(raw.stdout)


# ---------------------------------------------------------------------
# No regressions on the v2 marker pathway with --terse
# ---------------------------------------------------------------------

def test_terse_preserves_exit_code(tx_runner):
    """--terse must not eat the exit code header."""
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner("run", "--terse", pane, "false", timeout=15)
    assert res.returncode == 0
    assert "[exit:1]" in res.stdout, res.stdout


def test_terse_body_for_simple_echo(tx_runner):
    pane = _pane_id_from(tx_runner("new"))
    res = tx_runner("run", "--terse", pane, "echo terse-body", timeout=15)
    assert res.returncode == 0
    assert "terse-body" in res.stdout
