"""Per-call telemetry — JSON-lines record of every compaction emission.

Lands in P2 (rather than P4) because the data drives normalizer
prioritisation: when we choose the first 10 of 17 §5.3 normalizers to
ship in P4, P2's telemetry tells us which `cmd_head` values an agent
actually runs against the panes.

File: ``$TX_HOME/compact.jsonl`` (default ``~/.tx/compact.jsonl``).
Rolling cap controlled by ``[compact.telemetry] max_size_mb`` (default
10 MB). When the file exceeds the cap on a write, it's rotated to
``compact.jsonl.1`` (single backup, simpler than logs/ rotation).

**Privacy:** only ``shlex.split(cmd)[0]`` is recorded as ``cmd_head``.
Full command lines, arguments, paths, secrets are not. No network
upload, ever. `tx compact-stats --forget` wipes both the live file and
the rotated backup.

Record schema (one JSON object per line):

    { "ts": "2026-05-14T03:14:01Z",
      "pane": "p1",
      "run_id": "r-abc123",
      "cmd_head": "smartctl",
      "tier": 1,
      "normalizer": "smartctl-attr",   # optional (P4+)
      "layers": ["L1", "L2", "L3"],
      "in_bytes": 4380,
      "out_bytes": 740,
      "saved_pct": 83.1 }
"""

from __future__ import annotations

import json
import os
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .api import CompactCtx, CompactResult
from .tier import Tier


def _tx_home() -> Path:
    return Path(os.environ.get("TX_HOME") or str(Path.home() / ".tx"))


def telemetry_path() -> Path:
    return _tx_home() / "compact.jsonl"


def telemetry_backup_path() -> Path:
    return _tx_home() / "compact.jsonl.1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cmd_head(cmd: str) -> str:
    """Privacy filter: return only the first token of the command."""
    cmd = (cmd or "").strip()
    if not cmd:
        return ""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        # Unbalanced quotes etc. — fall back to whitespace split.
        parts = cmd.split()
    if not parts:
        return ""
    head = parts[0]
    # Some agents prefix with `sudo` — record both for accurate stats.
    if head == "sudo" and len(parts) >= 2:
        return f"sudo:{parts[1]}"
    if head == "env" and len(parts) >= 2:
        # `env FOO=bar cmd ...` — the second arg might be an env assignment.
        for p in parts[1:]:
            if "=" not in p:
                return p
    return head


def record(
    ctx: CompactCtx,
    result: CompactResult,
    *,
    enabled: bool = True,
    max_size_mb: int = 10,
) -> None:
    """Append one telemetry record for this compaction call.

    Best-effort: any I/O exception is swallowed (telemetry must never
    break the agent-facing call path). Rotation happens before write
    when the file would exceed ``max_size_mb``.
    """
    if not enabled:
        return
    if os.environ.get("TX_NO_TELEMETRY") == "1":
        return

    try:
        rec = {
            "ts": _now_iso(),
            "pane": ctx.pane,
            "run_id": ctx.run_id,
            "cmd_head": _cmd_head(ctx.cmd),
            "tier": int(result.tier),
            "mode": ctx.mode,
            "layers": list(result.applied_layers),
            "in_bytes": int(result.in_bytes),
            "out_bytes": int(result.out_bytes),
            "saved_pct": round(result.saved_pct, 2),
        }
        path = telemetry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate-on-overflow check (cheap stat).
        try:
            if path.exists() and path.stat().st_size >= max_size_mb * 1024 * 1024:
                backup = telemetry_backup_path()
                if backup.exists():
                    backup.unlink()
                path.rename(backup)
        except OSError:
            pass
        with open(path, "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")))
            f.write("\n")
    except Exception:
        # Telemetry is best-effort. Never propagate.
        return


def read_all() -> Iterator[dict[str, Any]]:
    """Yield every record (current file + rotated backup), oldest first."""
    for p in (telemetry_backup_path(), telemetry_path()):
        if not p.exists():
            continue
        try:
            with open(p, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


def wipe() -> int:
    """Delete both telemetry files. Returns the number of files removed."""
    removed = 0
    for p in (telemetry_path(), telemetry_backup_path()):
        try:
            if p.exists():
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def aggregate(
    records: Iterator[dict[str, Any]],
    *,
    since_ts: str | None = None,
) -> dict[str, Any]:
    """Compute per-``cmd_head`` aggregates from a stream of records.

    Result shape::

        {
          "count": int,
          "in_bytes": int, "out_bytes": int, "saved_pct": float,
          "by_cmd_head": {
            "smartctl": {"count": 8, "in": 35040, "out": 5920, "saved_pct": 83.1,
                          "tiers": {1: 8, 2: 0, 3: 0}},
            ...
          },
          "passthrough_cmd_heads": [("apt", 12), ...]   # cmd_heads with tier 3
        }
    """
    total_count = 0
    total_in = 0
    total_out = 0
    per_head: dict[str, dict[str, Any]] = {}

    for rec in records:
        if since_ts is not None and rec.get("ts", "") < since_ts:
            continue
        head = rec.get("cmd_head") or "<unknown>"
        tier = int(rec.get("tier", 1))
        in_b = int(rec.get("in_bytes", 0))
        out_b = int(rec.get("out_bytes", 0))
        total_count += 1
        total_in += in_b
        total_out += out_b
        slot = per_head.setdefault(head, {
            "count": 0, "in": 0, "out": 0, "tiers": {1: 0, 2: 0, 3: 0},
        })
        slot["count"] += 1
        slot["in"] += in_b
        slot["out"] += out_b
        slot["tiers"][tier] = slot["tiers"].get(tier, 0) + 1

    for head, slot in per_head.items():
        slot["saved_pct"] = (
            100.0 * (slot["in"] - slot["out"]) / slot["in"] if slot["in"] else 0.0
        )

    passthrough_heads = sorted(
        (
            (head, slot["tiers"].get(3, 0))
            for head, slot in per_head.items()
            if slot["tiers"].get(3, 0) > 0
        ),
        key=lambda kv: -kv[1],
    )

    return {
        "count": total_count,
        "in_bytes": total_in,
        "out_bytes": total_out,
        "saved_pct": (100.0 * (total_in - total_out) / total_in) if total_in else 0.0,
        "by_cmd_head": per_head,
        "passthrough_cmd_heads": passthrough_heads,
    }
