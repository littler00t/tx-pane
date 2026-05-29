"""tx_core.commands.read — log-reading commands (tail / dump / wait / reset / log / log-path / grep / mark)

Extracted verbatim from the monolithic `tx-pane` script during the modular
refactor. Each `@cli.command()` registers itself on the shared `cli`
root group on import.
"""

from __future__ import annotations

from tx_core.commands._common import *  # noqa: F401,F403


def _read_compact_ctx(
    cfg: dict[str, Any],
    state: dict[str, Any] | None,
    pane: str,
    cmd: str,
    raw_flag: bool,
    terse_flag: bool,
    token_budget_flag: int | None,
    no_strip_banners_flag: bool,
    no_collapse_repeats_flag: bool,
    no_normalize_flag: bool,
    *,
    must_keep_re: list[re.Pattern[str]] | None = None,
):
    per_call_mode = _per_call_compact_mode(raw_flag, terse_flag)
    compact_ctx = _build_compact_ctx(
        cfg, state, pane, cmd, None, per_call_mode,
        per_call_no_normalize=no_normalize_flag,
        per_call_no_strip_banners=no_strip_banners_flag,
        per_call_no_collapse_repeats=no_collapse_repeats_flag,
        must_keep_re=must_keep_re,
    )
    if compact_ctx is not None and token_budget_flag is not None:
        compact_ctx.token_budget = token_budget_flag
    return compact_ctx


def _append_compact_footer(lines: list[str], compact_ctx) -> list[str]:
    if compact_ctx is None:
        return lines
    result = getattr(compact_ctx, "result", None)
    if result is not None and result.footer:
        return [*lines, result.footer]
    return lines


def _retry_stale_offsets() -> None:
    err("pane offsets changed while reading; retry the command")


def _pending_equal(left: Any, right: Any) -> bool:
    return list(left or []) == list(right or [])


def _check_tail_snapshot(state: dict[str, Any], start_offset: int, pending_snapshot: list[str] | None) -> None:
    if int(state.get("tail_offset", 0)) != start_offset:
        _retry_stale_offsets()
    if not _pending_equal(state.get("pending_lines"), pending_snapshot):
        _retry_stale_offsets()


def _check_dump_snapshot(state: dict[str, Any], pending_snapshot: list[str] | None) -> None:
    if not _pending_equal(state.get("dump_pending_lines"), pending_snapshot):
        _retry_stale_offsets()


def _render_read_text(
    raw_text: str,
    cfg: dict[str, Any],
    state: dict[str, Any] | None,
    pane: str,
    cmd: str,
    strip_blanks: bool,
    raw_flag: bool,
    terse_flag: bool,
    token_budget_flag: int | None,
    no_strip_banners_flag: bool,
    no_collapse_repeats_flag: bool,
    no_normalize_flag: bool,
    *,
    log_path: Path | None = None,
    start_offset: int | None = None,
    end_offset: int | None = None,
    must_keep_re: list[re.Pattern[str]] | None = None,
) -> list[str]:
    text = apply_redactions(raw_text, cfg)
    compact_ctx = _read_compact_ctx(
        cfg, state, pane, cmd, raw_flag, terse_flag, token_budget_flag,
        no_strip_banners_flag, no_collapse_repeats_flag, no_normalize_flag,
        must_keep_re=must_keep_re,
    )
    lines = _render_buffer_output(
        text, strip_blanks, compact_ctx=compact_ctx, full_cfg=cfg,
        pane_state=state, log_path=log_path,
        start_offset=start_offset, end_offset=end_offset,
    )
    return _append_compact_footer(lines, compact_ctx)


