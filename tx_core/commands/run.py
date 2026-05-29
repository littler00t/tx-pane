"""tx_core.commands.run — run-execution commands (run / exec / wait-run / output / runs / kill-run / stream)

Extracted verbatim from the monolithic `tx` script during the modular
refactor. Each `@cli.command()` registers itself on the shared `cli`
root group on import.
"""

from __future__ import annotations

from tx_core.commands._common import *  # noqa: F401,F403


def _interrupt_for_bound(
    tmux_pane,
    log_path: Path,
    start_offset: int,
    run_id: str,
    cfg_defaults: dict[str, Any],
    timeout: float = 2.0,
) -> tuple[bool, int | None, int]:
    """Send C-c and return whether the run reached a marker/prompt fallback."""
    try:
        tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)
    except Exception:
        pass
    time.sleep(0.2)
    found, exit_code, end_offset, _idle = wait_for_marker(
        log_path, start_offset, run_id, timeout=timeout, cfg_defaults=cfg_defaults
    )
    return found, exit_code, end_offset


def _bound_pending_note(run_id: str, reason: str, matched: str | None) -> str:
    detail = f" matched '{matched}'" if matched else " matched"
    return (
        f"cancellation-pending: {reason}{detail}; C-c sent to run '{run_id}', "
        f"but no end marker or prompt fallback was observed. The run remains active; "
        f"use 'tx wait-run' or 'tx kill-run' to finish it."
    )


