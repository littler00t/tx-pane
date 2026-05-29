"""Pane state machine + helpers that depend on it.

`pane_state` is the read-side of the runtime — given a snapshot of
offsets + the live tmux pane, it returns one of {dead, paused, tui,
running, waiting-input, idle}. `finalize_runs` is the matching write-side:
if an active run's marker has appeared (or the prompt-pattern fallback
fires), it promotes the run into the historical `runs` list.

Also hosts the small lookup helpers (`require_pane`, `pane_log_path`)
and `render_log_range`, since they're used everywhere `pane_state` is.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import libtmux

from tx_core.config import record_run_end
from tx_core.constants import ANSI_RE, LOGS_DIR, SHELL_NAMES
from tx_core.log import process_raw_log
from tx_core.marker import find_run_marker
from tx_core.output import err
from tx_core.proc import pane_alt_screen
from tx_core.tmux import find_pane_anywhere
from tx_core.wait import _last_non_empty_line


def require_pane(offsets: dict[str, Any], pane_id: str) -> dict[str, Any]:
    if pane_id.startswith("_") or pane_id not in offsets:
        err(f"pane '{pane_id}' not found — run 'tx ls' to see active panes")
    return offsets[pane_id]


def pane_log_path(pane_id: str) -> Path:
    return LOGS_DIR / f"{pane_id}.log"


def render_log_range(
    log_path: Path,
    start_offset: int,
    end_offset: int | None,
    strip_blanks: bool,
) -> list[str]:
    """Read bytes from start_offset to end_offset (or EOF), return cleaned line list."""
    file_size = log_path.stat().st_size
    end = file_size if end_offset is None else min(end_offset, file_size)
    with open(log_path, "rb") as f:
        f.seek(start_offset)
        raw = f.read(max(0, end - start_offset))
    kept, _truncated, _remaining, _consumed = process_raw_log(raw, 10**9, strip_blanks)
    return kept


def _tail_text(log_path: Path, max_bytes: int = 4096) -> str:
    """Read the last `max_bytes` of a log, cleaned of ANSI / \\r. Safe on small files."""
    if not log_path.exists():
        return ""
    size = log_path.stat().st_size
    with open(log_path, "rb") as f:
        f.seek(max(0, size - max_bytes))
        raw = f.read()
    return ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).replace("\r", "")


def _matches_waiting(text: str, patterns: list[re.Pattern[str]]) -> str | None:
    """If the last non-empty line matches any pattern, return that line."""
    if not patterns:
        return None
    last = _last_non_empty_line(text)
    if not last:
        return None
    for pat in patterns:
        if pat.search(last):
            return last
    return None


def pane_state(
    server: libtmux.Server,
    state: dict[str, Any],
    pane_id: str,
    cfg_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute current pane state.

    Returns a dict with keys: status, active_run_id, current_command, pid,
    alt_screen, waiting_pattern. Pure read; does not mutate offsets.

    status values:
      - "dead"          : tmux pane is gone
      - "paused"        : tx handoff in effect (state["paused_at"] set)
      - "tui"           : alternate-screen on (vim/less/htop)
      - "running"       : active_run is set and its marker is not yet in the log
      - "waiting-input" : foreground is a shell and last log line matches a
                          waiting_patterns regex (only when cfg_defaults given)
      - "idle"          : everything else
    """
    tmux_id = state.get("tmux_id", "")
    pane = find_pane_anywhere(server, tmux_id) if tmux_id else None
    if pane is None:
        return {
            "status": "dead",
            "active_run_id": (state.get("active_run") or {}).get("id"),
            "current_command": None,
            "pid": None,
            "alt_screen": False,
            "waiting_pattern": None,
        }

    # paused takes precedence over everything except dead — tx commands must
    # refuse while the user is mid-handoff regardless of foreground state.
    if state.get("paused_at"):
        try:
            pane.refresh()
        except Exception:
            pass
        return {
            "status": "paused",
            "active_run_id": (state.get("active_run") or {}).get("id"),
            "current_command": (pane.pane_current_command or "").strip() or None,
            "pid": (pane.pane_pid or "").strip() or None,
            "alt_screen": pane_alt_screen(pane),
            "waiting_pattern": None,
        }

    try:
        pane.refresh()
    except Exception:
        pass
    current_cmd = (pane.pane_current_command or "").strip() or None
    pid = (pane.pane_pid or "").strip() or None
    alt = pane_alt_screen(pane)

    active = state.get("active_run")
    if active:
        run_id = active.get("id", "")
        start_offset = int(active.get("start_offset", 0))
        log_path = pane_log_path(pane_id)
        if log_path.exists():
            with open(log_path, "rb") as f:
                f.seek(start_offset)
                raw = f.read()
            if find_run_marker(raw, run_id) is None:
                # Marker not in log; declare 'running'. cfg_defaults trigger
                # the same prompt-pattern fallback wait_for_marker uses so
                # tx ls / tx status / tx runs don't show stale "running" for
                # nested-shell runs that completed without a marker.
                if cfg_defaults is not None:
                    prompt_patterns = [re.compile(p) for p in cfg_defaults.get("prompt_patterns", [])]
                    if prompt_patterns:
                        text = ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).replace("\r", "")
                        last_line = _last_non_empty_line(text)
                        for pat in prompt_patterns:
                            if pat.search(last_line):
                                # Run completed via fallback — surface as idle.
                                return {
                                    "status": "idle",
                                    "active_run_id": run_id,
                                    "current_command": current_cmd,
                                    "pid": pid,
                                    "alt_screen": alt,
                                    "waiting_pattern": None,
                                }
                return {
                    "status": "running",
                    "active_run_id": run_id,
                    "current_command": current_cmd,
                    "pid": pid,
                    "alt_screen": alt,
                    "waiting_pattern": None,
                }
        # Marker present (or log missing) — the run completed; caller may
        # want to call finalize_runs to record it.
        return {
            "status": "idle",
            "active_run_id": run_id,
            "current_command": current_cmd,
            "pid": pid,
            "alt_screen": alt,
            "waiting_pattern": None,
        }

    if alt:
        return {
            "status": "tui",
            "active_run_id": None,
            "current_command": current_cmd,
            "pid": pid,
            "alt_screen": True,
            "waiting_pattern": None,
        }

    # waiting-input heuristic: shell foreground + tail matches a waiting pattern.
    if cfg_defaults is not None:
        waiting_patterns = [re.compile(p) for p in cfg_defaults.get("waiting_patterns", [])]
        if waiting_patterns and current_cmd in SHELL_NAMES:
            text = _tail_text(pane_log_path(pane_id))
            matched = _matches_waiting(text, waiting_patterns)
            if matched is not None:
                return {
                    "status": "waiting-input",
                    "active_run_id": None,
                    "current_command": current_cmd,
                    "pid": pid,
                    "alt_screen": False,
                    "waiting_pattern": matched,
                }

    return {
        "status": "idle",
        "active_run_id": None,
        "current_command": current_cmd,
        "pid": pid,
        "alt_screen": False,
        "waiting_pattern": None,
    }


