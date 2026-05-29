"""tx_core.commands.panes — pane-lifecycle commands (new / ls / kill / status / restart / info)

Extracted verbatim from the monolithic `tx` script during the modular
refactor. Each `@cli.command()` registers itself on the shared `cli`
root group on import.
"""

from __future__ import annotations

from tx_core.commands._common import *  # noqa: F401,F403


# ----- new -----
@cli.command(
    name="new",
    short_help="Create a new pane.",
    help=(
        "Create a pane. Name is optional; a pane id is generated if omitted (e.g. p1).\n\n"
        "Returns the pane id on one line. Always capture this for subsequent commands.\n"
        "All panes live in the tmux session defined by config (default: 'tx').\n"
        "Attach any time with: tmux attach -t tx\n\n"
        "--cwd <path>  start the shell in <path>"
    ),
)
@click.argument("name", required=False)
@click.option("--cwd", "cwd", type=str, default=None, help="start the shell in this directory")
@click.option(
    "--shell",
    "shell",
    type=click.Choice(["bash", "zsh", "sh", "fish"], case_sensitive=False),
    default=None,
    help="override the shell for this pane (bash/zsh/sh/fish)",
)
def cmd_new(name: str | None, cwd: str | None, shell: str | None) -> None:
    cfg = load_config()
    offsets = load_offsets()
    session_name = cfg["defaults"]["tmux_session"]

    if name is None:
        next_id = int(offsets.get("_next_id", 1))
        while True:
            candidate = f"p{next_id}"
            if candidate not in offsets:
                break
            next_id += 1
        pane_id = candidate
        offsets["_next_id"] = next_id + 1
    else:
        if name.startswith("_"):
            err(f"pane name '{name}' is reserved (cannot start with '_')")
        if name in offsets:
            err(f"pane '{name}' already exists")
        pane_id = name

    if cwd is not None:
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_dir():
            err(f"--cwd '{cwd}' is not a directory")

    # Resolve the shell binary if --shell was given. We accept the bare names
    # bash/zsh/sh/fish so callers don't need an absolute path; the OS PATH
    # lookup happens at exec time inside the pane.
    shell_bin = shell.lower() if shell else None

    server = get_server()
    session = get_or_create_session(server, session_name)
    claimed = {
        v.get("tmux_id", "")
        for k, v in offsets.items()
        if not k.startswith("_") and isinstance(v, dict)
    }
    tmux_pane_id, adopted = allocate_pane(session, claimed, pane_id, start_directory=cwd)
    pane = find_pane_anywhere(server, tmux_pane_id)
    if pane is None:
        err(f"could not locate newly created tmux pane '{tmux_pane_id}'")

    log_path = pane_log_path(pane_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Rotate a non-trivial stale log (from a prior pane with the same id) so
    # we don't lose history. If it's small (under the threshold) or absent,
    # truncate as before.
    if not maybe_rotate_log(log_path, cfg):
        with open(log_path, "wb"):
            pass
    start_pipe_pane(pane, log_path)

    # Bump tmux's per-pane scrollback for long sysadmin sessions (§9.3).
    try:
        history_limit = int(cfg["defaults"].get("history_limit", 100000))
        pane.cmd("set-option", "-p", "history-limit", str(history_limit))
    except Exception:
        pass

    # Adopted-initial-pane path: tmux can't retroactively set the shell's cwd,
    # so we send `cd <path>` after the hook so prior interactive output (if any)
    # doesn't dominate. New-window path uses tmux's -c flag at creation.
    if cwd is not None and adopted:
        try:
            pane.send_keys(f"cd {click.format_filename(str(cwd_path))}", enter=True, suppress_history=False, literal=True)
        except Exception:
            pass

    # --shell: replace the current shell process with the requested one.
    # `exec <shell>` keeps the same tty and PID slot, so pipe-pane / cwd /
    # history-limit all remain attached. The marker hook is installed *after*
    # exec so it lands in the chosen shell, not the parent.
    if shell_bin is not None:
        try:
            pane.send_keys(f"exec {shell_bin}", enter=True, suppress_history=False, literal=True)
        except Exception:
            pass
        # Give the new shell a moment to come up.
        time.sleep(0.4)

    # Install the v2 marker-emission hook (PROMPT_COMMAND in bash, precmd in
    # zsh, fish_postexec in fish). When --shell wasn't given, send the bash/zsh
    # form (a no-op in fish, but harmless).
    init_snippet = shell_init_setup_for(shell_bin)
    try:
        pane.send_keys(init_snippet, enter=True, suppress_history=False, literal=True)
    except Exception:
        pass
    # Brief settle so the next tx command doesn't race the setup.
    time.sleep(0.25)

    # Where the post-setup tail starts — skip the noisy init output by default.
    init_offset = log_path.stat().st_size

    offsets[pane_id] = {
        "tmux_id": tmux_pane_id,
        "tail_offset": init_offset,
        "continue_offset": None,
        "status": "idle",
        "created": now_iso(),
        "shell": shell_bin,
        # Tracks whether the marker hook is believed to be installed. Flipped
        # to False whenever a run finalises with exit_code=None (a hook-missing
        # event); a subsequent tx run / exec / sudo will auto-resend the hook
        # before sending its wrap (controlled by [defaults] auto_reinstall_hook).
        "hook_ok": True,
    }
    offsets.setdefault("_panes", {})[pane_id] = tmux_pane_id
    save_offsets(offsets)

    click.echo(pane_id)


# ----- ls -----
_LS_HEADERS = ("ID", "STATUS", "COMMAND", "PID")


_LS_MIN_WIDTHS = (10, 9, 24, 6)


def _truncate(s: str, width: int) -> str:
    if len(s) <= width:
        return s
    if width <= 1:
        return "…"
    return s[: width - 1] + "…"


@cli.command(
    name="ls",
    short_help="List managed panes.",
    help=(
        "List all panes managed by tx.\n\n"
        "--format table (default) is fixed-width and human-friendly.\n"
        "--format tsv emits one TAB-separated row per pane (no header).\n"
        "--format json emits a JSON array; ideal for orchestrators."
    ),
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "tsv", "json"], case_sensitive=False),
    default="table",
)
def cmd_ls(fmt: str) -> None:
    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    with offsets_lock():
        offsets = load_offsets()
        # Daily-ish lazy log sweep — best-effort, never aborts ls on failure.
        try:
            if maybe_sweep_aged_logs(offsets, cfg):
                save_offsets(offsets)
        except OSError:
            pass
        pane_ids = [k for k in offsets.keys() if not k.startswith("_")]
        if not pane_ids:
            click.echo("[empty: no panes — create one with 'tx new']")
            return
        server = get_server()
        rows = []
        for pid in pane_ids:
            finalize_runs(offsets, pid, max_history, cfg["defaults"])
            info = pane_state(server, offsets[pid], pid, cfg["defaults"])
            status = info["status"]
            cmd_text = info.get("current_command") or "-"
            pid_text = info.get("pid") or "-"
            # Promote "unread" if log has unread bytes and pane is otherwise idle.
            if status == "idle":
                log_path = pane_log_path(pid)
                tail_offset = int(offsets[pid].get("tail_offset", 0))
                file_size = log_path.stat().st_size if log_path.exists() else 0
                if file_size > tail_offset:
                    status = "unread"
            elif status == "dead":
                cmd_text = "-"
                pid_text = "-"
            rows.append((pid, status, cmd_text, pid_text))
        save_offsets(offsets)

    if fmt == "json":
        data = [
            {"id": pid, "status": st, "command": cmd, "pid": pidv}
            for pid, st, cmd, pidv in rows
        ]
        click.echo(json.dumps(data, indent=2))
        return

    if fmt == "tsv":
        for r in rows:
            click.echo("\t".join(r))
        return

    widths = [
        max(_LS_MIN_WIDTHS[i], max((len(_truncate(r[i], 80)) for r in rows), default=0))
        for i in range(4)
    ]
    # Truncate command column to a sensible max to avoid runaway widths.
    widths[2] = min(widths[2], 40)
    click.echo("  ".join(_LS_HEADERS[i].ljust(widths[i]) for i in range(4)))
    for r in rows:
        cells = [_truncate(r[i], widths[i]).ljust(widths[i]) for i in range(4)]
        click.echo("  ".join(cells))


