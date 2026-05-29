"""tx_core.commands.input — input-sending commands (send / key / paste / handoff / resume / send-secret / sudo)

Extracted verbatim from the monolithic `tx-pane` script during the modular
refactor. Each `@cli.command()` registers itself on the shared `cli`
root group on import.
"""

from __future__ import annotations

from tx_core.commands._common import *  # noqa: F401,F403


@cli.command(name="send", short_help="Send raw text (no Enter).", help="Send text without Enter. Enforces allowlist and confirm-pattern policy.")
@click.argument("pane")
@click.argument("text")
def cmd_send(pane: str, text: str) -> None:
    cfg = load_config()
    offsets = load_offsets()
    state = require_pane(offsets, pane)
    if state.get("paused_at"):
        err(f"pane '{pane}' is paused (handoff); run 'tx-pane resume {pane}' first")
    server = get_server()
    tmux_pane = find_pane_anywhere(server, state.get("tmux_id", ""))
    if tmux_pane is None:
        err(f"pane '{pane}' tmux pane missing — has it been killed externally?")
    offender = check_allowlist(text, cfg, pane_id=pane)
    if offender is not None:
        err(f"'{offender}' not in command_allowlist — edit ~/.tx-pane/config.toml")
    check_confirm(text, cfg, yes=False)
    tmux_pane.send_keys(text, enter=False, suppress_history=False, literal=True)


# ----- key -----
KEY_ALIASES = {
    "enter": "Enter",
    "c-c": "C-c",
    "c-d": "C-d",
    "c-z": "C-z",
    "esc": "Escape",
    "escape": "Escape",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "tab": "Tab",
}


@cli.command(
    name="key",
    short_help="Send special keys in sequence.",
    help="Send one or more special keys in sequence.\n\nSupported: Enter  C-c  C-d  C-z  Esc  Up  Down  Left  Right  Tab",
)
@click.argument("pane")
@click.argument("keys", nargs=-1, required=True)
def cmd_key(pane: str, keys: tuple[str, ...]) -> None:
    offsets = load_offsets()
    state = require_pane(offsets, pane)
    if state.get("paused_at"):
        err(f"pane '{pane}' is paused (handoff); run 'tx-pane resume {pane}' first")
    server = get_server()
    tmux_pane = find_pane_anywhere(server, state.get("tmux_id", ""))
    if tmux_pane is None:
        err(f"pane '{pane}' tmux pane missing — has it been killed externally?")
    for raw_key in keys:
        canonical = KEY_ALIASES.get(raw_key.lower(), raw_key)
        if canonical not in KEY_ALIASES.values() and raw_key.lower() not in KEY_ALIASES:
            err(f"unsupported key '{raw_key}' — supported: Enter, C-c, C-d, C-z, Esc, Up, Down, Left, Right, Tab")
        tmux_pane.send_keys(canonical, enter=False, suppress_history=False, literal=False)
        if canonical == "Enter":
            time.sleep(0.05)