# ----- tail -----
@cli.command(
    name="tail",
    short_help="Return new output since last tail/run call.",
    help=(
        "Return new output since last tail/run call (incremental).\n\n"
        "--continue resumes reading after a truncation.\n"
        "--all iterates --continue internally until the buffer is fully drained.\n"
        "--from <name> reads from a named bookmark instead of tail_offset.\n"
        "tail offset only advances after the full buffer is consumed."
    ),
)
@click.argument("pane")
@click.option("--max", "max_lines", type=int, default=None, help="cap at N lines")
@click.option("--continue", "do_continue", is_flag=True, default=False, help="resume reading after a truncation")
@click.option("--all", "do_all", is_flag=True, default=False, help="drain all pending output (iterates --continue internally)")
@click.option("--from", "from_bookmark", type=str, default=None, help="read from a named bookmark instead of tail_offset")
@click.option("--no-strip", is_flag=True, default=False, help="disable blank-line collapsing")
@click.option("--keep-ansi", "keep_ansi", is_flag=True, default=False, help="do not strip ANSI escape sequences from output")
@click.option("--timestamps", "timestamps", is_flag=True, default=False, help="prefix each line with [hh:mm:ss] (read-time, not per-line)")
@_compact_options
def cmd_tail(
    pane: str,
    max_lines: int | None,
    do_continue: bool,
    do_all: bool,
    from_bookmark: str | None,
    no_strip: bool,
    keep_ansi: bool,
    timestamps: bool,
    raw_flag: bool,
    terse_flag: bool,
    token_budget_flag: int | None,
    no_strip_banners_flag: bool,
    no_collapse_repeats_flag: bool,
    no_normalize_flag: bool,
) -> None:
    cfg = load_config()
    with offsets_lock():
        offsets = load_offsets()
        state = dict(require_pane(offsets, pane))
    log_path = pane_log_path(pane)
    if not log_path.exists():
        err(f"log file missing for pane '{pane}' — pane may have been created outside tx-pane")

    max_lines = max_lines if max_lines is not None else int(cfg["defaults"]["max_lines"])
    strip_blanks = bool(cfg["defaults"]["strip"]) and not no_strip
    strip_ansi_flag = resolve_strip_ansi(cfg, keep_ansi)
    keep_ansi_resolved = not strip_ansi_flag

    def _emit(lines: list[str]) -> None:
        if not lines:
            return
        out = lines
        if timestamps:
            out = stamp_lines(out)
        click.echo("\n".join(out), color=keep_ansi_resolved or None)

    # Flag combos.
    if do_all and (do_continue or from_bookmark):
        err("--all is exclusive with --continue / --from")
    if do_continue and from_bookmark:
        err("--continue is exclusive with --from")

    # --all drains pending + reads new output, concatenated, no truncation.
    if do_all:
        with offsets_lock():
            offsets = load_offsets()
            state = dict(require_pane(offsets, pane))
            pending_snapshot = list(state.get("pending_lines") or [])
            start_offset = int(state.get("tail_offset", 0))
        pending = list(state.get("pending_lines") or [])
        file_size = log_path.stat().st_size
        if file_size > start_offset:
            raw_text = _read_cleaned_text(log_path, start_offset, file_size, keep_ansi=keep_ansi_resolved)
            pending.extend(_render_read_text(
                raw_text, cfg, state, pane, f"tx-pane tail {pane}", strip_blanks,
                raw_flag, terse_flag, token_budget_flag, no_strip_banners_flag,
                no_collapse_repeats_flag, no_normalize_flag,
                log_path=log_path, start_offset=start_offset, end_offset=file_size,
            ))
        with offsets_lock():
            offsets = load_offsets()
            state = offsets[pane]
            _check_tail_snapshot(state, start_offset, pending_snapshot)
            state["tail_offset"] = file_size
            state.pop("pending_lines", None)
            state["continue_offset"] = None
            offsets[pane] = state
            save_offsets(offsets)
        _emit(pending)
        click.echo("[end of output]")
        return

    # --from <name>: explicit one-shot read from a bookmark. Advances tail_offset
    # to end-of-file on completion (matches the "tail-style" semantic).
    if from_bookmark is not None:
        with offsets_lock():
            offsets = load_offsets()
            state = dict(require_pane(offsets, pane))
            pending_snapshot = list(state.get("pending_lines") or [])
            original_tail_offset = int(state.get("tail_offset", 0))
        start_offset = _resolve_bookmark(state, from_bookmark)
        file_size = log_path.stat().st_size
        raw_text = _read_cleaned_text(log_path, start_offset, file_size, keep_ansi=keep_ansi_resolved)
        all_lines = _render_read_text(
            raw_text, cfg, state, pane, f"tx-pane tail {pane}", strip_blanks,
            raw_flag, terse_flag, token_budget_flag, no_strip_banners_flag,
            no_collapse_repeats_flag, no_normalize_flag,
            log_path=log_path, start_offset=start_offset, end_offset=file_size,
        )
        shown = all_lines[:max_lines]
        remainder = all_lines[max_lines:]
        with offsets_lock():
            offsets = load_offsets()
            state = offsets[pane]
            _check_tail_snapshot(state, original_tail_offset, pending_snapshot)
            state["tail_offset"] = file_size
            if remainder:
                state["pending_lines"] = remainder
            else:
                state.pop("pending_lines", None)
            state["continue_offset"] = None
            offsets[pane] = state
            save_offsets(offsets)
        _emit(shown)
        if remainder:
            click.echo(f"[truncated: {len(remainder)} lines remain — run: tx-pane tail {pane} --continue]")
        return

    with offsets_lock():
        offsets = load_offsets()
        state = dict(require_pane(offsets, pane))
        pending = list(state.get("pending_lines") or [])
    if do_continue and not pending:
        err(f"no truncation in progress for pane '{pane}' — run 'tx-pane tail {pane}' first")

    if pending:
        if do_continue:
            chunk = pending[:max_lines]
            remainder = pending[max_lines:]
            with offsets_lock():
                offsets = load_offsets()
                state = offsets[pane]
                _check_dump_snapshot({"dump_pending_lines": state.get("pending_lines")}, pending)
                if remainder:
                    state["pending_lines"] = remainder
                else:
                    state.pop("pending_lines", None)
                    state["continue_offset"] = None
                offsets[pane] = state
                save_offsets(offsets)
            _emit(chunk)
            if remainder:
                click.echo(f"[truncated: {len(remainder)} lines remain — run: tx-pane tail {pane} --continue]")
            else:
                click.echo("[end of output]")
        else:
            chunk = pending[:max_lines]
            remainder = pending[max_lines:]
            _emit(chunk)
            if remainder:
                click.echo(f"[truncated: {len(remainder)} lines remain — run: tx-pane tail {pane} --continue]")
        return

    start_offset = int(state.get("tail_offset", 0))
    pending_snapshot = list(state.get("pending_lines") or [])
    file_size = log_path.stat().st_size
    raw_text = _read_cleaned_text(log_path, start_offset, file_size, keep_ansi=keep_ansi_resolved)
    all_lines = _render_read_text(
        raw_text, cfg, state, pane, f"tx-pane tail {pane}", strip_blanks,
        raw_flag, terse_flag, token_budget_flag, no_strip_banners_flag,
        no_collapse_repeats_flag, no_normalize_flag,
        log_path=log_path, start_offset=start_offset, end_offset=file_size,
    )
    shown = all_lines[:max_lines]
    remainder = all_lines[max_lines:]

    with offsets_lock():
        offsets = load_offsets()
        state = offsets[pane]
        _check_tail_snapshot(state, start_offset, pending_snapshot)
        state["tail_offset"] = file_size
        if remainder:
            state["pending_lines"] = remainder
        else:
            state.pop("pending_lines", None)
        state["continue_offset"] = None
        offsets[pane] = state
        save_offsets(offsets)
    _emit(shown)
    if remainder:
        click.echo(f"[truncated: {len(remainder)} lines remain — run: tx-pane tail {pane} --continue]")