# ----- kill -----
@cli.command(
    name="kill",
    short_help="Destroy a pane.",
    help=(
        "Destroy pane and stop logging. Log file is preserved.\n\n"
        "--signal selects how the foreground process is asked to exit:\n"
        "  hup    send C-d (EOF) — works when the pane is at a shell prompt\n"
        "  term   send C-c twice, then kill the tmux pane (default)\n"
        "  kill   kill the tmux pane immediately, no graceful interrupt"
    ),
)
@click.argument("pane")
@click.option(
    "--signal",
    "signal_kind",
    type=click.Choice(["hup", "term", "kill"], case_sensitive=False),
    default="term",
)
def cmd_kill(pane: str, signal_kind: str) -> None:
    offsets = load_offsets()
    state = require_pane(offsets, pane)
    tmux_id = state.get("tmux_id", "")
    server = get_server()
    tmux_pane = find_pane_anywhere(server, tmux_id) if tmux_id else None
    sig = signal_kind.lower()
    if tmux_pane is not None:
        if sig == "hup":
            # C-d: shell sees EOF on a prompt → exits; on a running cmd reading
            # stdin → closes the input stream.
            try:
                tmux_pane.send_keys("C-d", enter=False, suppress_history=False, literal=False)
                time.sleep(0.2)
            except Exception:
                pass
        elif sig == "term":
            try:
                tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)
                time.sleep(0.1)
                tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)
                time.sleep(0.1)
            except Exception:
                pass
        # 'kill' skips graceful — falls through to kill-pane below.
        stop_pipe_pane(tmux_pane)
        try:
            tmux_pane.kill()
        except Exception:
            try:
                tmux_pane.cmd("kill-pane")
            except Exception:
                pass
    offsets.pop(pane, None)
    if "_panes" in offsets:
        offsets["_panes"].pop(pane, None)
    save_offsets(offsets)
    click.echo(f"[killed: pane '{pane}' removed (signal={sig})]")