# ----- paste (Stage 3 bracketed paste) -----
@cli.command(
    name="paste",
    short_help="Paste content into the pane using tmux's bracketed-paste mode.",
    help=(
        "Read content from --file (or stdin) and paste it into the pane via\n"
        "tmux's bracketed-paste mode (load-buffer + paste-buffer -p). The shell\n"
        "receives the bytes atomically: no per-line shell evaluation, so heredocs,\n"
        "JSON blobs, and small scripts arrive intact.\n\n"
        "Refuses if the pane is busy or paused. The tmux buffer is auto-deleted\n"
        "after paste (-d) so it doesn't accumulate in the buffer stack."
    ),
)
@click.argument("pane")
@click.option("--file", "file_path", type=click.Path(dir_okay=False), default=None, help="read content from this file (default: stdin)")
def cmd_paste(pane: str, file_path: str | None) -> None:
    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))

    if file_path is not None:
        try:
            content = Path(file_path).read_text()
        except OSError as e:
            err(f"cannot read --file '{file_path}': {e}")
    else:
        if sys.stdin.isatty():
            err("paste needs content — pipe via stdin or pass --file <path>")
        content = sys.stdin.read()
    if not content:
        err("paste: content was empty")

    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        if state.get("paused_at"):
            err(f"pane '{pane}' is paused (handoff); run 'tx-pane resume {pane}' first")
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        info = pane_state(server, offsets[pane], pane, cfg["defaults"])
        if info["status"] in ("running", "tui"):
            err(busy_error_message(pane, info))
        if info["status"] == "dead":
            err(f"pane '{pane}' shell is dead — recreate with 'tx-pane kill {pane}' then 'tx-pane new {pane}'")

    # Allowlist check uses the first non-empty line as the "command".
    first_line = next((ln for ln in content.splitlines() if ln.strip()), "")
    if first_line:
        offender = check_allowlist(first_line, cfg, pane_id=pane)
        if offender is not None:
            err(f"'{offender}' not in command_allowlist — edit ~/.tx-pane/config.toml")

    # Use tmux's load-buffer + paste-buffer -p (bracketed) so the shell treats
    # the content atomically. Write via a temp file so binary-clean bytes pass
    # through libtmux's argv encoding unchanged. -b names the buffer so we can
    # target it precisely and -d deletes it after paste.
    buf_name = f"txpaste-{secrets.token_hex(4)}"
    fd, tmp_name = tempfile.mkstemp(prefix=".txpaste-", suffix=".buf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode("utf-8", errors="replace"))
        try:
            server.cmd("load-buffer", "-b", buf_name, tmp_name)
        except Exception as e:
            err(f"tmux load-buffer failed: {e}")
        try:
            tmux_pane.cmd("paste-buffer", "-d", "-p", "-b", buf_name, "-t", tmux_pane.pane_id)
        except Exception as e:
            # Best-effort cleanup of the buffer if paste failed.
            try:
                server.cmd("delete-buffer", "-b", buf_name)
            except Exception:
                pass
            err(f"tmux paste-buffer failed: {e}")
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    n = len(content.encode("utf-8", errors="replace"))
    click.echo(f"[pasted: {n} bytes via tmux buffer '{buf_name}']")


# ----- handoff / resume -----
@cli.command(
    name="handoff",
    short_help="Pause tx-pane control so the user can type directly.",
    help=(
        "Pause tx-pane control of this pane. Subsequent tx-pane run / tx-pane exec / tx-pane send /\n"
        "tx-pane key calls refuse with an error pointing at tx-pane resume.\n\n"
        "pipe-pane is stopped while paused; bytes the user types are not\n"
        "captured in the on-disk log. tx-pane resume re-attaches pipe-pane in append\n"
        "mode and writes a divider so the seam is visible in post-mortem reads."
    ),
)
@click.argument("pane")
def cmd_handoff(pane: str) -> None:
    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        state = offsets[pane]
        if state.get("paused_at"):
            err(f"pane '{pane}' is already paused (since {state['paused_at']}); run 'tx-pane resume {pane}'")
        ts = now_iso()
        # Stop the pipe so unsanctioned typing isn't logged.
        stop_pipe_pane(tmux_pane)
        # Append a divider directly to the log so the seam is obvious.
        try:
            with open(log_path, "ab") as f:
                f.write(f"\n--- tx-pane handoff at {ts} ---\n".encode("utf-8"))
        except OSError:
            pass
        state["paused_at"] = ts
        offsets[pane] = state
        save_offsets(offsets)
    click.echo(f"[paused: {pane} — user has control. Resume with 'tx-pane resume {pane}']")


@cli.command(
    name="resume",
    short_help="Resume tx-pane control after a handoff.",
    help=(
        "Re-attach pipe-pane (append mode) and clear the paused state.\n\n"
        "tail_offset is refreshed to the end of the log so reads don't bring\n"
        "back the gap that was missed during handoff."
    ),
)
@click.argument("pane")
def cmd_resume(pane: str) -> None:
    cfg = load_config()
    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        if not state.get("paused_at"):
            err(f"pane '{pane}' is not paused — nothing to resume")
        ts = now_iso()
        # Opportunistic rotation on every pipe-pane (re)start so long-lived
        # panes don't accumulate single huge logs.
        maybe_rotate_log(log_path, cfg)
        try:
            with open(log_path, "ab") as f:
                f.write(f"--- tx-pane resume at {ts} ---\n".encode("utf-8"))
        except OSError:
            pass
        start_pipe_pane(tmux_pane, log_path)
        file_size = log_path.stat().st_size
        state["paused_at"] = None
        state["tail_offset"] = file_size
        state.pop("pending_lines", None)
        state["continue_offset"] = None
        offsets[pane] = state
        save_offsets(offsets)
    click.echo(f"[resumed: {pane} — tx-pane control restored]")


# ----- send-secret -----
@cli.command(
    name="send-secret",
    short_help="Send a secret from stdin (NEVER argv) to the pane.",
    help=(
        "Read text from STDIN and send it to the pane, then append a redaction\n"
        "placeholder to the log instead of the actual bytes.\n\n"
        "Reading from stdin (not argv) ensures the secret never appears in 'ps'.\n"
        "pipe-pane is briefly stopped during the send so the bytes are not\n"
        "captured in the log.\n\n"
        "Use --enter to append Enter after the secret (typical for password prompts)."
    ),
)
@click.argument("pane")
@click.option("--enter", is_flag=True, default=False, help="append Enter after sending")
def cmd_send_secret(pane: str, enter: bool) -> None:
    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        if state.get("paused_at"):
            err(f"pane '{pane}' is paused (handoff); run 'tx-pane resume {pane}' first")
    # Read bytes from stdin, never from argv.
    if sys.stdin.isatty():
        err("send-secret requires stdin (pipe a value in; do not pass it as an argument)")
    secret = sys.stdin.read()
    if secret.endswith("\n") and not enter:
        # Strip a trailing newline so callers can use `echo $PW | tx-pane send-secret`
        # without inadvertently submitting. They opt in with --enter.
        secret = secret[:-1]
    if not secret:
        err("send-secret: stdin was empty")

    n_bytes = len(secret.encode("utf-8", errors="replace"))
    # Briefly stop pipe-pane so the secret bytes don't land in the log.
    stop_pipe_pane(tmux_pane)
    try:
        tmux_pane.send_keys(secret, enter=enter, suppress_history=False, literal=True)
    except Exception as e:
        # Best-effort to re-attach the log before re-raising.
        start_pipe_pane(tmux_pane, log_path)
        err(f"send-secret failed: {e}")
    # Append a redaction placeholder directly to the log.
    try:
        placeholder = f"[redacted: send-secret {n_bytes} bytes]"
        if enter:
            placeholder += " (with Enter)"
        with open(log_path, "ab") as f:
            f.write((placeholder + "\n").encode("utf-8"))
    except OSError:
        pass
    # Brief settle, then resume pipe.
    time.sleep(0.1)
    start_pipe_pane(tmux_pane, log_path)
    click.echo(f"[sent: {n_bytes} bytes redacted in log]")


# ----- sudo (Stage 3 convenience wrapper) -----
@cli.command(
    name="sudo",
    short_help="Run a command with sudo, prompting the local user for the password.",
    help=(
        "Convenience wrapper: prompts for the sudo password on the local TTY,\n"
        "sends `sudo -S -p '' <cmd>` to the pane, pipes the password via the\n"
        "send-secret path (so it never lands in the log), then waits for the\n"
        "run to complete and returns its output like `tx-pane run`.\n\n"
        "Requires an interactive TTY for the password prompt. Agents driving\n"
        "tx-pane without a TTY should use `tx-pane exec ... \"sudo -S -p '' ...\"` plus\n"
        "`tx-pane send-secret` manually instead."
    ),
)
@click.argument("pane")
@click.argument("cmd")
@click.option("--max", "max_lines", type=int, default=None, help="cap output at N lines")
@click.option("--timeout", "timeout", type=float, default=None, help="override wait timeout in seconds")
@click.option("--no-strip", is_flag=True, default=False, help="disable whitespace collapsing")
@click.option("--keep-ansi", "keep_ansi", is_flag=True, default=False, help="do not strip ANSI escape sequences from output")
@click.option("--yes", "yes", is_flag=True, default=False, help="skip confirm-pattern prompt")
def cmd_sudo(
    pane: str,
    cmd: str,
    max_lines: int | None,
    timeout: float | None,
    no_strip: bool,
    keep_ansi: bool,
    yes: bool,
) -> None:
    import getpass

    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    max_lines = max_lines if max_lines is not None else int(cfg["defaults"]["max_lines"])
    timeout = timeout if timeout is not None else float(cfg["defaults"]["timeout"])
    strip_blanks = bool(cfg["defaults"]["strip"]) and not no_strip
    strip_ansi_flag = resolve_strip_ansi(cfg, keep_ansi)
    keep_ansi_resolved = not strip_ansi_flag

    # Build the sudo command. -S reads password from stdin; -p '' suppresses
    # sudo's own prompt so the pane log stays tidy.
    wrapped_cmd = f"sudo -S -p '' {cmd}"

    offender = check_allowlist(wrapped_cmd, cfg, pane_id=pane)
    if offender is not None:
        err(f"'{offender}' not in command_allowlist — edit ~/.tx-pane/config.toml")
    check_confirm(wrapped_cmd, cfg, yes=yes)

    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        if state.get("paused_at"):
            err(f"pane '{pane}' is paused (handoff); run 'tx-pane resume {pane}' first")
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        info = pane_state(server, offsets[pane], pane, cfg["defaults"])
        if info["status"] in ("running", "tui"):
            err(busy_error_message(pane, info))
        if info["status"] == "dead":
            err(f"pane '{pane}' shell is dead — recreate with 'tx-pane kill {pane}' then 'tx-pane new {pane}'")
        save_offsets(offsets)

    if not sys.stdin.isatty():
        err(
            "tx-pane sudo requires an interactive TTY for the password prompt. "
            "Without a TTY, use 'tx-pane exec <pane> \"sudo -S -p \\\"\\\" <cmd>\"' "
            "and feed the password via 'tx-pane send-secret <pane> --enter'."
        )

    try:
        password = getpass.getpass(f"[sudo] password for pane '{pane}': ")
    except (EOFError, KeyboardInterrupt):
        err("sudo: aborted before password could be read")
    if not password:
        err("sudo: empty password — refusing to send")

    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        if state.get("paused_at"):
            err(f"pane '{pane}' is paused (handoff); run 'tx-pane resume {pane}' first")
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        info = pane_state(server, offsets[pane], pane, cfg["defaults"])
        if info["status"] in ("running", "tui"):
            err(busy_error_message(pane, info))
        if info["status"] == "dead":
            err(f"pane '{pane}' shell is dead — recreate with 'tx-pane kill {pane}' then 'tx-pane new {pane}'")

        # Re-check before sending in case policy or pane state changed while
        # the password prompt was open.
        offender = check_allowlist(wrapped_cmd, cfg, pane_id=pane)
        if offender is not None:
            err(f"'{offender}' not in command_allowlist — edit ~/.tx-pane/config.toml")
        check_confirm(wrapped_cmd, cfg, yes=yes)

        run_id = _start_run(tmux_pane, log_path, wrapped_cmd, timeout, offsets, pane, cfg=cfg)
        start_offset = int(offsets[pane]["active_run"]["start_offset"])
        save_offsets(offsets)

    # Give sudo a moment to start and emit its password prompt.
    time.sleep(0.3)

    # Feed the password via the send-secret pattern: stop pipe-pane, send,
    # write a redaction placeholder, restart pipe-pane.
    n_bytes = len(password.encode("utf-8", errors="replace"))
    stop_pipe_pane(tmux_pane)
    try:
        tmux_pane.send_keys(password, enter=True, suppress_history=False, literal=True)
    except Exception as e:
        start_pipe_pane(tmux_pane, log_path)
        err(f"sudo: send-keys failed: {e}")
    try:
        with open(log_path, "ab") as f:
            f.write(f"[redacted: sudo password {n_bytes} bytes (with Enter)]\n".encode("utf-8"))
    except OSError:
        pass
    time.sleep(0.1)
    start_pipe_pane(tmux_pane, log_path)

    # Wait for the run to finish.
    found, exit_code, end_offset, idle_age = wait_for_marker(
        log_path, start_offset, run_id, timeout, cfg["defaults"]
    )

    with offsets_lock():
        offsets = load_offsets()
        state = offsets[pane]
        if found:
            record_run_end(state, run_id, exit_code, end_offset, max_history)
            offsets[pane] = state
            kept = _render_run_output(
                log_path, start_offset, end_offset, strip_blanks,
                keep_ansi=keep_ansi_resolved, redact_cfg=cfg,
            )
            shown = kept[:max_lines]
            remainder = kept[max_lines:]
            state["tail_offset"] = end_offset
            if remainder:
                state["pending_lines"] = remainder
            else:
                state.pop("pending_lines", None)
            offsets[pane] = state
            save_offsets(offsets)
            exit_label = str(exit_code) if exit_code is not None else "?"
            out_parts = [f"[exit:{exit_label}]"] + shown
            if remainder:
                out_parts.append(
                    f"[truncated: {len(remainder)} lines remain — run: tx-pane tail {pane} --continue]"
                )
            click.echo("\n".join(out_parts), color=keep_ansi_resolved or None)
        else:
            info = pane_state(server, state, pane, cfg["defaults"])
            msg = truthful_timeout_message(pane, info, run_id, timeout, log_path, idle_age)
            save_offsets(offsets)
            click.echo(f"[timeout: {msg}]")