@cli.command(
    name="run",
    short_help="Send command, wait for marker, return new output.",
    help=(
        "Send <cmd> + Enter. Wait for the run's end marker. Return new output.\n\n"
        "Equivalent to `tx exec` followed by `tx wait-run` on the returned id.\n"
        "If the pane is busy, refuses by default — use --queue / --stdin / --kill-and-run."
    ),
)
@click.argument("pane")
@click.argument("cmd")
@click.option("--max", "max_lines", type=int, default=None, help="cap output at N lines")
@click.option("--timeout", "timeout", type=float, default=None, help="override idle timeout in seconds")
@click.option("--no-strip", is_flag=True, default=False, help="disable whitespace collapsing")
@click.option("--queue", is_flag=True, default=False, help="wait for the pane to become idle before sending")
@click.option("--max-wait", "max_wait", type=float, default=None, help="bound the --queue wait (default = --timeout)")
@click.option("--stdin", "stdin_mode", is_flag=True, default=False, help="feed text to the running command (not a new run)")
@click.option("--no-enter", is_flag=True, default=False, help="with --stdin, omit the trailing Enter")
@click.option("--kill-and-run", "kill_and_run", is_flag=True, default=False, help="send C-c, wait briefly for idle, then run")
@click.option(
    "--on-timeout",
    "on_timeout",
    type=click.Choice(["report", "cancel", "kill"], case_sensitive=False),
    default="report",
    help="behavior on marker timeout: report (default), cancel (C-c then re-check), kill (C-c twice + kill-pane)",
)
@click.option("--keep-ansi", "keep_ansi", is_flag=True, default=False, help="do not strip ANSI escape sequences from output")
@click.option("--json", "as_json", is_flag=True, default=False, help="emit a single JSON record instead of plain text")
@click.option("--yes", "yes", is_flag=True, default=False, help="skip confirm-pattern prompt (for non-interactive callers)")
@click.option("--wait-for", "wait_for", type=str, default=None, help="return early (exit=0) when this regex matches output")
@click.option("--fail-for", "fail_for", type=str, default=None, help="return early (exit=1) when this regex matches output")
@_compact_options
def cmd_run(
    pane: str,
    cmd: str,
    max_lines: int | None,
    timeout: float | None,
    no_strip: bool,
    queue: bool,
    max_wait: float | None,
    stdin_mode: bool,
    no_enter: bool,
    kill_and_run: bool,
    on_timeout: str,
    keep_ansi: bool,
    as_json: bool,
    yes: bool,
    wait_for: str | None,
    fail_for: str | None,
    raw_flag: bool,
    terse_flag: bool,
    token_budget_flag: int | None,
    no_strip_banners_flag: bool,
    no_collapse_repeats_flag: bool,
    no_normalize_flag: bool,
) -> None:
    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    max_lines = max_lines if max_lines is not None else int(cfg["defaults"]["max_lines"])
    timeout = timeout if timeout is not None else float(cfg["defaults"]["timeout"])
    max_wait = max_wait if max_wait is not None else timeout
    strip_blanks = bool(cfg["defaults"]["strip"]) and not no_strip
    strip_ansi_flag = resolve_strip_ansi(cfg, keep_ansi)
    keep_ansi_resolved = not strip_ansi_flag

    wait_for_re = None
    fail_for_re = None
    if wait_for is not None:
        try:
            wait_for_re = re.compile(wait_for)
        except re.error as e:
            err(f"invalid --wait-for regex: {e}")
    if fail_for is not None:
        try:
            fail_for_re = re.compile(fail_for)
        except re.error as e:
            err(f"invalid --fail-for regex: {e}")

    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)

        if state.get("paused_at"):
            err(f"pane '{pane}' is paused (handoff); run 'tx resume {pane}' first")

        # --stdin: send text to a running pane; no run-id, no marker.
        if stdin_mode:
            if queue or kill_and_run:
                err("--stdin is incompatible with --queue / --kill-and-run")
            if as_json or wait_for_re or fail_for_re:
                err("--stdin is incompatible with --json / --wait-for / --fail-for")
            finalize_runs(offsets, pane, max_history, cfg["defaults"])
            info = pane_state(server, offsets[pane], pane, cfg["defaults"])
            if info["status"] == "idle":
                err(f"pane '{pane}' is idle — nothing to send stdin to; use plain 'tx run' instead")
            tmux_pane.send_keys(cmd, enter=not no_enter, suppress_history=False, literal=True)
            save_offsets(offsets)
            return

        # Refuse-on-busy resolution.
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        info = pane_state(server, offsets[pane], pane, cfg["defaults"])
        if info["status"] in ("running", "tui"):
            if queue:
                info = poll_until_idle(server, offsets, pane, max_wait, cfg_defaults=cfg["defaults"])
                if info["status"] not in ("idle", "dead"):
                    err(f"--queue timed out after {int(max_wait)}s; pane still {info['status']}")
            elif kill_and_run:
                try:
                    tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)
                except Exception:
                    pass
                time.sleep(0.2)
                info = poll_until_idle(server, offsets, pane, min(5.0, max_wait), cfg_defaults=cfg["defaults"])
                if info["status"] not in ("idle", "dead"):
                    err(f"--kill-and-run could not return pane to idle within {int(min(5.0, max_wait))}s")
            else:
                err(busy_error_message(pane, info))

        if info["status"] == "dead":
            err(f"pane '{pane}' shell is dead — recreate with 'tx kill {pane}' then 'tx new {pane}'")

        # Allowlist check (run path only).
        offender = check_allowlist(cmd, cfg, pane_id=pane)
        if offender is not None:
            err(f"'{offender}' not in command_allowlist — edit ~/.tx/config.toml")
        # Confirm-pattern check.
        check_confirm(cmd, cfg, yes)

        run_id = _start_run(tmux_pane, log_path, cmd, max_wait, offsets, pane, cfg=cfg)
        start_offset = int(offsets[pane]["active_run"]["start_offset"])
        started_iso = (offsets[pane]["active_run"] or {}).get("started")
        save_offsets(offsets)

    # Now wait for the marker (no lock — long blocking call). cfg_defaults
    # enables the prompt-pattern fallback for nested-shell scenarios where
    # the hook isn't installed (ssh / sudo -i / docker exec / …).
    cancellation_pending_note: str | None = None
    if wait_for_re is not None or fail_for_re is not None:
        reason, exit_code, end_offset, idle_age, matched = wait_for_marker_or_bound(
            log_path, start_offset, run_id, timeout, cfg["defaults"],
            wait_for_re=wait_for_re, fail_for_re=fail_for_re,
        )
        if reason == "marker":
            found = True
        elif reason in ("wait-for", "fail-for"):
            # Pattern fired before marker. Interrupt the run, but only
            # finalize if the marker or prompt fallback proves it ended.
            found2, _exit2, end2 = _interrupt_for_bound(
                tmux_pane, log_path, start_offset, run_id, cfg["defaults"]
            )
            if found2:
                end_offset = end2
                found = True
                # Synthesize exit based on which pattern fired (overrides shell exit).
                exit_code = 0 if reason == "wait-for" else 1
                pattern_note = f"{reason}: matched '{matched}'"
            else:
                found = False
                pattern_note = None
                cancellation_pending_note = _bound_pending_note(run_id, reason, matched)
        else:
            found = False
            pattern_note = None
    else:
        found, exit_code, end_offset, idle_age = wait_for_marker(
            log_path, start_offset, run_id, timeout, cfg["defaults"]
        )
        pattern_note = None

    extra_note: str | None = None
    if not found and on_timeout != "report":
        found, exit_code, end_offset, idle_age, extra_note = _apply_on_timeout(
            on_timeout, tmux_pane, log_path, start_offset, run_id, cfg["defaults"]
        )

    with offsets_lock():
        offsets = load_offsets()
        state = offsets[pane]
        if found:
            record_run_end(state, run_id, exit_code, end_offset, max_history)
            offsets[pane] = state
            per_call_mode = _per_call_compact_mode(raw_flag, terse_flag)
            compact_ctx = _build_compact_ctx(
                cfg, state, pane, cmd, run_id, per_call_mode,
                per_call_no_normalize=no_normalize_flag,
                per_call_no_strip_banners=no_strip_banners_flag,
                per_call_no_collapse_repeats=no_collapse_repeats_flag,
            )
            if compact_ctx is not None and token_budget_flag is not None:
                compact_ctx.token_budget = token_budget_flag
            kept = _render_run_output(
                log_path, start_offset, end_offset, strip_blanks,
                keep_ansi=not strip_ansi_flag, redact_cfg=cfg,
                compact_ctx=compact_ctx,
                pane_state=state,
            )
            # Append the compaction footer (single line) if compact() emitted one.
            if compact_ctx is not None:
                _res = getattr(compact_ctx, "result", None)
                if _res is not None and _res.footer:
                    kept.append(_res.footer)
            shown = kept[:max_lines]
            remainder = kept[max_lines:]
            state["tail_offset"] = end_offset
            if remainder:
                state["pending_lines"] = remainder
            else:
                state.pop("pending_lines", None)
            offsets[pane] = state
            save_offsets(offsets)
            ended_iso = None
            for r in reversed(state.get("runs") or []):
                if r.get("id") == run_id:
                    ended_iso = r.get("ended")
                    break
            if as_json:
                notes: list[str] = []
                if exit_code is None:
                    notes.append("hook-missing: no marker observed but prompt returned")
                if extra_note:
                    notes.append(f"on-timeout: {extra_note}")
                if pattern_note:
                    notes.append(pattern_note)
                _emit_run_json(
                    pane, run_id, cmd, started_iso, ended_iso, exit_code,
                    shown, bool(remainder), notes,
                )
            else:
                exit_label = str(exit_code) if exit_code is not None else "?"
                out_parts: list[str] = [f"[exit:{exit_label}]"] + shown
                if exit_code is None:
                    out_parts.append(
                        f"[hook-missing: no marker observed but prompt returned — exit code unknown. "
                        f"If this pane runs a nested shell (ssh / sudo -i / etc.), run "
                        f"'tx hook-install {pane}' to enable marker tracking there.]"
                    )
                if extra_note:
                    out_parts.append(f"[on-timeout: {extra_note}]")
                if pattern_note:
                    out_parts.append(f"[{pattern_note}]")
                if remainder:
                    out_parts.append(
                        f"[truncated: {len(remainder)} lines remain — run: tx tail {pane} --continue]"
                    )
                click.echo("\n".join(out_parts), color=keep_ansi_resolved or None)
        else:
            save_offsets(offsets)
            if cancellation_pending_note is not None:
                if as_json:
                    _emit_run_json(
                        pane, run_id, cmd, started_iso, None, None,
                        [], False, [cancellation_pending_note],
                    )
                else:
                    click.echo(f"[{cancellation_pending_note}]")
                return
            info = pane_state(server, state, pane, cfg["defaults"])
            msg = truthful_timeout_message(pane, info, run_id, timeout, log_path, idle_age)
            if as_json:
                notes = [f"timeout: {msg}"]
                if extra_note:
                    notes.append(f"on-timeout: {extra_note}")
                _emit_run_json(
                    pane, run_id, cmd, started_iso, None, None,
                    [], False, notes,
                )
            else:
                note = f" [on-timeout: {extra_note}]" if extra_note else ""
                click.echo(f"[timeout: {msg}]{note}")