@cli.command(
    name="status",
    short_help="Print pane state and active/last run.",
    help="One-line snapshot of pane status, the active run if any, and the most recent completed run.",
)
@click.argument("pane")
def cmd_status(pane: str) -> None:
    cfg = load_config()
    session_name = cfg["defaults"]["tmux_session"]
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    with offsets_lock():
        offsets = load_offsets()
        require_pane(offsets, pane)
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        state = offsets[pane]
        server = get_server()
        info = pane_state(server, state, pane, cfg["defaults"])
        active = state.get("active_run")
        runs = state.get("runs") or []
    parts = [f"status={info['status']}"]
    if active:
        parts.append(f"active={active['id']} cmd='{(active.get('cmd') or '')[:50]}'")
    elif runs:
        last = runs[-1]
        parts.append(
            f"last={last['id']} exit={last.get('exit')} "
            f"duration={_duration_str(last.get('started', ''), last.get('ended'))}"
        )
    if info.get("current_command"):
        parts.append(f"fg={info['current_command']}")
    if info.get("waiting_pattern"):
        parts.append(f"waiting='{info['waiting_pattern']}'")
    clients = tmux_attached_clients(server, session_name)
    parts.append(f"attached={'yes' if clients else 'no'}")
    click.echo(" ".join(parts))


@cli.command(
    name="info",
    short_help="Print everything an orchestrator needs to know about a pane.",
    help=(
        "Print a human-readable block of facts about a pane:\n"
        "  state, foreground process, cwd, current run, last run, buffer + log\n"
        "  bytes, tail offset, attached tmux clients, created timestamp.\n\n"
        "cwd lookup uses /proc on Linux and lsof on macOS; '?' if neither works."
    ),
)
@click.argument("pane")
def cmd_info(pane: str) -> None:
    cfg = load_config()
    session_name = cfg["defaults"]["tmux_session"]
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    with offsets_lock():
        offsets = load_offsets()
        state = require_pane(offsets, pane)
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        state = offsets[pane]
        server = get_server()
        info = pane_state(server, state, pane, cfg["defaults"])
    log_path = pane_log_path(pane)
    log_bytes = log_path.stat().st_size if log_path.exists() else 0
    tail_offset = int(state.get("tail_offset", 0))
    unread = max(0, log_bytes - tail_offset)

    lines: list[str] = []
    def add(k: str, v: str) -> None:
        lines.append(f"{(k + ':').ljust(16)}{v}")

    add("pane", pane)
    add("session", session_name)
    add("state", info["status"])

    shell_cmd = info.get("current_command") or "?"
    shell_pid_str = info.get("pid") or "?"
    add("shell", f"{shell_cmd} (pid {shell_pid_str})")

    fg_label = "?"
    shell_pid_int: int | None = None
    if shell_pid_str.isdigit():
        shell_pid_int = int(shell_pid_str)
        fg = walk_foreground(shell_pid_int)
        if fg is None:
            fg_label = f"{shell_cmd} (pid {shell_pid_str})"
        else:
            fg_pid, fg_name = fg
            fg_label = f"{fg_name} (pid {fg_pid}, parent {shell_pid_str})"
    add("foreground", fg_label)

    active = state.get("active_run")
    if active:
        run_id = active.get("id", "?")
        add("current_run", run_id)
        elapsed = _running_for_seconds(active)
        if elapsed is not None:
            add("running_for", f"{elapsed:.1f}s")
    runs = state.get("runs") or []
    if runs:
        last = runs[-1]
        cmd_text = (last.get("cmd") or "")[:80]
        dur = _duration_str(last.get("started", ""), last.get("ended"))
        exit_v = last.get("exit")
        exit_label = str(exit_v) if exit_v is not None else "?"
        add("last_run", f"{last.get('id', '?')} exit={exit_label} duration={dur} cmd='{cmd_text}'")

    cwd = read_pane_cwd(shell_pid_int)
    add("cwd", cwd or "?")

    add("buffer_bytes", str(log_bytes))
    add("tail_offset", str(tail_offset))
    add("unread_bytes", str(unread))
    add("log_path", str(log_path))
    add("log_bytes", str(log_bytes))

    clients = tmux_attached_clients(server, session_name)
    if clients:
        add("user_attached", f"yes ({', '.join(clients)})")
    else:
        add("user_attached", "no")

    add("created", state.get("created") or "?")

    if state.get("paused_at"):
        add("paused_at", str(state["paused_at"]))

    bookmarks = state.get("bookmarks") or {}
    if bookmarks:
        bm = ", ".join(f"{k}={v}" for k, v in sorted(bookmarks.items()))
        add("bookmarks", bm)

    click.echo("\n".join(lines))


