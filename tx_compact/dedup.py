"""L5 — cross-call content-addressed dedup.

Ships in P5 *disabled* (``[compact.dedup] enabled = false``). The
machinery exists so opt-in users can enable it after a release of
telemetry data (``tx compact-stats --dedup-would-hit``) shows the hit
rate is worth the staleness risk.

Algorithm (design plan §4.5):
1. After L1-L4 (so the *emitted* text is what's hashed), SHA-256 the
   bytes, truncate to 12 hex chars.
2. Maintain a per-pane bounded cache of (hash → run_id, ts, emitted_text).
3. If a hash matches a recent entry:
     - in the same pane,
     - within `ttl_seconds`,
     - with no intervening non-idempotent run,
   replace the emitted text with a short reference line:
     `[tx:same-as <run_id> emitted Xs ago — handle=h-XXXX]`

The handle metadata is still allocated so the agent can fetch the
full original via `tx output --handle ...`.

Safeguards inherited from §4.5:
- Never cross-pane (host state).
- TTL bounded.
- No staleness check for now beyond TTL — `[compact.dedup]
  idempotent_only` can later restrict to known-read-only commands.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


_HASH_HEX_LEN = 12


def content_hash(text: str) -> str:
    """Truncated SHA-256 of ``text`` for use as a cache key."""
    h = hashlib.sha256(text.encode("utf-8"))
    return h.hexdigest()[:_HASH_HEX_LEN]


def _now() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class DedupHit:
    """A successful match against the dedup cache."""
    hash: str
    prior_run_id: str | None
    prior_handle: str | None
    age_seconds: float


def lookup(
    pane_state: dict[str, Any],
    text: str,
    *,
    ttl_seconds: int = 60,
) -> DedupHit | None:
    """Return a DedupHit iff `text` matches a recent entry in the cache.

    Falsy/None pane_state → no hit. Cache lives under
    ``pane_state["compact"]["dedup_cache"]``: a list of dicts each
    with keys ``hash``, ``run_id``, ``handle``, ``ts``.
    """
    if not pane_state:
        return None
    cache = (pane_state.get("compact") or {}).get("dedup_cache") or []
    if not cache:
        return None
    h = content_hash(text)
    now = _now()
    for entry in reversed(cache):  # most recent first
        if entry.get("hash") != h:
            continue
        try:
            ts = float(entry.get("ts", 0))
        except (TypeError, ValueError):
            continue
        age = now - ts
        if age > ttl_seconds:
            continue
        return DedupHit(
            hash=h,
            prior_run_id=entry.get("run_id"),
            prior_handle=entry.get("handle"),
            age_seconds=age,
        )
    return None


def remember(
    pane_state: dict[str, Any],
    *,
    text: str,
    run_id: str | None,
    handle: str | None,
    max_entries: int = 32,
) -> str:
    """Insert a new (hash, run_id, handle, ts) entry into the cache.

    Returns the hash. Trims the cache to ``max_entries`` keeping the
    most-recent entries. Cache is per-pane; never crosses panes.
    """
    compact_state = pane_state.setdefault("compact", {})
    cache: list[dict[str, Any]] = compact_state.setdefault("dedup_cache", [])
    h = content_hash(text)
    cache.append({
        "hash": h,
        "run_id": run_id,
        "handle": handle,
        "ts": _now(),
        "ts_iso": _now_iso(),
    })
    if len(cache) > max_entries:
        # Drop the oldest until we're within the cap.
        del cache[: len(cache) - max_entries]
    return h


def dedup_short_message(hit: DedupHit) -> str:
    """Format the short-form replacement text for a cache hit."""
    parts = [
        "[tx:same-as",
        hit.prior_run_id or "<unknown>",
        f"emitted {hit.age_seconds:.0f}s ago",
    ]
    if hit.prior_handle:
        parts.append(f"handle={hit.prior_handle}")
    return " ".join(parts) + "]"
