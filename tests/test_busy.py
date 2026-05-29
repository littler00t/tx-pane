"""Integration tests for refuse-on-busy and the resolution flags."""

from __future__ import annotations

import time


def _pane(tx_runner):
    res = tx_runner("new")
    assert res.returncode == 0
    return res.stdout.strip().splitlines()[-1].strip()


def test_refuse_on_busy(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "sleep 5", timeout=10)
    run_id = res.stdout.strip().splitlines()[-1].strip()
    time.sleep(0.3)
    res2 = tx_runner("run", pane, "echo blocked", timeout=10)
    assert res2.returncode == 1
    assert "busy" in res2.stdout
    assert "--queue" in res2.stdout
    assert "--stdin" in res2.stdout
    assert "--kill-and-run" in res2.stdout
    tx_runner("kill-run", pane, run_id)


def test_queue_blocks_then_runs(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "sleep 1", timeout=10)
    run_id = res.stdout.strip().splitlines()[-1].strip()
    time.sleep(0.2)
    res2 = tx_runner("run", "--queue", pane, "echo queued-ok", timeout=15)
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert "queued-ok" in res2.stdout
    assert "[exit:0]" in res2.stdout


def test_kill_and_run_interrupts(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "sleep 10", timeout=10)
    run_id = res.stdout.strip().splitlines()[-1].strip()
    time.sleep(0.3)
    res2 = tx_runner("run", "--kill-and-run", pane, "echo killed-ok", timeout=15)
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert "killed-ok" in res2.stdout


def test_stdin_refuses_idle_pane(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("run", "--stdin", pane, "y", timeout=10)
    assert res.returncode == 1
    assert "idle" in res.stdout


def test_stdin_feeds_running_command(tx_runner):
    pane = _pane(tx_runner)
    # `read` waits for stdin; --stdin feeds it.
    res = tx_runner(
        "exec",
        pane,
        "read REPLY && echo got=$REPLY",
        timeout=10,
    )
    run_id = res.stdout.strip().splitlines()[-1].strip()
    time.sleep(0.3)
    res2 = tx_runner("run", "--stdin", pane, "hello", timeout=10)
    assert res2.returncode == 0
    res3 = tx_runner("wait-run", pane, run_id, "--timeout", "5", timeout=10)
    assert res3.returncode == 0
    assert "got=hello" in res3.stdout


def test_run_wait_for_leaves_sigint_ignoring_run_active(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner(
        "run", pane,
        "trap '' INT; echo READY; while true; do sleep 1; done",
        "--wait-for", "READY",
        "--timeout", "8",
        timeout=15,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "cancellation-pending" in res.stdout
    assert "[exit:" not in res.stdout

    status = tx_runner("status", pane, timeout=10)
    assert status.returncode == 0
    assert "status=running" in status.stdout
    assert "active=" in status.stdout

    blocked = tx_runner("run", pane, "echo should-not-run", timeout=10)
    assert blocked.returncode == 1
    assert "busy" in blocked.stdout
    tx_runner("kill", pane, timeout=10)


def test_stream_bound_leaves_sigint_ignoring_run_active(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner(
        "stream", pane,
        "trap '' INT; echo READY; while true; do sleep 1; done",
        "--until", "READY",
        "--timeout", "8",
        timeout=15,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "cancellation-pending" in res.stdout
    assert "[exit:" not in res.stdout

    status = tx_runner("status", pane, timeout=10)
    assert status.returncode == 0
    assert "status=running" in status.stdout
    assert "active=" in status.stdout

    blocked = tx_runner("run", pane, "echo should-not-run", timeout=10)
    assert blocked.returncode == 1
    assert "busy" in blocked.stdout
    tx_runner("kill", pane, timeout=10)
