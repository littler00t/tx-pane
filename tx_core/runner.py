"""Run-execution primitives used by the CLI command handlers.

`_start_run` packages the marker-wrap → send-keys → record_run_start
sequence into one call. `_maybe_reinstall_hook` is its companion that
re-sends the marker hook before the wrap when a previous run finalised
without a marker. `_apply_on_timeout` implements the --on-timeout
report/cancel/kill policies after `wait_for_marker` returned timeout.

The two `_internal_*` helpers chain a multi-step composite run (used by
`tx write` today) behind one user-facing call: each step is its own
marker-tracked run visible in `tx runs`, but the orchestration is
hidden from the caller.
"""

from __future__ import annotations

import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

import libtmux

from tx_core.config import (
    load_offsets,
    offsets_lock,
    record_run_end,
    record_run_start,
    save_offsets,
)
from tx_core.marker import (
    make_run_id,
    shell_init_setup_for,
    strip_run_markers,
    wrap_command,
)
from tx_core.output import err
from tx_core.render import _read_cleaned_text, _resolve_pane_for_input
from tx_core.state import finalize_runs
from tx_core.tmux import get_server
from tx_core.wait import wait_for_marker


def _maybe_reinstall_hook(
    tmux_pane: libtmux.Pane,
    log_path: Path,
    state: dict[str, Any],
    cfg: dict[str, Any],
) -> bool:
    """If the pane's hook is flagged missing and auto-reinstall is enabled,
    send SHELL_INIT_SETUP (shell-aware) once before the next wrap.

    Returns True iff a reinstall was attempted. Marks `state['hook_ok']` True
    optimistically — the next run's marker observation will confirm or flip it
    back via `record_run_end`.
    """
    if state.get("hook_ok", True):
        return False
    if not bool(cfg["defaults"].get("auto_reinstall_hook", True)):
        return False
    shell = state.get("shell")
    snippet = shell_init_setup_for(shell)
    try:
        tmux_pane.send_keys(snippet, enter=True, suppress_history=False, literal=True)
    except Exception:
        return False
    # Give the shell a beat to absorb the setup before the wrap lands.
    time.sleep(0.15)
    # Tail-offset moves past the snippet echo so subsequent dumps don't see it.
    try:
        state["tail_offset"] = log_path.stat().st_size
    except OSError:
        pass
    state["hook_ok"] = True
    return True


