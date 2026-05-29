"""Log-tailing wait loops: idle detection, marker detection, bounded capture.

These are the polling primitives that block until something interesting
appears in the pipe-pane log:

- `wait_for_idle`     legacy prompt/silence detection (used by `tx send`,
                      `tx wait`, hooks-missing fallback)
- `wait_for_marker`   v2 marker detection with a prompt-pattern fallback
- `wait_for_marker_or_bound`  superset that also honours `--wait-for`,
                      `--fail-for`, `--until`, `--lines`, `--duration`

Plus two message-formatting helpers (`truthful_timeout_message`,
`busy_error_message`) that turn a state snapshot into the right CLI
error string.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from tx_core.constants import ANSI_RE
from tx_core.marker import find_run_marker, strip_run_markers


def _last_non_empty_line(text: str) -> str:
    for line in reversed(text.split("\n")):
        stripped = ANSI_RE.sub("", line).rstrip()
        if stripped:
            return stripped
    return ""


def wait_for_idle(
    log_path: Path,
    start_offset: int,
    cfg_defaults: dict[str, Any],
    timeout: float,
) -> tuple[bool, int]:
    """Wait until the pane reaches idle. Returns (success, final_log_size)."""
    method = cfg_defaults.get("idle_method", "prompt")
    deadline = time.monotonic() + timeout
    prompt_patterns = [re.compile(p) for p in cfg_defaults.get("prompt_patterns", [])]
    silence_ms = float(cfg_defaults.get("idle_silence_ms", 300))

    last_size = start_offset
    last_change = time.monotonic()
    has_grown = False

    while True:
        time.sleep(0.1)
        size_now = log_path.stat().st_size if log_path.exists() else 0
        if size_now > last_size:
            has_grown = True
            last_change = time.monotonic()
            last_size = size_now

        if method == "silence":
            if has_grown and (time.monotonic() - last_change) * 1000.0 >= silence_ms:
                return True, size_now
        else:
            if prompt_patterns and size_now > start_offset:
                with open(log_path, "rb") as f:
                    f.seek(start_offset)
                    chunk = f.read()
                text = chunk.decode("utf-8", errors="replace")
                text = ANSI_RE.sub("", text).replace("\r", "")
                last_line = _last_non_empty_line(text)
                for pat in prompt_patterns:
                    if pat.search(last_line):
                        return True, size_now

        if time.monotonic() >= deadline:
            return False, size_now


def wait_for_marker(
    log_path: Path,
    start_offset: int,
    run_id: str,
    timeout: float,
    cfg_defaults: dict[str, Any] | None = None,
    poll_interval: float = 0.1,
) -> tuple[bool, int | None, int, float]:
    """Poll the log for the v2 end marker of `run_id`, with a prompt fallback.

    Returns (found, exit_code, end_offset, idle_age_s).
    - `found` True + exit_code int  → hook emitted a real marker.
    - `found` True + exit_code None → no marker observed but the foreground
      returned to a shell prompt and the log has been silent for at least
      `idle_silence_ms`. The user command almost certainly completed, but
      we don't know its exit code. This is the "nested interactive shell
      without the hook" path (ssh / sudo -i / docker exec / …).
    - `found` False                 → real timeout, nothing to render.

    `end_offset` is the byte position right after whichever boundary fired
    (the marker line if a real marker; the current EOF if the fallback fired).
    `idle_age_s` is how long since the log last grew, for truthful timeouts.

    The fallback uses the same `prompt_patterns` / `idle_silence_ms` as the
    legacy `wait_for_idle`. If `cfg_defaults` is None or has no patterns,
    the fallback is disabled and only marker detection is used.
    """
    cfg_defaults = cfg_defaults or {}
    prompt_patterns = [re.compile(p) for p in cfg_defaults.get("prompt_patterns", [])]
    silence_ms = float(cfg_defaults.get("idle_silence_ms", 300))

    deadline = time.monotonic() + timeout
    last_size = start_offset if log_path.exists() else 0
    last_change = time.monotonic()

    while True:
        size_now = log_path.stat().st_size if log_path.exists() else 0
        if size_now > last_size:
            last_change = time.monotonic()
            last_size = size_now
        if size_now > start_offset:
            with open(log_path, "rb") as f:
                f.seek(start_offset)
                raw = f.read()
            marker = find_run_marker(raw, run_id)
            if marker is not None:
                _line_start, line_end, exit_code = marker
                return True, exit_code, start_offset + line_end, time.monotonic() - last_change
            # Prompt-pattern fallback for nested-shell scenarios where the
            # hook isn't installed (the local hook doesn't propagate through
            # ssh / sudo -i / docker exec etc.).
            silent_for_ms = (time.monotonic() - last_change) * 1000.0
            if prompt_patterns and silent_for_ms >= silence_ms:
                text = ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).replace("\r", "")
                last_line = _last_non_empty_line(text)
                for pat in prompt_patterns:
                    if pat.search(last_line):
                        return True, None, size_now, time.monotonic() - last_change
        if time.monotonic() >= deadline:
            return False, None, size_now, time.monotonic() - last_change
        time.sleep(poll_interval)


def wait_for_marker_or_bound(
    log_path: Path,
    start_offset: int,
    run_id: str,
    timeout: float,
    cfg_defaults: dict[str, Any] | None = None,
    wait_for_re: re.Pattern[str] | None = None,
    fail_for_re: re.Pattern[str] | None = None,
    until_re: re.Pattern[str] | None = None,
    until_lines: int | None = None,
    until_duration_s: float | None = None,
    poll_interval: float = 0.1,
) -> tuple[str, int | None, int, float, str | None]:
    """Poll the log until any termination condition fires.

    Termination reasons (`reason`):
      - "marker"   : the run's end marker was observed (normal completion).
      - "wait-for" : the wait-for regex matched in cleaned output first.
      - "fail-for" : the fail-for regex matched first.
      - "until"    : --until regex matched (`tx stream`).
      - "lines"    : --lines cleaned-line count reached.
      - "duration" : --duration wall-clock elapsed.
      - "timeout"  : nothing fired before the overall `timeout`.

    Returns (reason, exit_code, end_offset, idle_age_s, matched_text):
      - exit_code is populated only when reason == "marker".
      - matched_text is the matching line for wait-for / fail-for / until,
        or None otherwise.

    The prompt-pattern fallback (for nested-shell scenarios) is honoured
    when cfg_defaults provides `prompt_patterns` and `idle_silence_ms`.
    """
    cfg_defaults = cfg_defaults or {}
    prompt_patterns = [re.compile(p) for p in cfg_defaults.get("prompt_patterns", [])]
    silence_ms = float(cfg_defaults.get("idle_silence_ms", 300))

    deadline = time.monotonic() + timeout
    start_wall = time.monotonic()
    last_size = start_offset if log_path.exists() else 0
    last_change = time.monotonic()

    while True:
        size_now = log_path.stat().st_size if log_path.exists() else 0
        if size_now > last_size:
            last_change = time.monotonic()
            last_size = size_now
        if size_now > start_offset:
            with open(log_path, "rb") as f:
                f.seek(start_offset)
                raw = f.read()
            marker = find_run_marker(raw, run_id)
            if marker is not None:
                _ls, line_end, exit_code = marker
                return "marker", exit_code, start_offset + line_end, time.monotonic() - last_change, None

            cleaned = ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).replace("\r", "")
            cleaned = strip_run_markers(cleaned)

            if wait_for_re is not None:
                m = wait_for_re.search(cleaned)
                if m:
                    return "wait-for", None, size_now, time.monotonic() - last_change, m.group(0)
            if fail_for_re is not None:
                m = fail_for_re.search(cleaned)
                if m:
                    return "fail-for", None, size_now, time.monotonic() - last_change, m.group(0)
            if until_re is not None:
                m = until_re.search(cleaned)
                if m:
                    return "until", None, size_now, time.monotonic() - last_change, m.group(0)
            if until_lines is not None:
                non_empty = [ln for ln in cleaned.split("\n") if ln.strip()]
                if len(non_empty) >= until_lines:
                    return "lines", None, size_now, time.monotonic() - last_change, None

            silent_for_ms = (time.monotonic() - last_change) * 1000.0
            if prompt_patterns and silent_for_ms >= silence_ms:
                last_line = _last_non_empty_line(cleaned)
                for pat in prompt_patterns:
                    if pat.search(last_line):
                        return "marker", None, size_now, time.monotonic() - last_change, None

        if until_duration_s is not None and (time.monotonic() - start_wall) >= until_duration_s:
            return "duration", None, size_now, time.monotonic() - last_change, None

        if time.monotonic() >= deadline:
            return "timeout", None, size_now, time.monotonic() - last_change, None
        time.sleep(poll_interval)


def truthful_timeout_message(
    pane_id: str,
    state_info: dict[str, Any],
    run_id: str,
    timeout: float,
    log_path: Path,
    idle_age_s: float,
) -> str:
    """Construct a state-aware timeout meta line. The variants match §2.6."""
    status = state_info.get("status", "unknown")
    size = log_path.stat().st_size if log_path.exists() else 0
    if status == "running":
        return (
            f"no end marker after {int(timeout)}s; pane state=running "
            f"(last output {idle_age_s:.1f}s ago, {size}B). "
            f"Inspect with 'tx tail {pane_id}' or 'tx kill-run {pane_id} {run_id}'."
        )
    if status == "tui":
        return (
            f"no end marker after {int(timeout)}s; pane state=tui (alternate-screen on). "
            f"Use 'tx kill-run {pane_id} {run_id}' to interrupt."
        )
    if status == "waiting-input":
        prompt = state_info.get("waiting_pattern") or "(unknown)"
        return (
            f"no end marker after {int(timeout)}s; pane state=waiting-input ('{prompt}'). "
            f"Use 'tx run --stdin {pane_id} <text>' to feed input, "
            f"'tx send-secret {pane_id}' for secrets, or 'tx handoff {pane_id}'."
        )
    if status == "paused":
        return (
            f"no end marker after {int(timeout)}s; pane state=paused (handoff active). "
            f"Run 'tx resume {pane_id}' to take control back."
        )
    if status == "dead":
        # Best-effort: include the last few lines.
        try:
            with open(log_path, "rb") as f:
                f.seek(max(0, size - 4096))
                raw = f.read()
            cleaned = ANSI_RE.sub("", raw.decode("utf-8", errors="replace"))
            tail = "\n".join([l for l in cleaned.splitlines() if l][-5:])
        except Exception:
            tail = ""
        return f"pane state=dead; shell exited during run. Last lines:\n{tail}"
    return f"no end marker after {int(timeout)}s; pane state={status}."


def busy_error_message(pane_id: str, info: dict[str, Any]) -> str:
    """Format the multi-line refuse-on-busy error."""
    run_id = info.get("active_run_id") or "?"
    cur_cmd = info.get("current_command") or "?"
    state = info.get("status", "?")
    return (
        f"pane '{pane_id}' busy with run {run_id} (foreground: {cur_cmd}, state: {state}).\n"
        f"        Options:\n"
        f"          --queue          wait until idle, then send\n"
        f"          --stdin          feed text to the running command's stdin\n"
        f"          --kill-and-run   interrupt {run_id} (C-c), wait for prompt, then send\n"
        f"        Or run 'tx wait-run {pane_id} {run_id}' / 'tx kill-run {pane_id} {run_id}'."
    )
