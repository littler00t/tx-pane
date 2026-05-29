"""OS-level process and tmux-client probes.

Best-effort introspection of pid → cwd / comm / children, used by
pane_state() to surface "what's actually running in the pane" without
relying on libtmux's stale per-pane attributes alone. Each probe is
isolated and degrades gracefully (returns None / []) on platforms or
permission setups where the underlying lookup is unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import libtmux


def _read_proc_cwd(pid: int) -> str | None:
    """Linux only: read /proc/<pid>/cwd. Returns None if unavailable."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _lsof_cwd(pid: int) -> str | None:
    """macOS fallback: parse `lsof -p <pid>` for the cwd entry."""
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return line[1:].strip() or None
    return None


def read_pane_cwd(pid: int | None) -> str | None:
    """Best-effort: return the cwd of the process tree rooted at `pid`.

    Tries Linux's /proc first, falls back to macOS lsof.
    """
    if pid is None:
        return None
    cwd = _read_proc_cwd(pid)
    if cwd is not None:
        return cwd
    if sys.platform == "darwin":
        return _lsof_cwd(pid)
    return None


def _proc_comm(pid: int) -> str | None:
    """Best-effort: process name for a pid."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=2.0,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return None


def _proc_children(pid: int) -> list[int]:
    """Return immediate children of pid. Uses pgrep -P (POSIX-ish) for portability."""
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    out: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            out.append(int(line))
    return out


def walk_foreground(pid: int | None) -> tuple[int, str] | None:
    """Walk down the child-process tree from `pid` to its leaf.

    Returns (leaf_pid, comm) or None if pid is None / no descendants found
    (in which case the caller should treat `pid` itself as the foreground).
    """
    if pid is None:
        return None
    current = pid
    seen: set[int] = set()
    for _ in range(8):  # cap depth to avoid pathological loops
        if current in seen:
            break
        seen.add(current)
        children = _proc_children(current)
        if not children:
            break
        current = children[0]
    if current == pid:
        return None
    comm = _proc_comm(current) or "?"
    return current, comm


def tmux_attached_clients(server: libtmux.Server, session_name: str) -> list[str]:
    """Return ttys of clients attached to the given tmux session, or []."""
    try:
        result = server.cmd("list-clients", "-t", session_name, "-F", "#{client_tty}")
    except Exception:
        return []
    if not result.stdout:
        return []
    return [line.strip() for line in result.stdout if line.strip()]


def pane_alt_screen(pane: libtmux.Pane) -> bool:
    """Return True if the pane has the tmux alternate-screen flag set (TUI)."""
    try:
        result = pane.cmd("display-message", "-p", "#{alternate_on}")
        out = list(result.stdout) if result.stdout else []
        return bool(out) and out[0].strip() == "1"
    except Exception:
        return False