def _start_run(
    tmux_pane: libtmux.Pane,
    log_path: Path,
    cmd: str,
    max_wait_s: float,
    offsets: dict[str, Any],
    pane: str,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Send the marker-wrapped command and record an active run in offsets.

    Returns the new run_id. Caller is responsible for save_offsets. If `cfg`
    is provided and the pane's `hook_ok` is False, sends SHELL_INIT_SETUP
    before the wrap (auto-reinstall, see _maybe_reinstall_hook).
    """
    if cfg is not None:
        _maybe_reinstall_hook(tmux_pane, log_path, offsets[pane], cfg)
    run_id = make_run_id()
    wrapped = wrap_command(cmd, run_id)
    start_offset = log_path.stat().st_size
    state = offsets[pane]
    state.pop("pending_lines", None)
    state["tail_offset"] = start_offset
    record_run_start(state, run_id, cmd, start_offset, max_wait_s)
    offsets[pane] = state
    tmux_pane.send_keys(wrapped, enter=True, suppress_history=False, literal=True)
    return run_id


def _apply_on_timeout(
    policy: str,
    tmux_pane: libtmux.Pane,
    log_path: Path,
    start_offset: int,
    run_id: str,
    cfg_defaults: dict[str, Any],
) -> tuple[bool, int | None, int, float, str | None]:
    """Apply a post-timeout cancellation policy.

    Returns (found, exit_code, end_offset, idle_age, extra_note).
    For policy='report' returns (False, None, last_size, 0, None).
    For 'cancel' sends C-c then re-waits briefly; on success returns found=True.
    For 'kill' sends C-c twice then kill-pane and returns found=False with a note.
    """
    policy = policy.lower()
    if policy == "report":
        size = log_path.stat().st_size if log_path.exists() else 0
        return False, None, size, 0.0, None
    if policy == "cancel":
        try:
            tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)
        except Exception:
            pass
        time.sleep(0.3)
        found, exit_code, end_offset, idle_age = wait_for_marker(
            log_path, start_offset, run_id, timeout=3.0, cfg_defaults=cfg_defaults
        )
        return found, exit_code, end_offset, idle_age, "cancelled via C-c"
    if policy == "kill":
        try:
            tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)
            time.sleep(0.2)
            tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)
            time.sleep(0.2)
        except Exception:
            pass
        try:
            tmux_pane.cmd("kill-pane")
        except Exception:
            pass
        size = log_path.stat().st_size if log_path.exists() else 0
        return False, None, size, 0.0, "pane killed (tmux pane destroyed)"
    return False, None, 0, 0.0, None


def _internal_marker_run(
    pane: str,
    cmd: str,
    cfg: dict[str, Any],
    timeout: float,
) -> tuple[int | None, str]:
    """Run `cmd` in `pane` via the marker protocol; block until marker or
    timeout; return (exit_code, cleaned_stdout).

    Used by composite commands (currently `tx write`) that need to chain
    several remote operations behind one user-facing call. Each step:
      - acquires the offsets lock briefly to allocate a run-id + record the
        active run (so it shows up in `tx runs`),
      - releases the lock for the long wait,
      - re-acquires to finalize.
    Caller MUST verify the pane is idle / not paused / hook-OK before calling;
    refuse-on-busy is not re-checked here.
    """
    max_history = int(cfg["defaults"].get("max_run_history", 100))

    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        run_id = _start_run(tmux_pane, log_path, cmd, timeout, offsets, pane, cfg=cfg)
        start_offset = int(offsets[pane]["active_run"]["start_offset"])
        save_offsets(offsets)

    found, exit_code, end_offset, _idle = wait_for_marker(
        log_path, start_offset, run_id, timeout, cfg["defaults"]
    )

    with offsets_lock():
        offsets = load_offsets()
        state = offsets[pane]
        if found:
            record_run_end(state, run_id, exit_code, end_offset, max_history)
            state["tail_offset"] = end_offset
            offsets[pane] = state
            save_offsets(offsets)
            text = _read_cleaned_text(log_path, start_offset, end_offset)
            text = strip_run_markers(text)
            return exit_code, text
        # Timeout: try a one-shot C-c so the pane can recover for follow-up steps.
        try:
            tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)
        except Exception:
            pass
        save_offsets(offsets)
    return None, ""


def _internal_paste_then_marker(
    pane: str,
    prelude_cmd: str,
    paste_bytes: bytes,
    cfg: dict[str, Any],
    timeout: float,
) -> tuple[int | None, str]:
    """Compound: send `prelude_cmd` (which opens a heredoc), bracketed-paste
    `paste_bytes` as the heredoc body, then wait for the run's marker.

    Used by `tx write` so the wrapping `__tx_run_id=...; cat > ... <<'EOF'`
    line is sent first, and the file content is streamed via tmux's buffer
    paste machinery — robust for content of any size and any byte pattern.
    """
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    server = get_server()

    with offsets_lock():
        offsets = load_offsets()
        state, _srv, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        # The prelude carries the run-id assignment; reuse _start_run for the
        # offsets bookkeeping but capture the run-id so the heredoc body is
        # part of the same shell statement.
        run_id = _start_run(tmux_pane, log_path, prelude_cmd, timeout, offsets, pane, cfg=cfg)
        start_offset = int(offsets[pane]["active_run"]["start_offset"])
        save_offsets(offsets)

    # Brief settle: let the shell enter heredoc-reading state before we paste.
    time.sleep(0.15)

    # Load the heredoc body into a tmux buffer and paste it (bracketed). Using
    # a temp file means the bytes never traverse libtmux's argv encoding.
    buf_name = f"txwrite-{secrets.token_hex(4)}"
    fd, tmp_name = tempfile.mkstemp(prefix=".txwrite-", suffix=".buf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(paste_bytes)
        try:
            server.cmd("load-buffer", "-b", buf_name, tmp_name)
        except Exception as e:
            err(f"tmux load-buffer failed during tx write: {e}")
        try:
            tmux_pane.cmd("paste-buffer", "-d", "-b", buf_name, "-t", tmux_pane.pane_id)
        except Exception as e:
            try:
                server.cmd("delete-buffer", "-b", buf_name)
            except Exception:
                pass
            err(f"tmux paste-buffer failed during tx write: {e}")
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass

    found, exit_code, end_offset, _idle = wait_for_marker(
        log_path, start_offset, run_id, timeout, cfg["defaults"]
    )

    with offsets_lock():
        offsets = load_offsets()
        state = offsets[pane]
        if found:
            record_run_end(state, run_id, exit_code, end_offset, max_history)
            state["tail_offset"] = end_offset
            offsets[pane] = state
            save_offsets(offsets)
            text = _read_cleaned_text(log_path, start_offset, end_offset)
            text = strip_run_markers(text)
            return exit_code, text
        try:
            tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)
        except Exception:
            pass
        save_offsets(offsets)
    return None, ""