# ----- exec / wait-run / output / runs / kill-run / status -----

@cli.command(
    name="exec",
    short_help="Send command, return run-id immediately (async).",
    help=(
        "Send <cmd> + Enter, record an active run, and print the run-id.\n\n"
        "Use 'tx wait-run' to block until completion, or 'tx output' to fetch\n"
        "its output later. Refuses on a busy pane (same options as `tx run`)."
    ),
)
@click.argument("pane")
@click.argument("cmd")
@click.option("--timeout", "timeout", type=float, default=None, help="default timeout for tx wait-run; recorded with the run")
@click.option("--queue", is_flag=True, default=False)
@click.option("--max-wait", "max_wait", type=float, default=None)
@click.option("--kill-and-run", "kill_and_run", is_flag=True, default=False)
@click.option("--json", "as_json", is_flag=True, default=False, help="emit a JSON record describing the started run")
@click.option("--yes", "yes", is_flag=True, default=False, help="skip confirm-pattern prompt (for non-interactive callers)")
def cmd_exec(
    pane: str,
    cmd: str,
    timeout: float | None,
    queue: bool,
    max_wait: float | None,
    kill_and_run: bool,
    as_json: bool,
    yes: bool,
) -> None:
    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    timeout = timeout if timeout is not None else float(cfg["defaults"]["timeout"])
    max_wait = max_wait if max_wait is not None else timeout

    with offsets_lock():
        offsets = load_offsets()
        _state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        if _state.get("paused_at"):
            err(f"pane '{pane}' is paused (handoff); run 'tx resume {pane}' first")
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        info = pane_state(server, offsets[pane], pane, cfg["defaults"])
        if info["status"] in ("running", "tui"):
            if queue:
                info = poll_until_idle(server, offsets, pane, max_wait, cfg_defaults=cfg["defaults"])
                if info["status"] not in ("idle", "dead"):
                    err(f"--queue timed out after {int(max_wait)}s; pane still {info['status']}")
            elif kill_and_run:
                try:
                    tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)
                except Exception:
                    pass
                time.sleep(0.2)
                info = poll_until_idle(server, offsets, pane, min(5.0, max_wait), cfg_defaults=cfg["defaults"])
                if info["status"] not in ("idle", "dead"):
                    err(f"--kill-and-run could not return pane to idle within {int(min(5.0, max_wait))}s")
            else:
                err(busy_error_message(pane, info))
        if info["status"] == "dead":
            err(f"pane '{pane}' shell is dead — recreate with 'tx kill {pane}' then 'tx new {pane}'")

        offender = check_allowlist(cmd, cfg, pane_id=pane)
        if offender is not None:
            err(f"'{offender}' not in command_allowlist — edit ~/.tx/config.toml")
        check_confirm(cmd, cfg, yes)

        run_id = _start_run(tmux_pane, log_path, cmd, timeout, offsets, pane, cfg=cfg)
        started_iso = (offsets[pane]["active_run"] or {}).get("started")
        save_offsets(offsets)
    if as_json:
        record = {
            "pane": pane,
            "run_id": run_id,
            "cmd": cmd,
            "started": started_iso,
            "ended": None,
            "exit": None,
            "duration_ms": None,
            "stdout": None,
            "truncated": False,
        }
        click.echo(json.dumps(record, indent=2))
    else:
        click.echo(run_id)