# ----- dump -----
@cli.command(
    name="dump",
    short_help="Return full pane buffer; --tail N for the last N lines.",
    help=(
        "Return the pane buffer.\n\n"
        "By default reads from the start. --tail N returns just the last N\n"
        "cleaned lines (ideal for 'show me what's on screen right now').\n"
        "--from <name> reads from a named bookmark.\n"
        "Does not affect tail_offset. --continue resumes a previously-truncated\n"
        "dump via the per-pane dump_pending cache (independent of tx-pane tail's pending)."
    ),
)
@click.argument("pane")
@click.option("--max", "max_lines", type=int, default=None, help="cap at N lines")
@click.option("--tail", "tail_n", type=int, default=None, help="return only the last N cleaned lines")
@click.option("--head", "head_n", type=int, default=None, help="return only the first N cleaned lines")
@click.option("--from", "from_bookmark", type=str, default=None, help="read from a named bookmark")
@click.option("--continue", "do_continue", is_flag=True, default=False, help="resume reading after a dump truncation")
@click.option("--no-strip", is_flag=True, default=False, help="disable blank-line collapsing")
@click.option("--keep-ansi", "keep_ansi", is_flag=True, default=False, help="do not strip ANSI escape sequences from output")
@click.option("--timestamps", "timestamps", is_flag=True, default=False, help="prefix each line with [hh:mm:ss] (read-time, not per-line)")
@_compact_options
def cmd_dump(
    pane: str,
    max_lines: int | None,
    tail_n: int | None,
    head_n: int | None,
    from_bookmark: str | None,
    do_continue: bool,
    no_strip: bool,
    keep_ansi: bool,
    timestamps: bool,
    raw_flag: bool,
    terse_flag: bool,
    token_budget_flag: int | None,
    no_strip_banners_flag: bool,
    no_collapse_repeats_flag: bool,
    no_normalize_flag: bool,
) -> None:
    cfg = load_config()
    with offsets_lock():
        offsets = load_offsets()
        state = require_pane(offsets, pane)
    log_path = pane_log_path(pane)
    if not log_path.exists():
        err(f"log file missing for pane '{pane}' — pane may have been created outside tx-pane")
    max_lines = max_lines if max_lines is not None else int(cfg["defaults"]["max_lines"])
    strip_blanks = bool(cfg["defaults"]["strip"]) and not no_strip
    strip_ansi_flag = resolve_strip_ansi(cfg, keep_ansi)
    keep_ansi_resolved = not strip_ansi_flag

    def _emit(lines: list[str]) -> None:
        if not lines:
            return
        out = lines
        if timestamps:
            out = stamp_lines(out)
        click.echo("\n".join(out), color=keep_ansi_resolved or None)

    if from_bookmark is not None and (tail_n is not None or head_n is not None or do_continue):
        err("--from is exclusive with --tail / --head / --continue")
    if tail_n is not None and head_n is not None:
        err("--tail and --head are mutually exclusive")

    # --continue: serve next chunk from dump_pending (separate from tail's pending_lines).
    with offsets_lock():
        offsets = load_offsets()
        state = dict(require_pane(offsets, pane))
        pending = list(state.get("dump_pending_lines") or [])
    if do_continue:
        if not pending:
            err(f"no dump truncation in progress for pane '{pane}' — run 'tx-pane dump {pane}' first")
        chunk = pending[:max_lines]
        remainder = pending[max_lines:]
        with offsets_lock():
            offsets = load_offsets()
            state = offsets[pane]
            _check_dump_snapshot(state, pending)
            if remainder:
                state["dump_pending_lines"] = remainder
                offsets[pane] = state
                save_offsets(offsets)
            else:
                state.pop("dump_pending_lines", None)
                offsets[pane] = state
                save_offsets(offsets)
        _emit(chunk)
        if remainder:
            click.echo(f"[truncated: {len(remainder)} lines remain — run: tx-pane dump {pane} --continue]")
        else:
            click.echo("[end of output]")
        return

    file_size = log_path.stat().st_size
    dump_pending_snapshot = pending
    dump_start = _resolve_bookmark(state, from_bookmark) if from_bookmark else 0
    raw_text = _read_cleaned_text(log_path, dump_start, file_size, keep_ansi=keep_ansi_resolved)
    raw_text = strip_run_markers(raw_text)
    all_lines = _render_read_text(
        raw_text, cfg, state, pane, f"tx-pane dump {pane}", strip_blanks,
        raw_flag, terse_flag, token_budget_flag, no_strip_banners_flag,
        no_collapse_repeats_flag, no_normalize_flag,
        log_path=log_path, start_offset=dump_start, end_offset=file_size,
    )

    if tail_n is not None:
        if tail_n <= 0:
            err("--tail N must be positive")
        sliced = all_lines[-tail_n:]
        # --max still caps the displayed window for safety.
        shown = sliced[:max_lines]
        _emit(shown)
        if len(sliced) > max_lines:
            click.echo(f"[truncated: output exceeds --max {max_lines} — increase --max to see more]")
        return

    if head_n is not None:
        if head_n <= 0:
            err("--head N must be positive")
        sliced = all_lines[:head_n]
        shown = sliced[:max_lines]
        _emit(shown)
        if len(sliced) > max_lines:
            click.echo(f"[truncated: output exceeds --max {max_lines} — increase --max to see more]")
        return

    shown = all_lines[:max_lines]
    remainder = all_lines[max_lines:]
    if remainder:
        with offsets_lock():
            offsets = load_offsets()
            state = offsets[pane]
            _check_dump_snapshot(state, dump_pending_snapshot)
            state["dump_pending_lines"] = remainder
            offsets[pane] = state
            save_offsets(offsets)
        _emit(shown)
        click.echo(f"[truncated: {len(remainder)} lines remain — run: tx-pane dump {pane} --continue]")
    else:
        # Clear any stale dump pending.
        with offsets_lock():
            offsets = load_offsets()
            state = offsets[pane]
            _check_dump_snapshot(state, dump_pending_snapshot)
            if state.get("dump_pending_lines"):
                offsets[pane].pop("dump_pending_lines", None)
                save_offsets(offsets)
        _emit(shown)


