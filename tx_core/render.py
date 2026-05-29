"""Output rendering and the compaction pipeline.

Combines the byte-range reader (`_read_cleaned_text`), the cleaned-line
builder (`_strip_lines`), and the full compaction stack (L1-L5 via the
`tx_compact` package, plus handle / dedup / telemetry side-effects) into
the high-level emitters the CLI commands call:

- `_render_run_output`     for the byte range between a run's markers
- `_render_buffer_output`  for `tx-pane tail` / `tx-pane dump` / `tx-pane wait` / `tx-pane grep`
- `_emit_handle_buffer`    for `tx-pane output --handle h-XXXX`
- `_emit_run_json`         for the `--json` shape

Also owns the click decorator stack (`_compact_options`) and the per-call
mode resolver that turns `--raw` / `--terse` / pane state / global default
into a final `CompactCtx`.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import click
import libtmux

from tx_compact import (
    HANDLE_PLACEHOLDER,
    CompactCtx,
    CompactResult,
    compact,
    dedup,
    handle_store,
    is_compaction_disabled,
    telemetry_record,
)
from tx_core.constants import ANSI_RE
from tx_core.marker import strip_run_markers
from tx_core.output import apply_redactions, err
from tx_core.state import pane_log_path, require_pane
from tx_core.tmux import find_pane_anywhere, get_server


def _read_cleaned_text(log_path: Path, start_offset: int, end_offset: int, keep_ansi: bool = False) -> str:
    with open(log_path, "rb") as f:
        f.seek(start_offset)
        raw = f.read(max(0, end_offset - start_offset))
    text = raw.decode("utf-8", errors="replace")
    if not keep_ansi:
        text = ANSI_RE.sub("", text)
    text = text.replace("\r", "")
    return text


def _strip_lines(text: str, strip_blanks: bool) -> list[str]:
    lines = text.split("\n")
    if not strip_blanks:
        return lines
    out: list[str] = []
    blank_run = 0
    for line in lines:
        line = line.rstrip()
        if line == "":
            blank_run += 1
            if blank_run <= 2:
                out.append(line)
        else:
            blank_run = 0
            out.append(line)
    while out and out[0].strip() == "":
        out.pop(0)
    while out and out[-1].strip() == "":
        out.pop()
    return out


def _resolve_pane_for_input(
    offsets: dict[str, Any], pane: str
) -> tuple[dict[str, Any], libtmux.Server, libtmux.Pane, Path]:
    """Common preamble for run / exec / stdin: validate pane exists in tmux."""
    state = require_pane(offsets, pane)
    server = get_server()
    tmux_id = state.get("tmux_id", "")
    tmux_pane = find_pane_anywhere(server, tmux_id) if tmux_id else None
    if tmux_pane is None:
        err(f"pane '{pane}' tmux pane missing — has it been killed externally?")
    log_path = pane_log_path(pane)
    if not log_path.exists():
        err(f"log file missing for pane '{pane}' — pane may have been created outside tx-pane")
    return state, server, tmux_pane, log_path


def _render_run_output(
    log_path: Path,
    start_offset: int,
    end_offset: int,
    strip_blanks: bool,
    keep_ansi: bool = False,
    redact_cfg: dict[str, Any] | None = None,
    compact_ctx: CompactCtx | None = None,
    pane_state: dict[str, Any] | None = None,
) -> list[str]:
    """Slice the log, strip markers + echoed wrap lines, return cleaned line list.

    When `compact_ctx` is None (the default), behaves exactly as before:
    ANSI strip → marker strip → redact → blank-collapse. This is the path
    taken when --raw is set, when default_mode=="raw", or when the caller
    has not yet been migrated to the compaction surface.

    When `compact_ctx` is provided, the compaction pipeline runs after
    redaction and before the legacy `_strip_lines` blank-collapse. The
    result object is attached to `compact_ctx.result` so callers can read
    the tier / handle / footer without changing the return type yet.

    When ``pane_state`` is also provided and L4 elided content, a handle
    is allocated on the pane's compact.handles dict and the placeholder
    in the emitted text is swapped for the real handle id. Caller must
    hold the offsets lock and save afterwards.
    """
    raw_text = _read_cleaned_text(log_path, start_offset, end_offset, keep_ansi=keep_ansi)
    raw_text = strip_run_markers(raw_text)
    if redact_cfg is not None:
        raw_text = apply_redactions(raw_text, redact_cfg)
    if compact_ctx is not None and not is_compaction_disabled():
        result = compact(raw_text, compact_ctx)
        raw_text = _maybe_attach_handle(
            result, compact_ctx, log_path, start_offset, end_offset,
            pane_state,
        )
        raw_text = _maybe_apply_dedup(raw_text, result, compact_ctx, pane_state, redact_cfg)
        compact_ctx.result = result  # type: ignore[attr-defined]
        _emit_telemetry(redact_cfg, compact_ctx, result)
    return _strip_lines(raw_text, strip_blanks)


def _render_buffer_output(
    text: str,
    strip_blanks: bool,
    compact_ctx: CompactCtx | None = None,
    full_cfg: dict[str, Any] | None = None,
    pane_state: dict[str, Any] | None = None,
    log_path: Path | None = None,
    start_offset: int | None = None,
    end_offset: int | None = None,
) -> list[str]:
    """Sibling of `_render_run_output` for the bypass paths.

    Used by `tx-pane tail` / `tx-pane dump` / `tx-pane wait` / `tx-pane grep` / `tx-pane stream`
    which slice the log themselves (not via a marker pair) and have
    already cleaned ANSI + stripped markers. They feed the cleaned text
    straight in; compaction is optional via `compact_ctx`.
    """
    if compact_ctx is not None and not is_compaction_disabled():
        result = compact(text, compact_ctx)
        if log_path is not None and start_offset is not None and end_offset is not None:
            text = _maybe_attach_handle(
                result, compact_ctx, log_path, start_offset, end_offset,
                pane_state, kind="buffer",
            )
        else:
            text = result.text
        text = _maybe_apply_dedup(text, result, compact_ctx, pane_state, full_cfg)
        compact_ctx.result = result  # type: ignore[attr-defined]
        _emit_telemetry(full_cfg, compact_ctx, result)
    return _strip_lines(text, strip_blanks)


def _maybe_attach_handle(
    result: CompactResult,
    ctx: CompactCtx,
    log_path: Path,
    start_offset: int,
    end_offset: int,
    pane_state: dict[str, Any] | None,
    kind: str = "run",
) -> str:
    """If L4 elided, allocate a handle and swap the placeholder in `result.text`.

    Returns the (possibly placeholder-swapped) text. When pane_state is
    None or L4 didn't elide, returns result.text verbatim. Best-effort:
    a handle-store failure falls back to leaving the placeholder string
    (visible in output) — better than crashing the call path.
    """
    l4 = getattr(result, "l4", None)
    if l4 is None or not getattr(l4, "elided", False) or pane_state is None:
        return result.text
    try:
        hid = handle_store.store_handle(
            pane_state,
            kind=kind,
            run_id=ctx.run_id,
            log_path=str(log_path),
            start_offset=int(start_offset),
            end_offset=int(end_offset),
            applied_layers=list(result.applied_layers),
            normalizer=None,
            raw_lines=int(l4.raw_lines),
        )
    except Exception:
        return result.text
    # Remember on the result so a follow-up dedup pass can reference it.
    result.handle = hid  # type: ignore[attr-defined]
    return result.text.replace(HANDLE_PLACEHOLDER, hid)


def _maybe_apply_dedup(
    text: str,
    result: CompactResult,
    ctx: CompactCtx,
    pane_state: dict[str, Any] | None,
    cfg: dict[str, Any] | None,
) -> str:
    """L5 — cross-call dedup. Returns the possibly-short-circuited text.

    Disabled by default (ships in P5 with `[compact.dedup] enabled = False`).
    When enabled: a content-hash lookup against the pane's small bounded
    cache. On hit the original `text` is replaced by a single line
    naming the prior run + handle.
    """
    if pane_state is None or cfg is None:
        return text
    dedup_cfg = (cfg.get("compact") or {}).get("dedup") or {}
    if not dedup_cfg.get("enabled", False):
        return text
    ttl = int(dedup_cfg.get("ttl_seconds", 60))
    cap = int(dedup_cfg.get("cache_size_per_pane", 32))
    hit = dedup.lookup(pane_state, text, ttl_seconds=ttl)
    if hit is not None:
        # Hit. Replace body with the short reference line; reuse the
        # prior handle. The agent can still recover via tx-pane output --handle.
        result.applied_layers.append("L5")  # type: ignore[attr-defined]
        result.notes.append(  # type: ignore[attr-defined]
            f"L5 dedup hit: matches r-{hit.prior_run_id} from {hit.age_seconds:.0f}s ago"
        )
        return dedup.dedup_short_message(hit)
    # Miss → remember.
    try:
        dedup.remember(
            pane_state,
            text=text,
            run_id=ctx.run_id,
            handle=getattr(result, "handle", None),
            max_entries=cap,
        )
    except Exception:
        pass
    return text


def _apply_range_grep(
    lines: list[str],
    line_range: str | None,
    grep_pat: str | None,
    grep_context: int,
) -> list[str]:
    """Post-filter rendered lines via `tx-pane output --range` / `--grep`.

    --range N-M    → lines[N:M+1] (0-based, inclusive). Out-of-range
                    indices clamp at the boundaries.
    --grep PAT     → matching lines + ±context_lines. Returns an
                    interleaved view with `---` separators between
                    non-contiguous match groups.
    Both can be combined: --range first, then --grep inside the slice.
    """
    if line_range:
        m = re.match(r"^(\d+)-(\d+)$", line_range.strip())
        if not m:
            err(f"--range must be of the form N-M (got '{line_range}')")
        lo = max(0, int(m.group(1)))
        hi = max(lo, int(m.group(2)))
        lines = lines[lo:hi + 1]
    if grep_pat:
        try:
            pat = re.compile(grep_pat)
        except re.error as e:
            err(f"invalid --grep regex: {e}")
        match_idx = [i for i, l in enumerate(lines) if pat.search(l)]
        if not match_idx:
            return [f"[no matches for /{grep_pat}/]"]
        ctx = max(0, int(grep_context))
        keep: set[int] = set()
        for i in match_idx:
            for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
                keep.add(j)
        out: list[str] = []
        prev = -2
        for j in sorted(keep):
            if prev >= 0 and j != prev + 1:
                out.append("---")
            out.append(lines[j])
            prev = j
        lines = out
    return lines


def _emit_handle_buffer(
    pane: str,
    hrec: dict[str, Any],
    cfg: dict[str, Any],
    max_lines: int,
    strip_blanks: bool,
    keep_ansi_resolved: bool,
    line_range: str | None,
    grep_pat: str | None,
    grep_context: int,
    full_flag: bool,
    as_json: bool,
) -> None:
    """Re-render the byte range a buffer-handle points at.

    Used when `tx-pane output --handle h-XXXX` resolves to a non-run handle
    (tail/dump/wait/grep emissions, where there's no run_id). Reads the
    same byte range from the log; with --full it skips compaction.
    """
    log_path = Path(hrec["log_path"])
    start = int(hrec["start_offset"])
    end = int(hrec["end_offset"])
    raw_text = _read_cleaned_text(log_path, start, end, keep_ansi=keep_ansi_resolved)
    raw_text = strip_run_markers(raw_text)
    raw_text = apply_redactions(raw_text, cfg)
    # --full → no compaction. Without it, re-apply L1/L2/L3 (but not
    # L4 — we don't want to re-elide on re-fetch).
    if not full_flag and not is_compaction_disabled():
        ctx = CompactCtx(
            mode="terse", cmd="", pane=pane, run_id=None,
            strip_banners=True, collapse_repeats=True,
            repeat_threshold=int(cfg.get("compact", {}).get("collapse_repeats_threshold", 3)),
            token_budget=None,  # disable L4 on re-fetch
        )
        result = compact(raw_text, ctx)
        raw_text = result.text
    kept = _strip_lines(raw_text, strip_blanks)
    kept = _apply_range_grep(kept, line_range, grep_pat, grep_context)
    shown = kept[:max_lines]
    remainder = kept[max_lines:]
    if as_json:
        rec = {
            "pane": pane,
            "handle": None,
            "stdout": "\n".join(shown),
            "truncated": bool(remainder),
            "kind": hrec.get("kind", "buffer"),
        }
        click.echo(json.dumps(rec, indent=2))
        return
    click.echo("\n".join(shown), color=keep_ansi_resolved or None)
    if remainder:
        click.echo(f"[truncated: {len(remainder)} lines remain]")


def _emit_telemetry(
    cfg: dict[str, Any] | None,
    ctx: CompactCtx,
    result: CompactResult,
) -> None:
    """Append one telemetry record. Best-effort, never raises.

    The caller passes whichever config dict is at hand — `redact_cfg`
    in `_render_run_output` and the explicit `full_cfg` in
    `_render_buffer_output`. Both are the full config object, but we
    accept None to keep the helpers' signatures flexible.
    """
    if cfg is None:
        # Default to enabled when caller didn't pass cfg — the call path
        # exists for tests / internal use; in normal flow cfg is always set.
        enabled = True
        max_size_mb = 10
    else:
        tel = (cfg.get("compact") or {}).get("telemetry") or {}
        enabled = bool(tel.get("enabled", True))
        max_size_mb = int(tel.get("max_size_mb", 10))
    try:
        telemetry_record(ctx, result, enabled=enabled, max_size_mb=max_size_mb)
    except Exception:
        pass


def _compact_options(fn):
    """Decorator: attach the standard compaction CLI options to a click command.

    Applied to every emission entry point so the flags are uniformly
    available. Default mode comes from `[compact] default_mode` in
    config (ships as "terse"; use --raw as the escape hatch).
    """
    fn = click.option(
        "--raw", "raw_flag", is_flag=True, default=False,
        help="bypass all compaction layers; return cleaned bytes verbatim (escape hatch)",
    )(fn)
    fn = click.option(
        "--terse", "terse_flag", is_flag=True, default=False,
        help="enable compaction layers (L1 hygiene, L2 whitespace) on this call",
    )(fn)
    fn = click.option(
        "--token-budget", "token_budget_flag", type=int, default=None,
        help="override the L4 token budget for this call",
    )(fn)
    fn = click.option(
        "--no-strip-banners", "no_strip_banners_flag", is_flag=True, default=False,
        help="skip the L1 banner registry on this call",
    )(fn)
    fn = click.option(
        "--no-collapse-repeats", "no_collapse_repeats_flag", is_flag=True, default=False,
        help="skip L3 repeated-line collapse on this call",
    )(fn)
    fn = click.option(
        "--no-normalize", "no_normalize_flag", is_flag=True, default=False,
        help="skip the tool-specific normalizer on this call (still applies L1-L5)",
    )(fn)
    return fn


def _per_call_compact_mode(raw_flag: bool, terse_flag: bool) -> str | None:
    """Translate --raw / --terse flags into a per-call mode override.

    Returns "raw" / "terse" / None. None means "fall through to per-pane
    / global default". --raw wins over --terse if both are set.
    """
    if raw_flag:
        return "raw"
    if terse_flag:
        return "terse"
    return None


def _resolve_compact_mode(cfg: dict[str, Any], pane_state: dict[str, Any] | None,
                          per_call_mode: str | None) -> str:
    """Resolve compaction mode using the per-call > per-pane > global order.

    Returns one of "raw", "terse", "summary". Caller checks the result
    against "raw" to decide whether to build a CompactCtx or pass None.
    """
    if per_call_mode is not None:
        return per_call_mode
    if pane_state is not None:
        ps = pane_state.get("compact") or {}
        if ps.get("mode"):
            return ps["mode"]
    return cfg.get("compact", {}).get("default_mode", "terse")


def _build_compact_ctx(
    cfg: dict[str, Any],
    pane_state: dict[str, Any] | None,
    pane: str | None,
    cmd: str,
    run_id: str | None,
    per_call_mode: str | None,
    per_call_no_normalize: bool = False,
    per_call_no_strip_banners: bool = False,
    per_call_no_collapse_repeats: bool = False,
    must_keep_re: list[re.Pattern[str]] | None = None,
) -> CompactCtx | None:
    """Build a CompactCtx for an emission, or return None for raw passthrough.

    Returns None when mode is "raw" (or env-var disabled), which signals
    the caller to skip compaction entirely.
    """
    if is_compaction_disabled():
        return None
    mode = _resolve_compact_mode(cfg, pane_state, per_call_mode)
    if mode == "raw":
        return None
    cc = cfg.get("compact", {})
    ps = (pane_state or {}).get("compact") or {}
    disabled = list(ps.get("disabled_normalizers") or [])
    # Combine caller-supplied must_keep regex (e.g. the `tx-pane wait` pattern,
    # design Q6) with the config-seeded defaults (error/commit-sha/etc.).
    seed_patterns = cc.get("must_keep_patterns", []) or []
    seeded: list[re.Pattern[str]] = []
    for p in seed_patterns:
        try:
            seeded.append(re.compile(p))
        except re.error:
            continue  # bad user-supplied regex — skip silently rather than crash
    must_keep_all: list[re.Pattern[str]] = list(must_keep_re or []) + seeded
    # Pre-compute the shell wrap-echo so L1 can strip it from the body.
    # Mirrors wrap_command(): `__tx_run_id={run_id}; {cmd}`.
    cleaned_cmd_echo = None
    if run_id and cmd:
        cleaned_cmd_echo = f"__tx_run_id={run_id}; {cmd}"

    ctx = CompactCtx(
        mode=mode,
        cmd=cmd or "",
        pane=pane,
        run_id=run_id,
        token_budget=ps.get("token_budget") or cc.get("default_token_budget"),
        strip_banners=(not per_call_no_strip_banners) and bool(cc.get("strip_banners", True)),
        collapse_repeats=(not per_call_no_collapse_repeats) and bool(cc.get("collapse_repeats", True)),
        repeat_threshold=int(cc.get("collapse_repeats_threshold", 3)),
        must_keep=must_keep_all,
        disabled_normalizers=disabled,
        cleaned_cmd_echo=cleaned_cmd_echo,
        prompt_patterns=[re.compile(p) for p in cfg.get("defaults", {}).get("prompt_patterns", [])],
        verbose=bool(os.environ.get("TX_PANE_DEBUG")),
    )
    if per_call_no_normalize:
        # Placeholder for P4 where the registry exists; here it just signals
        # "skip any tool-specific normalizer". L1/L2 still fire.
        ctx.disabled_normalizers = ["*"]
    return ctx


def _duration_ms(started: str | None, ended: str | None) -> int | None:
    if not started or not ended:
        return None
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        e = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        return int(round((e - s).total_seconds() * 1000))
    except Exception:
        return None


def _emit_run_json(
    pane: str,
    run_id: str,
    cmd: str,
    started: str | None,
    ended: str | None,
    exit_code: int | None,
    stdout_lines: list[str],
    truncated: bool,
    notes: list[str] | None = None,
) -> None:
    """Emit the canonical Stage-3 run JSON record (single completed run)."""
    record = {
        "pane": pane,
        "run_id": run_id,
        "cmd": cmd,
        "started": started,
        "ended": ended,
        "exit": exit_code,
        "duration_ms": _duration_ms(started, ended),
        "stdout": "\n".join(stdout_lines),
        "truncated": bool(truncated),
    }
    if notes:
        record["notes"] = list(notes)
    click.echo(json.dumps(record, indent=2))
