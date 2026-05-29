"""Handle store — opaque h-XXXX ids the agent can use to retrieve more.

Handles are the reversibility contract of the compaction stage: when
L4 elides content, it emits a handle the agent can pass back to
`tx-pane output --handle h-XXXX --range N-M` to recover any slice of the
original output.

A handle is *not* a hash of the content. It's a key into per-pane
state stored under offsets.json::<pane>.compact.handles. The full
content is *already* on disk at $TX_PANE_HOME/logs/<pane>.log between
start_offset and end_offset — the handle just records which bytes.

Lifecycle:
- Created when L4 elides (or when buffer paths bypass and we want
  a future --grep/--range to work).
- Stored in offsets.json under the pane.
- Reused for the lifetime of the run record (max_run_history, default
  100 runs). When the run rotates out, its handle is purged.
- Survives daemon restarts because offsets.json is the source of truth.

Schema (see design §7.1, §7.3):
    {
      "kind": "run" | "buffer",
      "run_id": "r-abc123" | null,
      "log_path": "/abs/path/to/p1.log",
      "start_offset": int,
      "end_offset":   int,
      "applied_layers": ["L1", "L2", "L4"],
      "normalizer": "zpool-status" | null,
      "created": "2026-05-14T03:14:01Z",
      "raw_lines": 12400
    }
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Any


_HANDLE_BYTES = 4   # 8 hex chars after the "h-" prefix


def alloc_handle_id(prefix: str = "h-") -> str:
    """Allocate a short opaque handle id. Collision-safe within a pane
    because we re-roll on duplicate inside ``store_handle``."""
    return f"{prefix}{secrets.token_hex(_HANDLE_BYTES)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def store_handle(
    pane_state: dict[str, Any],
    *,
    kind: str,
    run_id: str | None,
    log_path: str,
    start_offset: int,
    end_offset: int,
    applied_layers: list[str],
    normalizer: str | None = None,
    raw_lines: int | None = None,
    max_handles: int = 100,
) -> str:
    """Allocate + persist a handle on the given pane state dict.

    Returns the handle id. Caller is responsible for saving the offsets
    via the existing lock (this function only mutates the in-memory
    state).

    If ``len(handles) > max_handles``, the oldest entries are evicted
    (sorted by created timestamp).
    """
    compact_state = pane_state.setdefault("compact", {})
    handles: dict[str, Any] = compact_state.setdefault("handles", {})

    # Collision retry — vanishingly unlikely but cheap insurance.
    for _ in range(10):
        hid = alloc_handle_id()
        if hid not in handles:
            break
    else:
        # 10 collisions in a row → handle space is unhealthily small
        # for this pane. Caller can re-allocate after wipe.
        raise RuntimeError("handle id collision storm — wipe pane handles")

    handles[hid] = {
        "kind": kind,
        "run_id": run_id,
        "log_path": str(log_path),
        "start_offset": int(start_offset),
        "end_offset": int(end_offset),
        "applied_layers": list(applied_layers),
        "normalizer": normalizer,
        "created": _now_iso(),
        "raw_lines": raw_lines,
    }

    # GC: keep the most-recent max_handles by created timestamp.
    if len(handles) > max_handles:
        items = sorted(handles.items(), key=lambda kv: kv[1].get("created", ""))
        # Drop oldest down to max_handles.
        for key, _ in items[: len(handles) - max_handles]:
            del handles[key]

    return hid


def find_handle(pane_state: dict[str, Any], handle_id: str) -> dict[str, Any] | None:
    """Return the handle record for ``handle_id`` or None if missing."""
    if not pane_state:
        return None
    handles = (pane_state.get("compact") or {}).get("handles") or {}
    rec = handles.get(handle_id)
    return rec if isinstance(rec, dict) else None


def gc_handles_for_rotated_runs(
    pane_state: dict[str, Any],
    live_run_ids: set[str],
) -> int:
    """Drop handles whose run_id is no longer in the pane's runs list.

    Buffer handles (run_id == None) are not touched here; they live by
    the ``max_handles`` GC inside ``store_handle``.

    Returns the number of handles removed.
    """
    compact_state = pane_state.get("compact")
    if not compact_state:
        return 0
    handles: dict[str, Any] = compact_state.get("handles") or {}
    to_drop = [
        hid for hid, rec in handles.items()
        if rec.get("kind") == "run"
        and rec.get("run_id") is not None
        and rec["run_id"] not in live_run_ids
    ]
    for hid in to_drop:
        del handles[hid]
    return len(to_drop)