# ----- wait -----
@cli.command(
    name="wait",
    short_help="Block until new output matches a regex.",
    help=(
        "Block until new output matches <regex>. Return matched chunk.\n\n"
        "On timeout: return partial output with [timeout: ...] notice. No error raised."
    ),
)
@click.argument("pane")
@click.argument("pattern")
@click.option("--timeout", "timeout", type=float, default=None, help="override timeout in seconds")
@click.option("--max", "max_lines", type=int, default=None, help="cap at N lines")
@click.option("--no-strip", is_flag=True, default=False, help="disable blank-line collapsing")
@_compact_options
def cmd_wait(
    pane: str,
    pattern: str,
    timeout: float | None,
    max_lines: int | None,
    no_strip: bool,
    raw_flag: bool,
    terse_flag: bool,
    token_budget_flag: int | None,
    no_strip_banners_flag: bool,
    no_collapse_repeats_flag: bool,
    no_normalize_flag: bool,
) -> None:
    cfg = load_config()
    with offsets_lock():
        offsets = load_offsets()
        state = dict(require_pane(offsets, pane))
        pending_snapshot = list(state.get("pending_lines") or [])
        start_offset = int(state.get("tail_offset", 0))
    log_path = pane_log_path(pane)
    if not log_path.exists():
        err(f"log file missing for pane '{pane}' — pane may have been created outside tx-pane")
    try:
        regex = re.compile(pattern)
    except re.error as e:
        err(f"invalid regex: {e}")
        return

    timeout = timeout if timeout is not None else float(cfg["defaults"]["timeout"])
    max_lines = max_lines if max_lines is not None else int(cfg["defaults"]["max_lines"])
    strip_blanks = bool(cfg["defaults"]["strip"]) and not no_strip
    deadline = time.monotonic() + timeout

    matched = False
    while True:
        file_size = log_path.stat().st_size
        if file_size > start_offset:
            with open(log_path, "rb") as f:
                f.seek(start_offset)
                raw = f.read()
            cleaned = ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).replace("\r", "")
            lines = cleaned.split("\n")
            for i, line in enumerate(lines):
                if regex.search(line):
                    matched_idx = i
                    matched = True
                    # Bytes up to and including the line that matched.
                    # Find the byte offset of the end of that line in the raw bytes.
                    raw_lines = _split_raw_by_newlines(raw)
                    # We need to map cleaned-line index to raw-line index. Since
                    # ANSI strip and \r removal don't add/remove newlines, the
                    # line count should match.
                    if matched_idx < len(raw_lines):
                        consumed = raw_lines[matched_idx][1]
                    else:
                        consumed = len(raw)
                    break
            if matched:
                break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)

    if matched:
        raw_text = _read_cleaned_text(log_path, start_offset, start_offset + consumed)
        all_lines = _render_read_text(
            raw_text, cfg, state, pane, f"tx-pane wait {pane} {pattern}", strip_blanks,
            raw_flag, terse_flag, token_budget_flag, no_strip_banners_flag,
            no_collapse_repeats_flag, no_normalize_flag,
            log_path=log_path, start_offset=start_offset, end_offset=start_offset + consumed,
            must_keep_re=[regex],
        )
        shown = all_lines[:max_lines]
        remainder = all_lines[max_lines:]
        with offsets_lock():
            offsets = load_offsets()
            state = offsets[pane]
            _check_tail_snapshot(state, start_offset, pending_snapshot)
            state["tail_offset"] = start_offset + consumed
            state["continue_offset"] = None
            if remainder:
                state["pending_lines"] = remainder
            else:
                state.pop("pending_lines", None)
            offsets[pane] = state
            save_offsets(offsets)
        if shown:
            click.echo("\n".join(shown), color=False)
        if remainder:
            click.echo(f"[truncated: {len(remainder)} lines remain — run: tx-pane tail {pane} --continue]")
    else:
        file_size = log_path.stat().st_size
        raw_text = _read_cleaned_text(log_path, start_offset, file_size)
        all_lines = _render_read_text(
            raw_text, cfg, state, pane, f"tx-pane wait {pane} {pattern}", strip_blanks,
            raw_flag, terse_flag, token_budget_flag, no_strip_banners_flag,
            no_collapse_repeats_flag, no_normalize_flag,
            log_path=log_path, start_offset=start_offset, end_offset=file_size,
            must_keep_re=[regex],
        )
        shown = all_lines[:max_lines]
        if shown:
            click.echo("\n".join(shown), color=False)
        click.echo(f"[timeout: pattern not matched after {int(timeout)}s — partial output above]")