@cli.command(
    name="wait-run",
    short_help="Block until a specific run completes.",
    help=(
        "Wait for the given run-id's end marker, then return its output.\n\n"
        "If the run already completed, returns the cached output immediately."
    ),
)
@click.argument("pane")
@click.argument("run_id")
@click.option("--timeout", "timeout", type=float, default=None, help="override wait timeout in seconds")
@click.option("--max", "max_lines", type=int, default=None, help="cap output at N lines")
@click.option("--no-strip", is_flag=True, default=False, help="disable whitespace collapsing")
@click.option(
    "--on-timeout",
    "on_timeout",
    type=click.Choice(["report", "cancel", "kill"], case_sensitive=False),
    default="report",
    help="behavior on marker timeout: report (default), cancel (C-c then re-check), kill (C-c twice + kill-pane)",
)
@click.option("--keep-ansi", "keep_ansi", is_flag=True, default=False, help="do not strip ANSI escape sequences from output")
@click.option("--json", "as_json", is_flag=True, default=False, help="emit a single JSON record instead of plain text")
@_compact_options
def cmd_wait_run(
    pane: str,
    run_id: str,
    timeout: float | None,
    max_lines: int | None,
    no_strip: bool,
    on_timeout: str,
    keep_ansi: bool,
    as_json: bool,
    raw_flag: bool,
    terse_flag: bool,
    token_budget_flag: int | None,
    no_strip_banners_flag: bool,
    no_collapse_repeats_flag: bool,
    no_normalize_flag: bool,
) -> None:
    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    timeout = timeout if timeout is not None else float(cfg["defaults"]["timeout"])
    max_lines = max_lines if max_lines is not None else int(cfg["defaults"]["max_lines"])
    strip_blanks = bool(cfg["defaults"]["strip"]) and not no_strip
    strip_ansi_flag = resolve_strip_ansi(cfg, keep_ansi)
    keep_ansi_resolved = not strip_ansi_flag

    with offsets_lock():
        offsets = load_offsets()
        state = require_pane(offsets, pane)
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        record = find_run_record(offsets[pane], run_id)
        if record is None:
            err(f"run '{run_id}' not found in pane '{pane}' — run 'tx runs {pane}'")
        if record.get("end_offset") is not None:
            # Already finalized; emit cached output.
            per_call_mode = _per_call_compact_mode(raw_flag, terse_flag)
            compact_ctx = _build_compact_ctx(
                cfg, state, pane, record.get("cmd", ""), run_id, per_call_mode,
                per_call_no_normalize=no_normalize_flag,
                per_call_no_strip_banners=no_strip_banners_flag,
                per_call_no_collapse_repeats=no_collapse_repeats_flag,
            )
            if compact_ctx is not None and token_budget_flag is not None:
                compact_ctx.token_budget = token_budget_flag
            kept = _render_run_output(
                pane_log_path(pane),
                int(record["start_offset"]),
                int(record["end_offset"]),
                strip_blanks,
                keep_ansi=keep_ansi_resolved,
                redact_cfg=cfg,
                compact_ctx=compact_ctx,
                pane_state=state,
            )
            if compact_ctx is not None:
                _res = getattr(compact_ctx, "result", None)
                if _res is not None and _res.footer:
                    kept.append(_res.footer)
            shown = kept[:max_lines]
            remainder = kept[max_lines:]
            offsets[pane] = state
            save_offsets(offsets)
            cached_exit = record.get("exit")
            if as_json:
                _emit_run_json(
                    pane, run_id, record.get("cmd", ""), record.get("started"),
                    record.get("ended"), cached_exit, shown, bool(remainder),
                )
            else:
                cached_label = str(cached_exit) if cached_exit is not None else "?"
                click.echo(
                    "\n".join([f"[exit:{cached_label}]"] + shown),
                    color=keep_ansi_resolved or None,
                )
                if remainder:
                    click.echo(
                        f"[truncated: {len(remainder)} lines remain — increase --max or use 'tx output {pane} {run_id}']"
                    )
            return
        start_offset = int(record["start_offset"])
        cmd_str = record.get("cmd", "")
        started_iso = record.get("started")
        server = get_server()
        tmux_pane = find_pane_anywhere(server, state.get("tmux_id", ""))

    # Wait outside the lock. Pass cfg_defaults so the prompt-pattern fallback
    # can fire for nested-shell scenarios.
    found, exit_code, end_offset, idle_age = wait_for_marker(
        pane_log_path(pane), start_offset, run_id, timeout, cfg["defaults"]
    )
    extra_note: str | None = None
    if not found and on_timeout != "report" and tmux_pane is not None:
        found, exit_code, end_offset, idle_age, extra_note = _apply_on_timeout(
            on_timeout, tmux_pane, pane_log_path(pane), start_offset, run_id, cfg["defaults"]
        )
    with offsets_lock():
        offsets = load_offsets()
        state = offsets[pane]
        if found:
            record_run_end(state, run_id, exit_code, end_offset, max_history)
            offsets[pane] = state
            per_call_mode = _per_call_compact_mode(raw_flag, terse_flag)
            compact_ctx = _build_compact_ctx(
                cfg, state, pane, cmd_str, run_id, per_call_mode,
                per_call_no_normalize=no_normalize_flag,
                per_call_no_strip_banners=no_strip_banners_flag,
                per_call_no_collapse_repeats=no_collapse_repeats_flag,
            )
            if compact_ctx is not None and token_budget_flag is not None:
                compact_ctx.token_budget = token_budget_flag
            kept = _render_run_output(
                pane_log_path(pane), start_offset, end_offset, strip_blanks,
                keep_ansi=keep_ansi_resolved, redact_cfg=cfg,
                compact_ctx=compact_ctx, pane_state=state,
            )
            if compact_ctx is not None:
                _res = getattr(compact_ctx, "result", None)
                if _res is not None and _res.footer:
                    kept.append(_res.footer)
            shown = kept[:max_lines]
            remainder = kept[max_lines:]
            offsets[pane] = state
            save_offsets(offsets)
            ended_iso = None
            for r in reversed(state.get("runs") or []):
                if r.get("id") == run_id:
                    ended_iso = r.get("ended")
                    break
            if as_json:
                notes: list[str] = []
                if exit_code is None:
                    notes.append("hook-missing: no marker observed but prompt returned")
                if extra_note:
                    notes.append(f"on-timeout: {extra_note}")
                _emit_run_json(
                    pane, run_id, cmd_str, started_iso, ended_iso, exit_code,
                    shown, bool(remainder), notes,
                )
            else:
                exit_label = str(exit_code) if exit_code is not None else "?"
                out_parts = [f"[exit:{exit_label}]"] + shown
                if exit_code is None:
                    out_parts.append(
                        f"[hook-missing: no marker observed but prompt returned — exit code unknown. "
                        f"Try 'tx hook-install {pane}' if this pane runs a nested shell.]"
                    )
                if extra_note:
                    out_parts.append(f"[on-timeout: {extra_note}]")
                if remainder:
                    out_parts.append(
                        f"[truncated: {len(remainder)} lines remain — increase --max or use 'tx output {pane} {run_id}']"
                    )
                click.echo("\n".join(out_parts), color=keep_ansi_resolved or None)
        else:
            info = pane_state(server, state, pane, cfg["defaults"])
            msg = truthful_timeout_message(pane, info, run_id, timeout, pane_log_path(pane), idle_age)
            save_offsets(offsets)
            if as_json:
                notes = [f"timeout: {msg}"]
                if extra_note:
                    notes.append(f"on-timeout: {extra_note}")
                _emit_run_json(
                    pane, run_id, cmd_str, started_iso, None, None,
                    [], False, notes,
                )
            else:
                note = f" [on-timeout: {extra_note}]" if extra_note else ""
                click.echo(f"[timeout: {msg}]{note}")