# ----- restart (dead pane revival) -----
@cli.command(
    name="restart",
    short_help="Re-attach a fresh tmux pane to a dead pane id, preserving the log.",
    help=(
        "When 'tx status <pane>' reports state=dead, this re-allocates a new tmux\n"
        "pane under the same tx id, re-attaches pipe-pane to the existing log file\n"
        "(append mode) so prior output is preserved, and re-installs the v2 marker\n"
        "hook. A divider line is appended to mark the seam."
    ),
)
@click.argument("pane")
def cmd_restart(pane: str) -> None:
    cfg = load_config()
    session_name = cfg["defaults"]["tmux_session"]
    with offsets_lock():
        offsets = load_offsets()
        state = require_pane(offsets, pane)
        server = get_server()
        tmux_id = state.get("tmux_id", "")
        existing = find_pane_anywhere(server, tmux_id) if tmux_id else None
        if existing is not None:
            err(f"pane '{pane}' is alive (tmux pane {tmux_id}); restart is only for dead panes")

        session = get_or_create_session(server, session_name)
        claimed = {
            v.get("tmux_id", "")
            for k, v in offsets.items()
            if not k.startswith("_") and isinstance(v, dict) and k != pane
        }
        new_tmux_id, _adopted = allocate_pane(session, claimed, pane)
        new_pane = find_pane_anywhere(server, new_tmux_id)
        if new_pane is None:
            err(f"could not locate newly created tmux pane '{new_tmux_id}'")

        log_path = pane_log_path(pane)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate the log if it exceeds the configured threshold before we
        # re-attach. The old shell's full history stays in <id>.log.1.
        maybe_rotate_log(log_path, cfg)
        log_path.touch(exist_ok=True)
        try:
            with open(log_path, "ab") as f:
                f.write(f"\n--- tx restart at {now_iso()} ---\n".encode("utf-8"))
        except OSError:
            pass
        # pipe-pane in append mode (we never truncate the existing log).
        new_pane.cmd("pipe-pane", "-o", f"cat >> {log_path}")

        # Re-bump history-limit (per-pane setting, doesn't carry over).
        try:
            history_limit = int(cfg["defaults"].get("history_limit", 100000))
            new_pane.cmd("set-option", "-p", "history-limit", str(history_limit))
        except Exception:
            pass

        # Re-install the marker hook (use the recorded shell if known).
        init_snippet = shell_init_setup_for(state.get("shell"))
        try:
            new_pane.send_keys(init_snippet, enter=True, suppress_history=False, literal=True)
        except Exception:
            pass
        time.sleep(0.25)

        state["tmux_id"] = new_tmux_id
        state["tail_offset"] = log_path.stat().st_size
        state["active_run"] = None
        state.pop("pending_lines", None)
        state.pop("dump_pending_lines", None)
        state["continue_offset"] = None
        state["paused_at"] = None
        state["hook_ok"] = True
        offsets[pane] = state
        offsets.setdefault("_panes", {})[pane] = new_tmux_id
        save_offsets(offsets)
    click.echo(f"[restarted: pane '{pane}' attached to fresh tmux pane {new_tmux_id}]")