# ----- reset -----
@cli.command(
    name="reset",
    short_help="Reset tail offset.",
    help=(
        "Reset tail offset to current end of log.\n\n"
        "--to <name> rewinds tail_offset to a saved bookmark instead, letting\n"
        "subsequent 'tx-pane tail' replay everything since the mark."
    ),
)
@click.argument("pane")
@click.option("--to", "to_bookmark", type=str, default=None, help="rewind tail_offset to a bookmark")
def cmd_reset(pane: str, to_bookmark: str | None) -> None:
    offsets = load_offsets()
    state = require_pane(offsets, pane)
    log_path = pane_log_path(pane)
    if to_bookmark is not None:
        target = _resolve_bookmark(state, to_bookmark)
        state["tail_offset"] = target
        state["continue_offset"] = None
        state.pop("pending_lines", None)
        offsets[pane] = state
        save_offsets(offsets)
        click.echo(f"[reset: tail offset rewound to bookmark '{to_bookmark}' ({target} bytes)]")
        return
    file_size = log_path.stat().st_size if log_path.exists() else 0
    state["tail_offset"] = file_size
    state["continue_offset"] = None
    state.pop("pending_lines", None)
    offsets[pane] = state
    save_offsets(offsets)
    click.echo("[reset: tail offset advanced to current position]")


