"""Thin libtmux wrappers used to manage the tmux server, sessions, panes,
and pipe-pane capture. No tx-specific state lives here — these are
mechanical helpers that translate tx-pane's intent to libtmux/tmux calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import libtmux
import libtmux.exc

from tx_core.output import err


def get_server() -> libtmux.Server:
    return libtmux.Server()


def get_or_create_session(server: libtmux.Server, session_name: str) -> libtmux.Session:
    try:
        sessions = list(server.sessions)
    except libtmux.exc.LibTmuxException:
        sessions = []
    for s in sessions:
        if s.session_name == session_name:
            return s
    try:
        return server.new_session(session_name=session_name, detach=True)
    except libtmux.exc.LibTmuxException as e:
        err(f"could not create tmux session '{session_name}' — is tmux installed? ({e})")
        raise  # unreachable


def find_pane_anywhere(server: libtmux.Server, tmux_pane_id: str) -> libtmux.Pane | None:
    try:
        for s in server.sessions:
            for w in s.windows:
                for p in w.panes:
                    if p.pane_id == tmux_pane_id:
                        return p
    except libtmux.exc.LibTmuxException:
        return None
    return None


def allocate_pane(
    session: libtmux.Session,
    claimed_tmux_ids: set[str],
    window_name: str,
    start_directory: str | None = None,
) -> tuple[str, bool]:
    """Allocate a tmux pane for tx-pane.

    Returns (tmux_pane_id, adopted) — `adopted` True iff we took over an
    existing session-initial pane (in which case the caller may need to send
    `cd <path>` afterward to honour --cwd).

    - If the session has exactly one window with one unclaimed pane (i.e. just the
      session's initial shell), adopt that pane in-place.
    - Otherwise create a new window so each tx-pane pane is full-size in its own
      viewport (no split layouts).
    """
    windows = list(session.windows)
    if len(windows) == 1 and len(list(windows[0].panes)) == 1:
        only_pane = windows[0].panes[0]
        if only_pane.pane_id not in claimed_tmux_ids:
            try:
                windows[0].cmd("rename-window", window_name)
            except Exception:
                pass
            return only_pane.pane_id, True

    kwargs: dict[str, Any] = {"window_name": window_name, "attach": False}
    if start_directory:
        kwargs["start_directory"] = start_directory
    new_window = session.new_window(**kwargs)
    panes = list(new_window.panes)
    if not panes:
        err("tmux new-window created a window with no panes")
    return panes[0].pane_id, False


def start_pipe_pane(pane: libtmux.Pane, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    # -o means "only open if no current pipe" (toggle semantics for fresh panes).
    pane.cmd("pipe-pane", "-o", f"cat >> {log_path}")


def stop_pipe_pane(pane: libtmux.Pane) -> None:
    try:
        pane.cmd("pipe-pane")
    except libtmux.exc.LibTmuxException:
        pass
