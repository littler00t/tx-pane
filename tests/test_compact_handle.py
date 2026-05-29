"""End-to-end tests for the L4 handle protocol.

Verifies the reversibility contract: when L4 elides content, the agent
can retrieve the full or sliced original via `tx output --handle`.

Drives real `tx run` / `tx output` invocations against an isolated
tmux server via the existing test fixtures.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest


def _pane(tx_runner) -> str:
    res = tx_runner("new", timeout=15)
    assert res.returncode == 0, res.stderr or res.stdout
    return res.stdout.strip().splitlines()[-1].strip()


def _last_run_id(tx_runner, pane: str) -> str:
    """Get the most recent completed run id for a pane."""
    res = tx_runner("runs", pane, timeout=15)
    assert res.returncode == 0
    # Find the last line that looks like 'r-XXXX'
    for line in reversed(res.stdout.splitlines()):
        m = re.search(r"\br-[0-9a-f]+\b", line)
        if m:
            return m.group(0)
    raise AssertionError(f"no run-id in:\n{res.stdout}")


def _extract_handle(text: str) -> str | None:
    m = re.search(r"handle=(h-[0-9a-f]+)", text)
    return m.group(1) if m else None


# Test fixture: a shell snippet that produces N lines whose first words
# vary enough that L3 RLE near-identical fingerprinting won't collapse
# them. ``awk`` builds the output in one process (much faster than a
# bash loop spawning a binary per line).
def _diverse_lines_cmd(n: int) -> str:
    return (
        f"awk 'BEGIN {{ words = \"alpha bravo charlie delta echo foxtrot golf "
        f"hotel india juliet\"; split(words, w, \" \"); "
        f"for (i = 1; i <= {n}; i++) {{ "
        f"print w[((i-1) % 10) + 1] \" \" w[((i*3) % 10) + 1] \" id-\" i \" payload\" }} }}'"
    )


# ---------------------------------------------------------------------
# Handle creation when L4 elides
# ---------------------------------------------------------------------

def test_terse_with_tiny_budget_emits_handle(tx_runner):
    pane = _pane(tx_runner)
    # Generate ~500 lines of unique content (defeats L3 RLE) so L4 must elide.
    cmd = _diverse_lines_cmd(500)
    res = tx_runner(
        "run", "--terse", "--token-budget", "60", "--max", "10000",
        pane, cmd, timeout=30,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    handle = _extract_handle(res.stdout)
    assert handle is not None, f"no handle in:\n{res.stdout}"
    # Marker line is present
    assert "tx:elided" in res.stdout
    # Tail keeps the last lines of the awk output
    assert "id-500 payload" in res.stdout
    # The middle is elided
    assert "id-250 payload" not in res.stdout
    # The compaction footer shows L4 fired
    assert "L4" in res.stdout


def test_no_handle_when_under_budget(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("run", "--terse", pane, "echo small output", timeout=15)
    assert res.returncode == 0
    assert _extract_handle(res.stdout) is None


def test_no_handle_in_raw_mode(tx_runner):
    pane = _pane(tx_runner)
    cmd = _diverse_lines_cmd(500)
    res = tx_runner("run", "--raw", "--token-budget", "60", "--max", "10000",
                    pane, cmd, timeout=30)
    assert res.returncode == 0
    assert _extract_handle(res.stdout) is None
    # Raw → all 500 ids present
    assert "id-1 payload" in res.stdout
    assert "id-250 payload" in res.stdout
    assert "id-500 payload" in res.stdout


# ---------------------------------------------------------------------
# Handle resolution via tx output
# ---------------------------------------------------------------------

def test_output_handle_range_returns_slice(tx_runner):
    pane = _pane(tx_runner)
    cmd = _diverse_lines_cmd(500)
    res = tx_runner(
        "run", "--terse", "--token-budget", "60", "--max", "10000",
        pane, cmd, timeout=30,
    )
    handle = _extract_handle(res.stdout)
    assert handle is not None

    # Pull a middle slice.
    res2 = tx_runner(
        "output", pane,
        "--handle", handle,
        "--range", "100-110",
        "--max", "10000",
        timeout=15,
    )
    assert res2.returncode == 0, res2.stderr or res2.stdout
    body = res2.stdout
    # Some line ids in the 100-110 vicinity should be present
    found = sum(1 for i in range(90, 130) if f"id-{i} payload" in body)
    assert found >= 3, f"expected several id-N in 90..130 in body:\n{body}"
    # Distant ids should not be present
    assert "id-5 payload" not in body
    assert "id-400 payload" not in body


def test_output_handle_grep(tx_runner):
    pane = _pane(tx_runner)
    cmd = _diverse_lines_cmd(500) + "; echo MARKER_LINE_X"
    res = tx_runner(
        "run", "--terse", "--token-budget", "60", "--max", "10000",
        pane, cmd, timeout=30,
    )
    handle = _extract_handle(res.stdout)
    assert handle is not None

    res2 = tx_runner(
        "output", pane,
        "--handle", handle,
        "--grep", "id-250 ",
        "--max", "10000",
        timeout=15,
    )
    assert res2.returncode == 0
    assert "id-250 payload" in res2.stdout


def test_output_handle_full_returns_everything(tx_runner):
    pane = _pane(tx_runner)
    cmd = _diverse_lines_cmd(500)
    res = tx_runner(
        "run", "--terse", "--token-budget", "60", "--max", "10000",
        pane, cmd, timeout=30,
    )
    handle = _extract_handle(res.stdout)
    assert handle is not None

    res2 = tx_runner(
        "output", pane,
        "--handle", handle,
        "--full",
        "--max", "10000",
        timeout=15,
    )
    assert res2.returncode == 0
    # The full body should include the middle that was elided
    assert "id-250 payload" in res2.stdout
    # And no compaction footer
    assert "tx:elided" not in res2.stdout


def test_output_handle_expired_errors_clearly(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner(
        "output", pane,
        "--handle", "h-deadbeef",
        timeout=15,
    )
    assert res.returncode != 0
    assert "not found" in res.stdout or "not found" in res.stderr


# ---------------------------------------------------------------------
# TX_NO_COMPACT baseline regression
# ---------------------------------------------------------------------

def test_tx_no_compact_kills_handle_emission(tx_runner):
    pane = _pane(tx_runner)
    tx_runner.env["TX_NO_COMPACT"] = "1"
    try:
        cmd = _diverse_lines_cmd(500)
        res = tx_runner("run", "--terse", "--token-budget", "60",
                        "--max", "10000",
                        pane, cmd, timeout=30)
    finally:
        tx_runner.env.pop("TX_NO_COMPACT", None)
    assert res.returncode == 0
    # No handle emitted under the kill switch
    assert _extract_handle(res.stdout) is None
    # All 500 ids should be present (output verbatim)
    assert "id-1 payload" in res.stdout
    assert "id-250 payload" in res.stdout
    assert "id-500 payload" in res.stdout


# ---------------------------------------------------------------------
# --range / --grep without --handle (works on any run-id)
# ---------------------------------------------------------------------

def test_output_range_without_handle(tx_runner):
    pane = _pane(tx_runner)
    cmd = "for i in $(seq 1 50); do echo \"line-$i\"; done"
    res = tx_runner("run", "--raw", pane, cmd, timeout=15)
    assert res.returncode == 0
    run_id = _last_run_id(tx_runner, pane)
    res2 = tx_runner("output", "--raw", pane, run_id, "--range", "10-15", timeout=15)
    assert res2.returncode == 0, res2.stderr or res2.stdout
    # Lines 10..15 (0-based) of the captured kept-list. The echoed
    # wrap-command line is line 0, so item content is offset by 1.
    body = res2.stdout
    # Some of the expected content range should be present
    found = sum(1 for i in range(8, 18) if f"line-{i}" in body)
    assert found >= 3, f"expected several line-N hits in:\n{body}"


def test_output_grep_without_handle(tx_runner):
    pane = _pane(tx_runner)
    cmd = "for i in $(seq 1 50); do echo \"line-$i\"; done; echo MATCHME"
    res = tx_runner("run", pane, cmd, timeout=15)
    assert res.returncode == 0
    run_id = _last_run_id(tx_runner, pane)
    res2 = tx_runner("output", pane, run_id, "--grep", "MATCHME", timeout=15)
    assert res2.returncode == 0
    assert "MATCHME" in res2.stdout


def test_output_grep_no_match_returns_marker(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("run", pane, "echo hello", timeout=15)
    assert res.returncode == 0
    run_id = _last_run_id(tx_runner, pane)
    res2 = tx_runner("output", pane, run_id, "--grep", "ZZZZ_no_match",
                     timeout=15)
    assert res2.returncode == 0
    assert "no matches" in res2.stdout