# ----- log-path / log -----
@cli.command(
    name="log-path",
    short_help="Print the on-disk log path for a pane.",
    help=(
        "Print the absolute path to ~/.tx-pane/logs/<pane>.log.\n\n"
        "The log is preserved across 'tx-pane kill' so post-mortem inspection works."
    ),
)
@click.argument("pane")
def cmd_log_path(pane: str) -> None:
    # log path is content-addressed by pane id; no need to validate existence
    # of the pane state itself, but reject reserved underscore names.
    if pane.startswith("_"):
        err(f"pane name '{pane}' is reserved")
    click.echo(str(pane_log_path(pane)))


@cli.command(
    name="log",
    short_help="Read the on-disk log (independent of tail offset).",
    help=(
        "Read ~/.tx-pane/logs/<pane>.log directly. Does NOT advance tail_offset and\n"
        "is independent of the pending tail/dump caches.\n\n"
        "Slicing options:\n"
        "  --tail N       last N cleaned lines\n"
        "  --head N       first N cleaned lines\n"
        "  --since-run R  bytes after run R's end marker\n"
        "Defaults to the full log capped by --max."
    ),
)
@click.argument("pane")
@click.option("--max", "max_lines", type=int, default=None, help="cap output at N lines")
@click.option("--tail", "tail_n", type=int, default=None, help="last N cleaned lines")
@click.option("--head", "head_n", type=int, default=None, help="first N cleaned lines")
@click.option("--since-run", "since_run", type=str, default=None, help="bytes after this run's end marker")
@click.option("--no-strip", is_flag=True, default=False, help="disable blank-line collapsing")
@click.option("--keep-ansi", "keep_ansi", is_flag=True, default=False, help="do not strip ANSI escape sequences from output")
@_compact_options
def cmd_log(
    pane: str,
    max_lines: int | None,
    tail_n: int | None,
    head_n: int | None,
    since_run: str | None,
    no_strip: bool,
    keep_ansi: bool,
    raw_flag: bool,
    terse_flag: bool,
    token_budget_flag: int | None,
    no_strip_banners_flag: bool,
    no_collapse_repeats_flag: bool,
    no_normalize_flag: bool,
) -> None:
    cfg = load_config()
    max_lines = max_lines if max_lines is not None else int(cfg["defaults"]["max_lines"])
    strip_blanks = bool(cfg["defaults"]["strip"]) and not no_strip
    strip_ansi_flag = resolve_strip_ansi(cfg, keep_ansi)
    keep_ansi_resolved = not strip_ansi_flag
    if sum(1 for x in (tail_n, head_n, since_run) if x is not None) > 1:
        err("--tail / --head / --since-run are mutually exclusive")
    log_path = pane_log_path(pane)
    if not log_path.exists():
        err(f"log file missing: {log_path}")

    start_offset = 0
    if since_run is not None:
        with offsets_lock():
            offsets = load_offsets()
            require_pane(offsets, pane)
            record = find_run_record(offsets[pane], since_run)
        if record is None:
            err(f"run '{since_run}' not found in pane '{pane}' — run 'tx-pane runs {pane}'")
        if record.get("end_offset") is None:
            err(f"run '{since_run}' is still active — wait for completion or use 'tx-pane tail'")
        start_offset = int(record["end_offset"])

    file_size = log_path.stat().st_size
    raw_text = _read_cleaned_text(log_path, start_offset, file_size, keep_ansi=keep_ansi_resolved)
    raw_text = strip_run_markers(raw_text)
    all_lines = _render_read_text(
        raw_text, cfg, None, pane, f"tx-pane log {pane}", strip_blanks,
        raw_flag, terse_flag, token_budget_flag, no_strip_banners_flag,
        no_collapse_repeats_flag, no_normalize_flag,
        log_path=log_path, start_offset=start_offset, end_offset=file_size,
    )

    if tail_n is not None:
        if tail_n <= 0:
            err("--tail N must be positive")
        all_lines = all_lines[-tail_n:]
    elif head_n is not None:
        if head_n <= 0:
            err("--head N must be positive")
        all_lines = all_lines[:head_n]

    shown = all_lines[:max_lines]
    remainder = all_lines[max_lines:]
    if shown:
        click.echo("\n".join(shown), color=keep_ansi_resolved or None)
    if remainder:
        click.echo(f"[truncated: {len(remainder)} lines remain — increase --max]")