@cli.command(
    name="output",
    short_help="Return the output of a completed run.",
    help=(
        "Return the buffer slice between a run's start and end markers.\n\n"
        "Either pass an explicit <run_id>, or one of:\n"
        "  --last          most recent completed run for this pane\n"
        "  --since-run R   concatenate every completed run after run R\n"
        "  --handle h-XXX  resolve a compaction handle to its run\n\n"
        "Slice the result with:\n"
        "  --range N-M     return only lines N through M (0-based)\n"
        "  --grep PAT      return only lines matching PAT (+ context)\n"
        "  --full          bypass all compaction layers (escape hatch)"
    ),
)
@click.argument("pane")
@click.argument("run_id", required=False)
@click.option("--max", "max_lines", type=int, default=None, help="cap output at N lines")
@click.option("--last", "last", is_flag=True, default=False, help="output the most recent completed run")
@click.option("--since-run", "since_run", type=str, default=None, help="concat every run after this id")
@click.option("--no-strip", is_flag=True, default=False, help="disable whitespace collapsing")
@click.option("--keep-ansi", "keep_ansi", is_flag=True, default=False, help="do not strip ANSI escape sequences from output")
@click.option("--json", "as_json", is_flag=True, default=False, help="emit a JSON record (single-run forms only)")
@click.option("--handle", "handle_id", type=str, default=None,
              help="resolve a compaction handle (h-XXXX) to its run + byte range")
@click.option("--range", "line_range", type=str, default=None,
              help="return only line indices N-M (0-based; inclusive on both ends)")
@click.option("--grep", "grep_pat", type=str, default=None,
              help="return only lines matching PAT (with ±N context, configurable)")
@click.option("--grep-context", "grep_context", type=int, default=3,
              help="lines of context around each --grep match")
@click.option("--full", "full_flag", is_flag=True, default=False,
              help="bypass compaction; emit the cleaned bytes verbatim")
