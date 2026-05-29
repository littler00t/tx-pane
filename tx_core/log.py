"""Log file rotation, sweeping, and raw-bytes-to-cleaned-lines processing.

Owns the lifecycle of `<pane>.log` rotation (size threshold → `.1`, `.2`, …;
age-based sweep of rotated copies) and the bytes-to-text pipeline that
turns the on-disk pipe-pane capture into the cleaned `list[str]` that
`tx-pane tail` / `tx-pane dump` / run rendering all consume.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tx_core.config import now_iso
from tx_core.constants import ANSI_RE, LOGS_DIR


def _logs_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve the [logs] section with defaults filled in (for callers that
    loaded a partial config)."""
    out = {
        "max_size_mb": 100,
        "max_age_days": 30,
        "max_keep": 10,
        "sweep_interval_hours": 24,
    }
    section = cfg.get("logs") if isinstance(cfg, dict) else None
    if isinstance(section, dict):
        for k in out:
            if k in section:
                try:
                    out[k] = float(section[k]) if k == "sweep_interval_hours" else int(section[k])
                except (TypeError, ValueError):
                    pass
    return out


def _rotated_log_paths(log_path: Path) -> list[Path]:
    """All `<id>.log.N` files for the given log_path, sorted by N ascending."""
    parent = log_path.parent
    base = log_path.name
    candidates: list[tuple[int, Path]] = []
    if not parent.exists():
        return []
    for p in parent.iterdir():
        if not p.name.startswith(base + "."):
            continue
        tail = p.name[len(base) + 1 :]
        if not tail.isdigit():
            continue
        candidates.append((int(tail), p))
    candidates.sort(key=lambda x: x[0])
    return [p for _, p in candidates]


def rotate_log(log_path: Path, max_keep: int) -> Path | None:
    """Rename log_path -> log_path.1, shifting any existing .N -> .N+1.
    Drops rotated files past `max_keep`. Returns the path of the newly-created
    `.1` file (or None if the source didn't exist / was empty).
    """
    if not log_path.exists() or log_path.stat().st_size == 0:
        return None
    # Shift existing rotated copies up.
    existing = _rotated_log_paths(log_path)
    # Shift from highest to lowest to avoid clobbering.
    for n, p in sorted([(int(p.name[len(log_path.name) + 1 :]), p) for p in existing], reverse=True):
        new_n = n + 1
        if max_keep > 0 and new_n > max_keep:
            try:
                p.unlink()
            except OSError:
                pass
            continue
        target = log_path.with_name(f"{log_path.name}.{new_n}")
        try:
            p.rename(target)
        except OSError:
            pass
    rotated = log_path.with_name(f"{log_path.name}.1")
    try:
        log_path.rename(rotated)
    except OSError:
        return None
    # Recreate the empty source so pipe-pane has a target.
    try:
        log_path.touch()
    except OSError:
        pass
    return rotated


def maybe_rotate_log(log_path: Path, cfg: dict[str, Any]) -> Path | None:
    """Rotate log_path if its size exceeds the configured max_size_mb."""
    lc = _logs_cfg(cfg)
    max_bytes = max(0, int(lc["max_size_mb"])) * 1024 * 1024
    if max_bytes <= 0:
        return None
    try:
        size = log_path.stat().st_size
    except OSError:
        return None
    if size < max_bytes:
        return None
    return rotate_log(log_path, int(lc["max_keep"]))


def sweep_aged_logs(cfg: dict[str, Any], logs_dir: Path | None = None) -> list[Path]:
    """Delete rotated log files older than max_age_days. Returns the deleted
    paths. Does NOT touch the active `<id>.log`."""
    lc = _logs_cfg(cfg)
    days = int(lc["max_age_days"])
    if days <= 0:
        return []
    cutoff = time.time() - (days * 86400)
    target_dir = logs_dir if logs_dir is not None else LOGS_DIR
    if not target_dir.exists():
        return []
    deleted: list[Path] = []
    for p in target_dir.iterdir():
        # Only sweep rotated copies (`*.log.N`), never the live `*.log`.
        parts = p.name.split(".")
        if len(parts) < 3 or not parts[-1].isdigit() or parts[-2] != "log":
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            try:
                p.unlink()
                deleted.append(p)
            except OSError:
                pass
    return deleted


def maybe_sweep_aged_logs(offsets: dict[str, Any], cfg: dict[str, Any]) -> list[Path]:
    """Lazy sweep: only runs if the last sweep was more than
    `sweep_interval_hours` ago. Stamps the timestamp in offsets on completion.
    Caller is responsible for save_offsets.
    """
    lc = _logs_cfg(cfg)
    interval_s = float(lc["sweep_interval_hours"]) * 3600.0
    last_iso = offsets.get("_last_sweep")
    if last_iso:
        try:
            last_t = datetime.fromisoformat(last_iso.replace("Z", "+00:00")).timestamp()
            if (time.time() - last_t) < interval_s:
                return []
        except (TypeError, ValueError):
            pass
    deleted = sweep_aged_logs(cfg)
    offsets["_last_sweep"] = now_iso()
    return deleted


def _split_raw_by_newlines(raw: bytes) -> list[tuple[bytes, int]]:
    out: list[tuple[bytes, int]] = []
    pos = 0
    n = len(raw)
    while pos < n:
        nl = raw.find(b"\n", pos)
        if nl == -1:
            out.append((raw[pos:], n))
            break
        out.append((raw[pos:nl], nl + 1))
        pos = nl + 1
    return out


def _clean_line(line_bytes: bytes) -> str:
    s = line_bytes.decode("utf-8", errors="replace")
    s = ANSI_RE.sub("", s)
    s = s.replace("\r", "")
    return s


def process_raw_log(
    raw: bytes, max_lines: int, strip_blanks: bool
) -> tuple[list[str], bool, int, int]:
    """Process raw bytes from a log.

    Returns (kept_lines, truncated, remaining_count, consumed_raw_offset).
    consumed_raw_offset is the byte offset within `raw` representing the end of the
    region we've consumed. When not truncated this equals len(raw); when truncated
    it's the offset right after the last raw line that contributed to kept_lines.
    """
    raw_lines = _split_raw_by_newlines(raw)
    kept: list[str] = []
    remaining = 0
    blank_run = 0
    consumed = 0
    threshold_hit = False

    for line_bytes, end_offset in raw_lines:
        s = _clean_line(line_bytes)
        if strip_blanks:
            s = s.rstrip()
            if s == "":
                blank_run += 1
                if blank_run > 2:
                    if not threshold_hit:
                        consumed = end_offset
                    continue
            else:
                blank_run = 0

        if threshold_hit:
            if not strip_blanks or s.strip() != "":
                remaining += 1
        else:
            kept.append(s)
            consumed = end_offset
            if len(kept) >= max_lines:
                threshold_hit = True

    if strip_blanks:
        while kept and kept[0].strip() == "":
            kept.pop(0)
        if not threshold_hit:
            while kept and kept[-1].strip() == "":
                kept.pop()

    truncated = threshold_hit and remaining > 0
    if not truncated:
        consumed = len(raw)

    return kept, truncated, remaining, consumed