def finalize_runs(
    offsets: dict[str, Any],
    pane_id: str,
    max_history: int,
    cfg_defaults: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """If the active run's end marker is now in the log, move it to the runs
    history and clear active_run. Returns the recorded run entry, or None if
    no finalisation was needed.

    When `cfg_defaults` is passed and includes `prompt_patterns`, applies the
    nested-shell fallback: if no marker but the log ends in a shell prompt and
    has been silent for `idle_silence_ms`, records the run as exit=None.
    """
    state = offsets.get(pane_id)
    if not state:
        return None
    active = state.get("active_run")
    if not active:
        return None
    run_id = active.get("id", "")
    start_offset = int(active.get("start_offset", 0))
    log_path = pane_log_path(pane_id)
    if not log_path.exists():
        return None
    with open(log_path, "rb") as f:
        f.seek(start_offset)
        raw = f.read()
    found = find_run_marker(raw, run_id)
    if found is not None:
        _line_start, line_end, exit_code = found
        end_offset = start_offset + line_end
        record_run_end(state, run_id, exit_code, end_offset, max_history)
        offsets[pane_id] = state
        return state["runs"][-1]

    # Fallback path: prompt + silence. Used by tx ls / tx status / tx runs so
    # they don't show a stale "running" for nested-shell runs that completed
    # without a marker. Requires both: caller passed cfg_defaults, and the log
    # has been silent at least idle_silence_ms.
    if cfg_defaults is None:
        return None
    prompt_patterns = [re.compile(p) for p in cfg_defaults.get("prompt_patterns", [])]
    if not prompt_patterns:
        return None
    silence_ms = float(cfg_defaults.get("idle_silence_ms", 300))
    mtime_age_ms = (time.time() - log_path.stat().st_mtime) * 1000.0
    if mtime_age_ms < silence_ms:
        return None
    text = ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).replace("\r", "")
    last_line = _last_non_empty_line(text)
    if not last_line:
        return None
    for pat in prompt_patterns:
        if pat.search(last_line):
            end_offset = start_offset + len(raw)
            record_run_end(state, run_id, None, end_offset, max_history)
            offsets[pane_id] = state
            return state["runs"][-1]
    return None


def pane_status(
    server: libtmux.Server,
    state: dict[str, Any],
    pane_id: str,
) -> tuple[str, str, str]:
    """Back-compat: return the old (status, command, pid) tuple used by `tx ls`."""
    info = pane_state(server, state, pane_id)
    status = info["status"]
    if status == "dead":
        return "exited", "-", "-"
    cmd_text = info["current_command"] or "-"
    pid_text = info["pid"] or "-"
    # Surface "unread" when there is content past tail_offset and the pane is
    # otherwise idle — keeps the existing CLAUDE-facing semantics for now.
    if status == "idle":
        log_path = pane_log_path(pane_id)
        tail_offset = int(state.get("tail_offset", 0))
        file_size = log_path.stat().st_size if log_path.exists() else 0
        if file_size > tail_offset:
            return "unread", cmd_text, pid_text
    return status, cmd_text, pid_text


def poll_until_idle(
    server: libtmux.Server,
    offsets: dict[str, Any],
    pane_id: str,
    max_wait_s: float,
    poll_interval: float = 0.1,
    cfg_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Poll pane_state (auto-finalising completed runs) until it reads idle.

    Returns the final state-info dict. If max_wait expires the dict may still
    report running/tui — caller decides what to do.
    """
    max_history = 100
    deadline = time.monotonic() + max_wait_s
    while True:
        finalize_runs(offsets, pane_id, max_history, cfg_defaults)
        info = pane_state(server, offsets[pane_id], pane_id, cfg_defaults)
        if info["status"] in ("idle", "dead"):
            return info
        if time.monotonic() >= deadline:
            return info
        time.sleep(poll_interval)