@_compact_options
def cmd_output(
    pane: str,
    run_id: str | None,
    max_lines: int | None,
    last: bool,
    since_run: str | None,
    no_strip: bool,
    keep_ansi: bool,
    as_json: bool,
    handle_id: str | None,
    line_range: str | None,
    grep_pat: str | None,
    grep_context: int,
    full_flag: bool,
    raw_flag: bool,
    terse_flag: bool,
    token_budget_flag: int | None,
    no_strip_banners_flag: bool,
    no_collapse_repeats_flag: bool,
    no_normalize_flag: bool,
) -> None:
    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    max_lines = max_lines if max_lines is not None else int(cfg["defaults"]["max_lines"])
    strip_blanks = bool(cfg["defaults"]["strip"]) and not no_strip
    strip_ansi_flag = resolve_strip_ansi(cfg, keep_ansi)
    keep_ansi_resolved = not strip_ansi_flag

    # --handle resolves to a run-id + byte range. We override the
    # selector inputs below so the rest of cmd_output works unchanged.
    if handle_id is not None:
        with offsets_lock():
            offsets = load_offsets()
            require_pane(offsets, pane)
            state = offsets[pane]
            hrec = _handle_store.find_handle(state, handle_id)
        if hrec is None:
            err(
                f"handle '{handle_id}' not found in pane '{pane}'. "
                f"It may have expired (handles are GC'd with the run record). "
                f"Use `tx output {pane} <run_id> --full` to re-fetch the full content."
            )
        # Carry the byte range via run_id resolution below — easier than
        # forking the read path.
        if hrec.get("run_id"):
            run_id = hrec["run_id"]
        else:
            # Buffer handle: re-render the cached byte range directly.
            _emit_handle_buffer(
                pane, hrec, cfg, max_lines, strip_blanks, keep_ansi_resolved,
                line_range, grep_pat, grep_context, full_flag, as_json,
            )
            return

    # --full forces raw mode (skip all compaction layers). Implemented
    # by routing through the existing path with compact_ctx=None.
    if full_flag:
        raw_flag = True

    chosen = sum(1 for x in (run_id, last, since_run) if x)
    if chosen != 1:
        err("specify exactly one of: <run_id>, --last, --since-run <id>, --handle h-XXXX")
    if as_json and since_run is not None:
        err("--json is only supported with a single-run selector (<run_id> or --last)")

    with offsets_lock():
        offsets = load_offsets()
        require_pane(offsets, pane)
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        state = offsets[pane]
        completed = list(state.get("runs") or [])

    if last:
        # Only completed runs (end_offset set) count.
        candidates = [r for r in completed if r.get("end_offset") is not None]
        if not candidates:
            err(f"no completed runs in pane '{pane}'")
        target = candidates[-1]
        per_call_mode = _per_call_compact_mode(raw_flag, terse_flag)
        compact_ctx = _build_compact_ctx(
            cfg, state, pane, target.get("cmd", ""), target.get("id"), per_call_mode,
            per_call_no_normalize=no_normalize_flag,
            per_call_no_strip_banners=no_strip_banners_flag,
            per_call_no_collapse_repeats=no_collapse_repeats_flag,
        )
        if compact_ctx is not None and token_budget_flag is not None:
            compact_ctx.token_budget = token_budget_flag
        kept = _render_run_output(
            pane_log_path(pane),
            int(target["start_offset"]),
            int(target["end_offset"]),
            strip_blanks,
            keep_ansi=keep_ansi_resolved,
            redact_cfg=cfg,
            compact_ctx=compact_ctx,
            pane_state=state,
        )
        kept = _apply_range_grep(kept, line_range, grep_pat, grep_context)
        shown = kept[:max_lines]
        remainder = kept[max_lines:]
        exit_v = target.get("exit")
        if as_json:
            _emit_run_json(
                pane, target.get("id", "?"), target.get("cmd", ""),
                target.get("started"), target.get("ended"), exit_v,
                shown, bool(remainder),
            )
        else:
            out_parts = [f"[exit:{exit_v if exit_v is not None else '?'}]"] + shown
            if remainder:
                out_parts.append(f"[truncated: {len(remainder)} lines remain — increase --max]")
            if compact_ctx is not None:
                _res = getattr(compact_ctx, "result", None)
                if _res is not None and _res.footer:
                    out_parts.append(_res.footer)
            click.echo("\n".join(out_parts), color=keep_ansi_resolved or None)
        return

    if since_run is not None:
        # Find the anchor; emit everything after it.
        idx = next((i for i, r in enumerate(completed) if r.get("id") == since_run), None)
        if idx is None:
            err(f"run '{since_run}' not found in pane '{pane}' — run 'tx runs {pane}'")
        slice_runs = completed[idx + 1:]
        if not slice_runs:
            click.echo(f"[empty: no runs recorded after '{since_run}']")
            return
        all_lines: list[str] = []
        for r in slice_runs:
            if r.get("end_offset") is None:
                continue
            kept = _render_run_output(
                pane_log_path(pane),
                int(r["start_offset"]),
                int(r["end_offset"]),
                strip_blanks,
                keep_ansi=keep_ansi_resolved,
                redact_cfg=cfg,
            )
            exit_v = r.get("exit")
            label = f"--- {r.get('id', '?')} exit={exit_v if exit_v is not None else '?'} ---"
            all_lines.append(label)
            all_lines.extend(kept)
        shown = all_lines[:max_lines]
        remainder = all_lines[max_lines:]
        click.echo("\n".join(shown), color=keep_ansi_resolved or None)
        if remainder:
            click.echo(f"[truncated: {len(remainder)} lines remain — increase --max]")
        return

    # Default: explicit run_id.
    record = find_run_record(state, run_id)  # type: ignore[arg-type]
    if record is None:
        err(f"run '{run_id}' not found in pane '{pane}' — run 'tx runs {pane}'")
    if record.get("end_offset") is None:
        err(f"run '{run_id}' is still active — use 'tx wait-run {pane} {run_id}' to block")
    per_call_mode = _per_call_compact_mode(raw_flag, terse_flag)
    compact_ctx = _build_compact_ctx(
        cfg, state, pane, record.get("cmd", ""), record.get("id"), per_call_mode,
        per_call_no_normalize=no_normalize_flag,
        per_call_no_strip_banners=no_strip_banners_flag,
        per_call_no_collapse_repeats=no_collapse_repeats_flag,
    )
    if compact_ctx is not None and token_budget_flag is not None:
        compact_ctx.token_budget = token_budget_flag
    kept = _render_run_output(
        pane_log_path(pane),
        int(record["start_offset"]),
        int(record["end_offset"]),
        strip_blanks,
        keep_ansi=keep_ansi_resolved,
        redact_cfg=cfg,
        compact_ctx=compact_ctx,
        pane_state=state,
    )
    kept = _apply_range_grep(kept, line_range, grep_pat, grep_context)
    shown = kept[:max_lines]
    remainder = kept[max_lines:]
    exit_v = record.get("exit")
    if as_json:
        _emit_run_json(
            pane, record.get("id", run_id or "?"), record.get("cmd", ""),
            record.get("started"), record.get("ended"), exit_v,
            shown, bool(remainder),
        )
        return
    out_parts = []
    out_parts.append(f"[exit:{exit_v if exit_v is not None else '?'}]")
    out_parts.extend(shown)
    if remainder:
        out_parts.append(f"[truncated: {len(remainder)} lines remain — increase --max]")
    if compact_ctx is not None:
        _res = getattr(compact_ctx, "result", None)
        if _res is not None and _res.footer:
            out_parts.append(_res.footer)
    click.echo("\n".join(out_parts), color=keep_ansi_resolved or None)