# ----- grep -----
@cli.command(
    name="grep",
    short_help="Search the pane log with regex, with GNU-style -A/-B/-C context.",
    help=(
        "Read ~/.tx-pane/logs/<pane>.log, ANSI-strip, then emit cleaned lines that\n"
        "match <regex>. Groups of matches are separated by '--' lines (GNU grep\n"
        "convention) when context lines are requested.\n\n"
        "Context flags:\n"
        "  -A N   include N lines after each match\n"
        "  -B N   include N lines before each match\n"
        "  -C N   include N lines before and after (shorthand)\n\n"
        "Plain text only — matches are not highlighted."
    ),
)
@click.argument("pane")
@click.argument("pattern")
@click.option("-A", "after", type=int, default=0, help="lines of trailing context")
@click.option("-B", "before", type=int, default=0, help="lines of leading context")
@click.option("-C", "context", type=int, default=None, help="lines of context on both sides")
@click.option("--max", "max_lines", type=int, default=None, help="cap output at N lines")
@click.option("--no-strip", is_flag=True, default=False, help="disable blank-line collapsing")
@click.option("--keep-ansi", "keep_ansi", is_flag=True, default=False, help="do not strip ANSI escape sequences from output")
@_compact_options
def cmd_grep(
    pane: str,
    pattern: str,
    after: int,
    before: int,
    context: int | None,
    max_lines: int | None,
    no_strip: bool,
    keep_ansi: bool,
    raw_flag: bool,
    terse_flag: bool,
    token_budget_flag: int | None,
    no_strip_banners_flag: bool,
    no_collapse_repeats_flag: bool,
    no_normalize_flag: bool,
) -> None:
    cfg = load_config()
    max_lines = max_lines if max_lines is not None else int(cfg["defaults"]["max_lines"])
    strip_blanks = bool(cfg["defaults"]["strip"]) and not no_strip
    strip_ansi_flag = resolve_strip_ansi(cfg, keep_ansi)
    keep_ansi_resolved = not strip_ansi_flag

    if context is not None:
        after = max(after, context)
        before = max(before, context)
    if after < 0 or before < 0:
        err("context counts (-A / -B / -C) must be non-negative")

    try:
        regex = re.compile(pattern)
    except re.error as e:
        err(f"invalid regex: {e}")

    log_path = pane_log_path(pane)
    if not log_path.exists():
        err(f"log file missing: {log_path}")
    file_size = log_path.stat().st_size
    raw_text = _read_cleaned_text(log_path, 0, file_size, keep_ansi=keep_ansi_resolved)
    raw_text = strip_run_markers(raw_text)
    raw_text = apply_redactions(raw_text, cfg)
    all_lines = _strip_lines(raw_text, strip_blanks)
    if not all_lines:
        click.echo("[empty: log has no lines yet]")
        return

    matched_idx = [i for i, ln in enumerate(all_lines) if regex.search(ln)]
    if not matched_idx:
        click.echo("[no matches]")
        return

    # Build ranges with context, then merge overlapping/adjacent regions.
    ranges: list[tuple[int, int]] = []
    for idx in matched_idx:
        lo = max(0, idx - before)
        hi = min(len(all_lines) - 1, idx + after)
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], hi))
        else:
            ranges.append((lo, hi))

    output: list[str] = []
    for n, (lo, hi) in enumerate(ranges):
        if n > 0:
            output.append("--")
        output.extend(all_lines[lo:hi + 1])

    compact_ctx = _read_compact_ctx(
        cfg, None, pane, f"tx-pane grep {pane} {pattern}", raw_flag, terse_flag,
        token_budget_flag, no_strip_banners_flag, no_collapse_repeats_flag,
        no_normalize_flag, must_keep_re=[regex],
    )
    output = _render_buffer_output(
        "\n".join(output), strip_blanks, compact_ctx=compact_ctx, full_cfg=cfg,
        log_path=log_path, start_offset=0, end_offset=file_size,
    )
    output = _append_compact_footer(output, compact_ctx)

    shown = output[:max_lines]
    remainder = output[max_lines:]
    if shown:
        click.echo("\n".join(shown), color=keep_ansi_resolved or None)
    if remainder:
        click.echo(f"[truncated: {len(remainder)} lines remain — increase --max]")


# ----- mark / bookmarks -----
@cli.command(
    name="mark",
    short_help="Save a named byte-offset bookmark.",
    help=(
        "Save the current end-of-log byte offset under <name>.\n\n"
        "Read from it later with 'tx-pane tail --from <name>' / 'tx-pane dump --from <name>',\n"
        "or rewind tail to it with 'tx-pane reset --to <name>'."
    ),
)
@click.argument("pane")
@click.argument("name")
def cmd_mark(pane: str, name: str) -> None:
    if not name or name.startswith("-") or " " in name:
        err(f"bookmark name '{name}' invalid (no spaces, no leading '-')")
    log_path = pane_log_path(pane)
    if not log_path.exists():
        err(f"log file missing for pane '{pane}'")
    with offsets_lock():
        offsets = load_offsets()
        state = require_pane(offsets, pane)
        size = log_path.stat().st_size
        bookmarks = dict(state.get("bookmarks") or {})
        bookmarks[name] = size
        state["bookmarks"] = bookmarks
        offsets[pane] = state
        save_offsets(offsets)
    click.echo(f"[mark: {name} = {size}]")
