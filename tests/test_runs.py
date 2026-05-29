"""Integration tests for tx exec / wait-run / output / runs / kill-run / status."""

from __future__ import annotations

import json
import time

import pytest


def _pane(tx_runner):
    res = tx_runner("new")
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout.strip().splitlines()[-1].strip()


def test_exec_returns_run_id(tx_runner, tx_home):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "echo immediate", timeout=15)
    assert res.returncode == 0, res.stdout + res.stderr
    run_id = res.stdout.strip().splitlines()[-1].strip()
    assert run_id.startswith("r-")
    offsets = json.loads((tx_home / "offsets.json").read_text())
    # active_run was set; may or may not be cleared by the time we check
    # (depends on whether finalize ran on a subsequent invocation).
    assert "active_run" in offsets[pane] or any(
        r["id"] == run_id for r in offsets[pane].get("runs", [])
    )


def test_wait_run_completes(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "echo wait-target", timeout=15)
    run_id = res.stdout.strip().splitlines()[-1].strip()
    res2 = tx_runner("wait-run", pane, run_id, "--timeout", "10", timeout=15)
    assert res2.returncode == 0
    assert "[exit:0]" in res2.stdout
    assert "wait-target" in res2.stdout


def test_wait_run_returns_cached_when_already_done(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("run", pane, "echo first", timeout=15)
    assert res.returncode == 0
    # Pull the run id from `tx runs`.
    res2 = tx_runner("runs", pane)
    assert res2.returncode == 0
    run_id = [
        line.split()[0]
        for line in res2.stdout.splitlines()
        if line.startswith("r-")
    ][0]
    res3 = tx_runner("wait-run", pane, run_id, timeout=10)
    assert res3.returncode == 0
    assert "[exit:0]" in res3.stdout
    assert "first" in res3.stdout


def test_wait_run_unknown_errors(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("wait-run", pane, "r-deadbe", "--timeout", "1")
    assert res.returncode == 1
    assert "not found" in res.stdout


def test_output_returns_slice(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo output-target", timeout=15)
    res = tx_runner("runs", pane)
    run_id = [
        line.split()[0]
        for line in res.stdout.splitlines()
        if line.startswith("r-")
    ][0]
    res2 = tx_runner("output", pane, run_id)
    assert res2.returncode == 0
    assert "output-target" in res2.stdout
    assert "[exit:0]" in res2.stdout


def test_output_active_run_errors(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "sleep 5", timeout=10)
    run_id = res.stdout.strip().splitlines()[-1].strip()
    res2 = tx_runner("output", pane, run_id)
    assert res2.returncode == 1
    assert "still active" in res2.stdout
    # Clean up: wait for it to finish.
    tx_runner("wait-run", pane, run_id, "--timeout", "10", timeout=15)


def test_runs_lists_active_and_completed(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo done", timeout=15)
    tx_runner("run", pane, "echo done2", timeout=15)
    res = tx_runner("runs", pane)
    assert res.returncode == 0
    # Header + at least 2 rows
    body_lines = [l for l in res.stdout.splitlines() if l.startswith("r-")]
    assert len(body_lines) >= 2


def test_kill_run_interrupts(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "sleep 10", timeout=10)
    run_id = res.stdout.strip().splitlines()[-1].strip()
    time.sleep(0.3)
    res2 = tx_runner("kill-run", pane, run_id, timeout=10)
    assert res2.returncode == 0
    # Either the marker was observed (most likely) or we got the "no marker" notice.
    assert "killed:" in res2.stdout


def test_kill_run_not_active(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo done", timeout=15)
    res = tx_runner("runs", pane)
    run_id = [
        line.split()[0]
        for line in res.stdout.splitlines()
        if line.startswith("r-")
    ][0]
    res2 = tx_runner("kill-run", pane, run_id)
    assert res2.returncode == 1
    assert "not active" in res2.stdout


def test_status_idle_then_running(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("status", pane)
    assert res.returncode == 0
    assert "status=idle" in res.stdout
    # Now start a long run.
    res2 = tx_runner("exec", pane, "sleep 5", timeout=10)
    run_id = res2.stdout.strip().splitlines()[-1].strip()
    time.sleep(0.3)
    res3 = tx_runner("status", pane)
    assert res3.returncode == 0
    assert "status=running" in res3.stdout
    assert run_id in res3.stdout
    tx_runner("kill-run", pane, run_id)