@cli.command(
    name="runs",
    short_help="List recorded runs for a pane.",
    help="Table of recent runs: id, exit, duration, started, cmd.",
)
@click.argument("pane")
@click.option("--limit", "limit", type=int, default=20, help="show at most N most recent runs")
def cmd_runs(pane: str, limit: int) -> None:
    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    with offsets_lock():
        offsets = load_offsets()
        require_pane(offsets, pane)
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        state = offsets[pane]
        active = state.get("active_run")
        completed = state.get("runs") or []
    rows: list[tuple[str, str, str, str, str]] = []
    if active:
        rows.append((
            active.get("id", "?"),
            "active",
            "-",
            active.get("started", "-")[-8:],
            (active.get("cmd") or "")[:60],
        ))
    for r in list(reversed(completed))[:limit]:
        exit_v = r.get("exit")
        exit_text = str(exit_v) if exit_v is not None else "?"
        rows.append((
            r.get("id", "?"),
            exit_text,
            _duration_str(r.get("started", ""), r.get("ended")),
            (r.get("started", "") or "")[-8:],
            (r.get("cmd") or "")[:60],
        ))
    if not rows:
        click.echo("[empty: no runs recorded for this pane]")
        return
    headers = ("ID", "EXIT", "DUR", "AT", "CMD")
    widths = [
        max(len(headers[i]), max((len(r[i]) for r in rows), default=0))
        for i in range(len(headers))
    ]
    line = "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers)))
    click.echo(line)
    for r in rows:
        click.echo("  ".join(r[i].ljust(widths[i]) for i in range(len(headers))))


@cli.command(
    name="kill-run",
    short_help="Interrupt a running command (send C-c).",
    help="Send C-c to the pane and wait briefly for the active run to emit its end marker.",
)
@click.argument("pane")
@click.argument("run_id")
def cmd_kill_run(pane: str, run_id: str) -> None:
    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        active = offsets[pane].get("active_run") or {}
        if active.get("id") != run_id:
            record = find_run_record(offsets[pane], run_id)
            if record is None:
                err(f"run '{run_id}' not found in pane '{pane}'")
            err(f"run '{run_id}' is not active (already completed)")
        start_offset = int(active["start_offset"])
        try:
            tmux_pane.send_keys("C-c", enter=False, suppress_history=False, literal=False)
        except Exception:
            pass

    found, exit_code, end_offset, _idle = wait_for_marker(
        pane_log_path(pane), start_offset, run_id, timeout=3.0, cfg_defaults=cfg["defaults"]
    )
    with offsets_lock():
        offsets = load_offsets()
        state = offsets[pane]
        if found:
            record_run_end(state, run_id, exit_code, end_offset, max_history)
            offsets[pane] = state
            save_offsets(offsets)
            label = str(exit_code) if exit_code is not None else "?"
            click.echo(f"[killed: run '{run_id}' interrupted, exit={label}]")
        else:
            save_offsets(offsets)
            click.echo(
                f"[killed: C-c sent to run '{run_id}', but no end marker observed after 3s — "
                f"the process may be ignoring SIGINT. Use 'tx kill {pane}' to destroy the pane.]"
            )


