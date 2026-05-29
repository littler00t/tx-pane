"""Tests for the nested-shell fallback path and tx hook-install.

These simulate the SSH/sudo-i/docker-exec scenario by uninstalling the
PROMPT_COMMAND/precmd hook from within the pane after creation, then
verifying:
1. tx run falls back to prompt-pattern detection and emits [exit:?].
2. tx hook-install re-wires the hook and subsequent runs get real exits.
"""

from __future__ import annotations

import time


def _pane(tx_runner):
    res = tx_runner("new")
    assert res.returncode == 0
    return res.stdout.strip().splitlines()[-1].strip()


def _enable_prompt_fallback(tx_home):
    """Rewrite the test config so wait_for_marker's prompt fallback is active.

    The conftest defaults set prompt_patterns = [] (silence mode); for these
    tests we want patterns that match common shells.
    """
    cfg = (tx_home / "config.toml").read_text()
    cfg = cfg.replace(
        "prompt_patterns = []",
        'prompt_patterns = ["[$%#>] *$"]',
    )
    (tx_home / "config.toml").write_text(cfg)


def _disable_hook(tx_runner, pane):
    """Strip the v2 marker hook from the pane's current shell, simulating a
    nested shell (ssh/sudo -i/docker exec) that never received the install.
    """
    tx_runner(
        "send", pane,
        "unset PROMPT_COMMAND; unset -f precmd 2>/dev/null; "
        "precmd_functions=() 2>/dev/null; true"
    )
    tx_runner("key", pane, "Enter")
    time.sleep(0.5)


def test_nested_shell_fallback_emits_unknown_exit(tx_runner, tx_home):
    _enable_prompt_fallback(tx_home)
    pane = _pane(tx_runner)
    _disable_hook(tx_runner, pane)

    res = tx_runner("run", pane, "echo nested-target", timeout=15)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "nested-target" in res.stdout
    assert "[exit:?]" in res.stdout
    assert "hook-missing" in res.stdout
    assert "tx hook-install" in res.stdout


def test_hook_install_restores_real_exit_codes(tx_runner, tx_home):
    _enable_prompt_fallback(tx_home)
    pane = _pane(tx_runner)
    _disable_hook(tx_runner, pane)

    # First run shows the fallback path (sanity check).
    res = tx_runner("run", pane, "echo before-install", timeout=15)
    assert "[exit:?]" in res.stdout

    # Now re-install the hook explicitly.
    res2 = tx_runner("hook-install", pane, timeout=15)
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert "installed:" in res2.stdout

    # Subsequent runs should produce a real marker again.
    res3 = tx_runner("run", pane, "echo after-install", timeout=15)
    assert res3.returncode == 0
    assert "after-install" in res3.stdout
    assert "[exit:0]" in res3.stdout
    assert "[exit:?]" not in res3.stdout
    assert "hook-missing" not in res3.stdout


def test_hook_install_refuses_on_busy_pane(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "sleep 5", timeout=10)
    run_id = res.stdout.strip().splitlines()[-1].strip()
    time.sleep(0.3)
    res2 = tx_runner("hook-install", pane, timeout=10)
    assert res2.returncode == 1
    assert "requires an idle pane" in res2.stdout
    tx_runner("kill-run", pane, run_id)
