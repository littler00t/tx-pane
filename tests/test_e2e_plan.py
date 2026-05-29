"""End-to-end QA plan — gap-fill (tx_e2e_testplan1.md).

The plan in `homeserver/tx_e2e_testplan1.md` has 55 numbered cases. 44 are
already covered elsewhere in tests/; this file closes the 10 that weren't:

  T-2.3 — --queue --max-wait bounds the wait
  T-2.7 — concurrent tx run from two processes
  T-3.1 — fresh pane is idle
  T-3.3 — pane state is `tui` while a TUI owns the alt-screen
  T-4.3 — two-level nested-shell hook handling
  T-4.4 — returning from a nested shell preserves the outer hook
  T-5.3 — kill-run + recover (follow-up command works on the same pane)
  T-5.4 — dead-shell recovery via tx kill + tx new
  T-5.5 — marker wrapper survives pipefail / set -e style pipelines
  T-10.2 — multiple runs share pane shell state (env var carry-over)

Function names use `test_T_X_Y_<slug>` so a failure points straight at the
plan row.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest


def _pane(tx_runner, *args: str) -> str:
    res = tx_runner("new", *args)
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout.strip().splitlines()[-1].strip()


def _enable_prompt_fallback(tx_home: Path) -> None:
    """Switch the test config from silence-mode to prompt-pattern mode so
    `wait_for_marker`'s fallback can fire (needed when a nested shell has
    no marker hook). Mirrors `tests/test_nested_hook.py:_enable_prompt_fallback`.
    """
    cfg_path = tx_home / "config.toml"
    cfg = cfg_path.read_text()
    cfg = cfg.replace("prompt_patterns = []", 'prompt_patterns = ["[$%#>] *$"]')
    cfg_path.write_text(cfg)


# ============== T-2.3 — --queue --max-wait ==============

def test_T_2_3_queue_max_wait_bounds_the_wait(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "sleep 30", timeout=10)
    run_id = res.stdout.strip().splitlines()[-1].strip()
    time.sleep(0.3)
    t0 = time.monotonic()
    res2 = tx_runner(
        "run", "--queue", "--max-wait", "2", pane, "echo too-slow", timeout=15
    )
    elapsed = time.monotonic() - t0
    # Bounded: returns ~2s after sending, well under the 30s sleep.
    assert elapsed < 8.0, f"--max-wait did not bound the wait (took {elapsed:.1f}s)"
    assert res2.returncode == 1, res2.stdout
    assert "--queue" in res2.stdout or "queue" in res2.stdout
    assert "timed out" in res2.stdout or "max-wait" in res2.stdout
    # The original sleep run is still active.
    status_after = tx_runner("status", pane)
    assert run_id in status_after.stdout or "running" in status_after.stdout
    # Cleanup.
    tx_runner("kill-run", pane, run_id)


# ============== T-2.7 — concurrent tx run from two processes ==============

def test_T_2_7_concurrent_tx_run_serialises(tx_runner):
    """Two simultaneous `tx run` calls against the same idle pane:
    one wins, the other refuses with the documented busy error. This
    proves the fcntl.flock on offsets.json serialises the state check
    across separate tx processes."""
    pane = _pane(tx_runner)
    env = tx_runner.env  # exposed by conftest
    tx_script = tx_runner.tx_script

    # Process A: 2-second sleep (so the second process has a window).
    proc_a = subprocess.Popen(
        [tx_script, "run", pane, "echo a; sleep 2"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # Process B: a brief stagger so A is the one that grabs the lock first.
    time.sleep(0.3)
    proc_b = subprocess.Popen(
        [tx_script, "run", pane, "echo b"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    out_a, _err_a = proc_a.communicate(timeout=15)
    out_b, _err_b = proc_b.communicate(timeout=15)

    # Documented v1.0 behavior: refuse-on-busy. Exactly one of (A, B) wins
    # (exit 0 + [exit:0] in stdout); the other refuses with the busy error.
    winners = sum(p.returncode == 0 for p in (proc_a, proc_b))
    losers = sum(p.returncode != 0 for p in (proc_a, proc_b))
    assert winners == 1 and losers == 1, (
        f"expected one winner / one refusal; A.rc={proc_a.returncode} B.rc={proc_b.returncode}\n"
        f"A.stdout={out_a!r}\nB.stdout={out_b!r}"
    )
    loser_out = out_a if proc_a.returncode != 0 else out_b
    assert "busy" in loser_out, f"loser should report busy: {loser_out!r}"


# ============== T-3.1 — fresh pane is idle ==============

def test_T_3_1_fresh_pane_is_idle(tx_runner):
    pane = _pane(tx_runner)
    # Settle so any init noise has flushed and PROMPT_COMMAND has fired once.
    time.sleep(0.4)
    res = tx_runner("status", pane)
    assert res.returncode == 0, res.stdout
    assert "status=idle" in res.stdout, res.stdout


# ============== T-3.3 — pane state `tui` during alt-screen ==============

@pytest.mark.skipif(shutil.which("vim") is None, reason="vim not installed on this host")
def test_T_3_3_tui_during_alt_screen(tx_runner, tx_home: Path, tmp_path: Path):
    pane = _pane(tx_runner)
    target = tmp_path / "tx-tui-target"
    target.write_text("hello\n")

    # Launch vim via tx send (NOT tx run — there's no marker to wait for).
    tx_runner("send", pane, f"vim {target}")
    tx_runner("key", pane, "Enter")

    # Wait for tmux's alternate_on bit to flip; tx pane_state surfaces it as `tui`.
    deadline = time.monotonic() + 6.0
    saw_tui = False
    while time.monotonic() < deadline:
        s = tx_runner("status", pane).stdout
        if "status=tui" in s:
            saw_tui = True
            break
        time.sleep(0.2)
    assert saw_tui, "expected status=tui while vim owns the pane"

    # Quit vim cleanly. Escape exits any pending mode; then :q! + Enter quits.
    tx_runner("key", pane, "Escape")
    tx_runner("send", pane, ":q!")
    tx_runner("key", pane, "Enter")

    deadline = time.monotonic() + 6.0
    back_idle = False
    while time.monotonic() < deadline:
        s = tx_runner("status", pane).stdout
        if "status=idle" in s:
            back_idle = True
            break
        time.sleep(0.2)
    assert back_idle, "pane stayed in tui after :q!"


# ============== T-4.3 — two-level nested-shell hook handling ==============

def test_T_4_3_two_level_nesting(tx_runner, tx_home: Path):
    _enable_prompt_fallback(tx_home)
    pane = _pane(tx_runner)

    # --- Nest 1: launch a bare bash interactive shell via send/key
    # (--noprofile --norc avoids inheriting the host's PROMPT_COMMAND).
    tx_runner("send", pane, "bash --noprofile --norc -i")
    tx_runner("key", pane, "Enter")
    time.sleep(0.6)

    # Without the hook, a run falls back to prompt-pattern → [exit:?].
    r = tx_runner("run", pane, "echo nest-1-no-hook", "--timeout", "3", timeout=15)
    assert "[exit:?]" in r.stdout or "[timeout:" in r.stdout, r.stdout

    # Install hook in nest 1, then a real run works.
    r = tx_runner("hook-install", pane, timeout=15)
    assert r.returncode == 0 and "installed:" in r.stdout, r.stdout
    r = tx_runner("run", pane, "echo nest-1-with-hook", timeout=10)
    assert "[exit:0]" in r.stdout and "nest-1-with-hook" in r.stdout

    # --- Nest 2: another bash inside nest 1.
    tx_runner("send", pane, "bash --noprofile --norc -i")
    tx_runner("key", pane, "Enter")
    time.sleep(0.6)

    # Nest 2 has no hook again.
    r = tx_runner("run", pane, "echo nest-2-no-hook", "--timeout", "3", timeout=15)
    assert "[exit:?]" in r.stdout or "[timeout:" in r.stdout, r.stdout

    # Install hook in nest 2, then it works.
    r = tx_runner("hook-install", pane, timeout=15)
    assert r.returncode == 0 and "installed:" in r.stdout, r.stdout
    r = tx_runner("run", pane, "echo nest-2-with-hook", timeout=10)
    assert "[exit:0]" in r.stdout and "nest-2-with-hook" in r.stdout


# ============== T-4.4 — returning from nested preserves outer hook ==============

def test_T_4_4_returning_from_nested_preserves_outer_hook(tx_runner, tx_home: Path):
    _enable_prompt_fallback(tx_home)
    pane = _pane(tx_runner)

    # Enter one level of nesting, install hook there.
    tx_runner("send", pane, "bash --noprofile --norc -i")
    tx_runner("key", pane, "Enter")
    time.sleep(0.6)
    r = tx_runner("hook-install", pane, timeout=15)
    assert r.returncode == 0 and "installed:" in r.stdout

    # Confirm nested runs work.
    r = tx_runner("run", pane, "echo in-nested", timeout=10)
    assert "[exit:0]" in r.stdout and "in-nested" in r.stdout

    # Exit the nested shell using tx send (a bare `exit` line). We use send,
    # not run, because the marker would be set by the *inner* shell that's
    # about to terminate.
    tx_runner("send", pane, "exit")
    tx_runner("key", pane, "Enter")
    time.sleep(0.6)

    # Outer shell's hook should still be in place from `tx new`.
    r = tx_runner("run", pane, "echo back-outside", timeout=10)
    assert "[exit:0]" in r.stdout, r.stdout
    assert "back-outside" in r.stdout


# ============== T-5.3 — kill-and-recover ==============

def test_T_5_3_kill_and_recover(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "while true; do sleep 1; done", timeout=10)
    run_id = res.stdout.strip().splitlines()[-1].strip()
    time.sleep(0.3)

    res = tx_runner("kill-run", pane, run_id, timeout=10)
    assert res.returncode == 0, res.stdout

    # The killed run is recorded with exit=None.
    runs_out = tx_runner("runs", pane).stdout
    assert run_id in runs_out

    # Follow-up command works on the same pane.
    res = tx_runner("run", pane, "echo recovered", timeout=15)
    assert res.returncode == 0, res.stdout
    assert "recovered" in res.stdout
    assert "[exit:0]" in res.stdout

    # Pane is idle between runs.
    s = tx_runner("status", pane).stdout
    assert "status=idle" in s


# ============== T-5.4 — dead-shell recovery ==============

def test_T_5_4_dead_shell_recovery(tx_runner):
    pane = _pane(tx_runner, "deadpane")

    # Drive the shell to exit. The shell terminates before the marker can
    # fire, so tx-run may report [exit:?] or a timeout — that's incidental
    # to the test; the assertion is on the post-exit state.
    tx_runner("run", pane, "exit", "--timeout", "3", timeout=10)
    time.sleep(0.5)

    s = tx_runner("status", pane).stdout
    assert "status=dead" in s, s

    # A run against the dead pane refuses cleanly.
    r = tx_runner("run", pane, "echo nope", timeout=10)
    assert r.returncode == 1
    assert "dead" in r.stdout

    # Cleanly dispose, then re-create under the same name.
    r = tx_runner("kill", pane, timeout=10)
    assert r.returncode == 0, r.stdout

    pane2 = _pane(tx_runner, "deadpane")
    assert pane2 == "deadpane"
    r = tx_runner("run", pane2, "echo alive", timeout=15)
    assert r.returncode == 0 and "alive" in r.stdout and "[exit:0]" in r.stdout


# ============== T-5.5 — marker wrapper survives pipefail / set -e ==============

def _exit_line(stdout: str) -> int | None:
    for line in stdout.splitlines():
        if line.startswith("[exit:"):
            try:
                return int(line[len("[exit:"):].rstrip("]"))
            except ValueError:
                return None
    return None


def test_T_5_5_pipefail_marker_survives(tx_runner):
    pane = _pane(tx_runner)

    # Default pipeline semantics: status of the last command. `false | true` → 0.
    r = tx_runner("run", pane, "false | true", timeout=10)
    assert r.returncode == 0, r.stdout
    assert _exit_line(r.stdout) == 0, r.stdout

    # pipefail flips the result to the leftmost non-zero exit.
    r = tx_runner("run", pane, "set -o pipefail; false | true", timeout=10)
    assert r.returncode == 0, r.stdout
    assert _exit_line(r.stdout) == 1, r.stdout

    # `&&` short-circuits, so `echo never` doesn't run; exit=1.
    r = tx_runner("run", pane, "false && echo never", timeout=10)
    assert _exit_line(r.stdout) == 1, r.stdout


# ============== T-10.2 — multiple runs share pane shell state ==============

def test_T_10_2_multiple_runs_share_pane_state(tx_runner):
    pane = _pane(tx_runner)
    r = tx_runner("run", pane, "x=42", timeout=10)
    assert r.returncode == 0 and "[exit:0]" in r.stdout

    r = tx_runner("run", pane, "echo $x", timeout=10)
    assert r.returncode == 0, r.stdout
    assert "[exit:0]" in r.stdout
    # The value is in the output (alongside the shell echo of the command,
    # which we tolerate).
    assert "\n42" in r.stdout or " 42" in r.stdout or r.stdout.endswith("42\n"), r.stdout