@cli.command(
    name="stream",
    short_help="Run a command, capture output until a bound, then C-c and return.",
    help=(
        "Run <cmd> as a normal tx exec, watch the output, and as soon as one of\n"
        "the bounds is reached send C-c to terminate the command. Returns the\n"
        "captured output (everything from the run's start to the C-c).\n\n"
        "Exactly one bound must be specified:\n"
        "  --duration N[s|m|h]   wall-clock time before C-c (e.g. '5', '5s', '2m')\n"
        "  --lines N             non-empty cleaned lines before C-c\n"
        "  --until <regex>       regex match in cleaned output before C-c\n\n"
        "Useful for 'give me 5 seconds of journalctl -f' or 'run until\n"
        "\"Listening on\" appears, then stop'.\n"
        "--timeout caps the whole call (default = configured timeout)."
    ),
)
@click.argument("pane")
@click.argument("cmd")
@click.option("--duration", "duration", type=str, default=None, help="wall-clock duration before C-c (e.g. '5s', '2m')")
@click.option("--lines", "lines", type=int, default=None, help="non-empty cleaned lines before C-c")
@click.option("--until", "until", type=str, default=None, help="regex to match before C-c")
@click.option("--timeout", "timeout", type=float, default=None, help="overall call cap in seconds")
@click.option("--max", "max_lines", type=int, default=None, help="cap output at N lines")
@click.option("--no-strip", is_flag=True, default=False, help="disable whitespace collapsing")
@click.option("--keep-ansi", "keep_ansi", is_flag=True, default=False, help="do not strip ANSI escape sequences")
@click.option("--yes", "yes", is_flag=True, default=False, help="skip confirm-pattern prompt")
@_compact_options
def cmd_stream(
    pane: str,
    cmd: str,
    duration: str | None,
    lines: int | None,
    until: str | None,
    timeout: float | None,
    max_lines: int | None,
    no_strip: bool,
    keep_ansi: bool,
    yes: bool,
    raw_flag: bool,
    terse_flag: bool,
    token_budget_flag: int | None,
    no_strip_banners_flag: bool,
    no_collapse_repeats_flag: bool,
    no_normalize_flag: bool,
) -> None:
    cfg = load_config()
    max_history = int(cfg["defaults"].get("max_run_history", 100))
    max_lines = max_lines if max_lines is not None else int(cfg["defaults"]["max_lines"])
    timeout = timeout if timeout is not None else float(cfg["defaults"]["timeout"])
    strip_blanks = bool(cfg["defaults"]["strip"]) and not no_strip
    strip_ansi_flag = resolve_strip_ansi(cfg, keep_ansi)
    keep_ansi_resolved = not strip_ansi_flag

    chosen = sum(1 for x in (duration, lines, until) if x is not None)
    if chosen != 1:
        err("specify exactly one of --duration / --lines / --until")

    until_re = None
    if until is not None:
        try:
            until_re = re.compile(until)
        except re.error as e:
            err(f"invalid --until regex: {e}")
    if lines is not None and lines <= 0:
        err("--lines N must be positive")

    duration_s: float | None = None
    if duration is not None:
        try:
            duration_s = _parse_duration(duration)
        except ValueError as e:
            err(str(e))
        if duration_s <= 0:
            err("--duration must be positive")
        # Bound the overall timeout to duration+grace if user didn't override.
        if timeout < duration_s + 5.0:
            timeout = duration_s + 5.0

    with offsets_lock():
        offsets = load_offsets()
        state, server, tmux_pane, log_path = _resolve_pane_for_input(offsets, pane)
        if state.get("paused_at"):
            err(f"pane '{pane}' is paused (handoff); run 'tx resume {pane}' first")
        finalize_runs(offsets, pane, max_history, cfg["defaults"])
        info = pane_state(server, offsets[pane], pane, cfg["defaults"])
        if info["status"] in ("running", "tui"):
            err(busy_error_message(pane, info))
        if info["status"] == "dead":
            err(f"pane '{pane}' shell is dead — recreate with 'tx kill {pane}' then 'tx new {pane}'")

        offender = check_allowlist(cmd, cfg, pane_id=pane)
        if offender is not None:
            err(f"'{offender}' not in command_allowlist — edit ~/.tx/config.toml")
        check_confirm(cmd, cfg, yes)

        run_id = _start_run(tmux_pane, log_path, cmd, timeout, offsets, pane, cfg=cfg)
        start_offset = int(offsets[pane]["active_run"]["start_offset"])
        save_offsets(offsets)

    reason, exit_code, end_offset, _idle, matched = wait_for_marker_or_bound(
        log_path, start_offset, run_id, timeout, cfg["defaults"],
        until_re=until_re, until_lines=lines, until_duration_s=duration_s,
    )

    bound_note: str | None = None
    cancellation_pending_note: str | None = None
    finalized = reason == "marker"
    if reason in ("until", "lines", "duration"):
        # Bound fired; interrupt, but only clear active_run if finalization is observed.
        found2, exit2, end2 = _interrupt_for_bound(
            tmux_pane, log_path, start_offset, run_id, cfg["defaults"]
        )
        if found2:
            end_offset = end2
            exit_code = exit2
            finalized = True
        else:
            cancellation_pending_note = _bound_pending_note(run_id, reason, matched)
        if reason == "until":
            bound_note = f"stream-stopped: until matched '{matched}'"
        elif reason == "lines":
            bound_note = f"stream-stopped: reached {lines} lines"
        else:
            bound_note = f"stream-stopped: duration {duration} elapsed"

    with offsets_lock():
        offsets = load_offsets()
        state = offsets[pane]
        active = state.get("active_run") or {}
        if active.get("id") == run_id and finalized:
            record_run_end(state, run_id, exit_code, end_offset, max_history)
        offsets[pane] = state
        per_call_mode = _per_call_compact_mode(raw_flag, terse_flag)
        compact_ctx = _build_compact_ctx(
            cfg, state, pane, cmd, run_id, per_call_mode,
            per_call_no_normalize=no_normalize_flag,
            per_call_no_strip_banners=no_strip_banners_flag,
            per_call_no_collapse_repeats=no_collapse_repeats_flag,
        )
        if compact_ctx is not None and token_budget_flag is not None:
            compact_ctx.token_budget = token_budget_flag
        kept = _render_run_output(
            log_path, start_offset, end_offset, strip_blanks,
            keep_ansi=keep_ansi_resolved, redact_cfg=cfg,
            compact_ctx=compact_ctx, pane_state=state,
        )
        if compact_ctx is not None:
            _res = getattr(compact_ctx, "result", None)
            if _res is not None and _res.footer:
                kept.append(_res.footer)
        shown = kept[:max_lines]
        remainder = kept[max_lines:]
        state["tail_offset"] = end_offset
        if remainder:
            state["pending_lines"] = remainder
        else:
            state.pop("pending_lines", None)
        offsets[pane] = state
        save_offsets(offsets)

    if reason == "timeout":
        click.echo(f"[timeout: stream exceeded {int(timeout)}s with no bound hit]")
        return
    if cancellation_pending_note is not None:
        out_parts = shown + [f"[{cancellation_pending_note}]"]
        if bound_note:
            out_parts.append(f"[{bound_note}]")
        if remainder:
            out_parts.append(
                f"[truncated: {len(remainder)} lines remain — run: tx tail {pane} --continue]"
            )
        click.echo("\n".join(out_parts), color=keep_ansi_resolved or None)
        return
    exit_label = str(exit_code) if exit_code is not None else "?"
    out_parts = [f"[exit:{exit_label}]"] + shown
    if bound_note:
        out_parts.append(f"[{bound_note}]")
    if remainder:
        out_parts.append(
            f"[truncated: {len(remainder)} lines remain — run: tx tail {pane} --continue]"
        )
    click.echo("\n".join(out_parts), color=keep_ansi_resolved or None)
