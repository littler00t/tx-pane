"""Filesystem-backed config + offsets I/O, with the exclusive run-state lock.

`load_config` / `load_offsets` / `save_offsets` are the only entry points
to ~/.tx-pane/config.toml and ~/.tx-pane/offsets.json. The `_OffsetsLock` context
manager (obtained via `offsets_lock()`) is the only sanctioned way to
read-modify-write offsets across concurrent tx-pane invocations.

Run-lifecycle bookkeeping (record_run_start / record_run_end / find_run_record)
lives here too because it operates on the in-memory `state` dict that
load/save_offsets round-trips.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import tomllib
from datetime import datetime, timezone
from typing import Any

import tomli_w

from tx_core.constants import (
    CONFIG_PATH,
    DEFAULT_CONFIG,
    LOCK_PATH,
    LOGS_DIR,
    OFFSETS_PATH,
    TX_DIR,
)


def ensure_dirs() -> None:
    TX_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _deepcopy(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _deepcopy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deepcopy(v) for v in obj]
    return obj


def load_config() -> dict[str, Any]:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, "wb") as f:
            tomli_w.dump(DEFAULT_CONFIG, f)
        return _deepcopy(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "rb") as f:
        loaded = tomllib.load(f)
    cfg = _deepcopy(DEFAULT_CONFIG)
    for section, values in loaded.items():
        if isinstance(values, dict) and section in cfg and isinstance(cfg[section], dict):
            cfg[section].update(values)
        else:
            cfg[section] = values
    return cfg


def load_offsets() -> dict[str, Any]:
    ensure_dirs()
    if not OFFSETS_PATH.exists():
        return {"_next_id": 1, "_panes": {}}
    try:
        with open(OFFSETS_PATH, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"_next_id": 1, "_panes": {}}
    data.setdefault("_next_id", 1)
    data.setdefault("_panes", {})
    # Note: per-pane "compact" state (mode/budget overrides, handle store,
    # dedup ring buffer) is migrated lazily by consumers — absence ≡
    # legacy defaults, and `_build_compact_ctx` handles missing keys
    # via .get(..., default). Keeping load_offsets a pure read-through
    # preserves the save → load idempotency tested in test_state.py.
    return data


def save_offsets(offsets: dict[str, Any]) -> None:
    ensure_dirs()
    fd, tmp_path = tempfile.mkstemp(prefix=".offsets.", suffix=".tmp", dir=str(TX_DIR))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(offsets, f, indent=2)
        os.replace(tmp_path, OFFSETS_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


class _OffsetsLock:
    """Per-process exclusive lock around offsets.json read-modify-write cycles.

    Uses a separate lock file so the JSON itself can be atomically replaced via
    rename without losing the lock. Acquired exclusively, released on __exit__.
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    def __enter__(self) -> "_OffsetsLock":
        ensure_dirs()
        self._fd = os.open(str(LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def offsets_lock() -> _OffsetsLock:
    """Return a context manager that holds the exclusive offsets lock."""
    return _OffsetsLock()


def now_iso() -> str:
    """ISO-8601 UTC timestamp, second precision, ending in 'Z'."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def record_run_start(state: dict[str, Any], run_id: str, cmd: str, start_offset: int, max_wait: float) -> None:
    state["active_run"] = {
        "id": run_id,
        "cmd": cmd,
        "started": now_iso(),
        "start_offset": start_offset,
        "max_wait_seconds": max_wait,
    }


def record_run_end(
    state: dict[str, Any], run_id: str, exit_code: int | None, end_offset: int, max_history: int
) -> None:
    runs = state.get("runs") or []
    # Idempotent: if a concurrent invocation already recorded this run, just
    # ensure active_run is cleared and return.
    for r in runs:
        if r.get("id") == run_id and r.get("end_offset") is not None:
            state["active_run"] = None
            state["runs"] = runs
            if exit_code is None:
                state["hook_ok"] = False
            return
    active = state.get("active_run") or {}
    entry = {
        "id": run_id,
        "cmd": active.get("cmd", ""),
        "started": active.get("started", now_iso()),
        "ended": now_iso(),
        "exit": exit_code,
        "start_offset": int(active.get("start_offset", 0)),
        "end_offset": int(end_offset),
    }
    runs.append(entry)
    if max_history > 0 and len(runs) > max_history:
        runs = runs[-max_history:]
    state["runs"] = runs
    state["active_run"] = None
    # exit_code None means the marker wasn't observed but the prompt returned
    # (prompt-pattern fallback). That's the canonical signal that the marker
    # hook isn't wired into the current foreground shell — flag the pane so
    # the next tx-pane run can auto-reinstall.
    if exit_code is None:
        state["hook_ok"] = False
    else:
        # Successful marker observation implies the hook is alive (re-set to
        # True in case a transient miss was recorded earlier).
        state["hook_ok"] = True


def find_run_record(state: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    """Return the run record matching run_id (active or historical), or None."""
    active = state.get("active_run") or {}
    if active.get("id") == run_id:
        return {**active, "exit": None, "end_offset": None}
    for r in state.get("runs") or []:
        if r.get("id") == run_id:
            return r
    return None
