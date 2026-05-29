"""Stateless helpers used across the tx-pane codebase.

Duration formatting / parsing, tmux version probing, bookmark resolution,
'how long has this run been alive' math, and the sudo-prefix string. These
are pure leaves with at most one tx_core dependency (`output.err`).
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Any

from tx_core.output import err


def _duration_str(started: str, ended: str | None) -> str:
    if not ended:
        return "-"
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        e = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        secs = (e - s).total_seconds()
        if secs < 1:
            return f"{int(secs * 1000)}ms"
        if secs < 60:
            return f"{secs:.1f}s"
        return f"{int(secs // 60)}m{int(secs % 60)}s"
    except Exception:
        return "-"


def _parse_duration(spec: str) -> float:
    """Parse '5', '5s', '2m', '1h' into seconds. Bare numbers are seconds."""
    s = spec.strip().lower()
    if not s:
        raise ValueError("empty duration")
    if s[-1] in "smh":
        unit = s[-1]
        try:
            value = float(s[:-1])
        except ValueError as e:
            raise ValueError(f"invalid number in '{spec}'") from e
        return value * (1 if unit == "s" else 60 if unit == "m" else 3600)
    try:
        return float(s)
    except ValueError as e:
        raise ValueError(f"invalid duration '{spec}'") from e


def _detect_tmux_version() -> str:
    """Best-effort detection of the running tmux version."""
    try:
        result = subprocess.run(
            ["tmux", "-V"], capture_output=True, text=True, timeout=2.0
        )
        return result.stdout.strip() or result.stderr.strip() or "unknown"
    except Exception:
        return "unknown"


def _resolve_bookmark(state: dict[str, Any], name: str) -> int:
    bookmarks = state.get("bookmarks") or {}
    if name not in bookmarks:
        err(f"bookmark '{name}' not found (known: {', '.join(sorted(bookmarks)) or 'none'})")
    return int(bookmarks[name])


def _running_for_seconds(active: dict[str, Any]) -> float | None:
    started = active.get("started")
    if not started:
        return None
    try:
        s = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - s).total_seconds()
    except Exception:
        return None


def _sudo_prefix(use_sudo: bool) -> str:
    """Return 'sudo -n ' when --sudo is in effect, else empty.

    `-n` keeps the command non-interactive: sudo refuses cleanly with
    'a password is required' if no cached credentials exist, instead of
    silently blocking the pane.
    """
    return "sudo -n " if use_sudo else ""
