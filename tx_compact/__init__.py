"""tx_compact — agent-facing output compaction.

Public surface used by the `tx-pane` script:

    from tx_compact import compact, CompactCtx, CompactResult, Tier

Design contract (see docs/tx-doc-compaction.md):

- `compact(text, ctx)` is a pure function — no I/O, no global state.
  Determinism makes the layer trivially testable and trivially reversible
  (re-render from the on-disk log produces the same output).
- TX_PANE_NO_COMPACT=1 short-circuits to identity (text unchanged) at the
  entry point. Mirrors rtk's RTK_NO_TOML=1 escape hatch.
- The function returns a CompactResult that always tells the caller what
  happened: which layers fired, the tier, and a footer string if any.

Phase 1 ships L1 hygiene + L2 whitespace only. L3 RLE / L4 budget+handle
/ L5 dedup / normalizer registry land in subsequent phases. The public
surface here is intentionally stable so later phases extend layers.py
without rippling through the `tx-pane` script.
"""

from __future__ import annotations

import os

from .api import CompactCtx, CompactResult, NormalizeCtx, NormalizeResult
from .tier import Tier
from .layers import (
    apply_l1_hygiene,
    apply_l2_whitespace,
    apply_l3_rle,
    BUILTIN_BANNERS,
)
from .budget import apply_l4_budget, L4Decision
from .telemetry import record as telemetry_record
from . import handle as handle_store
from . import registry as normalizer_registry
from . import dedup

__all__ = [
    "compact",
    "CompactCtx",
    "CompactResult",
    "NormalizeCtx",
    "NormalizeResult",
    "Tier",
    "L4Decision",
    "BUILTIN_BANNERS",
    "is_compaction_disabled",
    "telemetry_record",
    "handle_store",
    "normalizer_registry",
    "dedup",
]


# Sentinel that callers will replace with the persisted handle id.
HANDLE_PLACEHOLDER = "__TX_HANDLE_PENDING__"


def is_compaction_disabled() -> bool:
    """Check the env-var escape hatch.

    Resolved at call time (not import time) so tests can monkeypatch the
    env without re-importing the module.
    """
    return os.environ.get("TX_PANE_NO_COMPACT") == "1"


def compact(text: str, ctx: CompactCtx) -> CompactResult:
    """Run the compaction pipeline on `text`.

    With ctx.mode == "raw" or the TX_PANE_NO_COMPACT env var set, this is the
    identity function (text unchanged, tier=FULL, no layers reported).
    With ctx.mode == "terse" or "summary", L1 + L2 fire (P1 scope).
    """
    in_bytes = len(text.encode("utf-8"))

    if is_compaction_disabled() or ctx.mode == "raw":
        return CompactResult(
            text=text,
            tier=Tier.FULL,
            applied_layers=[],
            notes=[],
            handle=None,
            footer=None,
            in_bytes=in_bytes,
            out_bytes=in_bytes,
        )

    applied: list[str] = []
    notes: list[str] = []
    tier_override: Tier | None = None
    normalizer_name: str | None = None

    # Normalizer dispatch (P4+). Runs before generic layers so that a
    # tool-specific filter can short-circuit hygiene + whitespace. When
    # a normalizer fires it may emit DEGRADED or PASSTHROUGH (per the
    # 3-tier contract); we propagate the tier to the final result.
    reg = normalizer_registry.load_registry()
    norm = normalizer_registry.find_normalizer(reg, ctx.cmd)
    if norm is not None and not normalizer_registry.is_normalizer_disabled(
        norm, ctx.disabled_normalizers
    ):
        nctx = NormalizeCtx(cmd=ctx.cmd, pane=ctx.pane, run_id=ctx.run_id)
        nresult = normalizer_registry.invoke(norm, text, nctx)
        text = nresult.text
        tier_override = nresult.tier
        normalizer_name = normalizer_registry.normalizer_name(norm)
        applied.append(normalizer_name)
        if nresult.warnings:
            notes.extend(nresult.warnings)

    if ctx.strip_banners:
        text, fired = apply_l1_hygiene(text, ctx)
        if fired:
            applied.append("L1")

    text = apply_l2_whitespace(text, ctx)
    applied.append("L2")

    if ctx.collapse_repeats:
        text, collapsed = apply_l3_rle(text, ctx)
        if collapsed > 0:
            applied.append("L3")
            notes.append(f"L3 collapsed {collapsed} repeated lines")

    # L4 — budget. Emits a HANDLE_PLACEHOLDER which the caller swaps for
    # the real handle id after persisting to offsets.json. The L4Decision
    # is stashed on the result so the caller can decide whether to
    # allocate a handle at all (only when ``decision.elided`` is True).
    l4 = apply_l4_budget(text, ctx, handle_placeholder=HANDLE_PLACEHOLDER)
    text = l4.text
    if l4.elided:
        applied.append("L4")
        notes.append(
            f"L4 truncated {l4.elided_end_index - l4.elided_start_index} "
            f"lines (rows {l4.elided_start_index}-{l4.elided_end_index - 1})"
        )

    out_bytes = len(text.encode("utf-8"))
    tier = tier_override if tier_override is not None else Tier.FULL

    footer = _build_footer(ctx, tier, applied, in_bytes, out_bytes, handle=None)

    result = CompactResult(
        text=text,
        tier=tier,
        applied_layers=applied,
        notes=notes,
        handle=None,
        footer=footer,
        in_bytes=in_bytes,
        out_bytes=out_bytes,
    )
    # Stash the L4 decision + normalizer name for callers (handle store
    # uses it; telemetry records the normalizer).
    result.l4 = l4  # type: ignore[attr-defined]
    result.normalizer = normalizer_name  # type: ignore[attr-defined]
    return result


def _build_footer(
    ctx: CompactCtx,
    tier: Tier,
    layers: list[str],
    in_bytes: int,
    out_bytes: int,
    handle: str | None,
) -> str | None:
    """Build the single-line `[tx-pane:compact ...]` footer string.

    Returns None when nothing useful would be reported:
    - Tier.FULL with no layers + no handle (non-verbose).
    - Tier.FULL when the footer itself would be longer than what was
      saved — emitting it would net-grow the output, defeating the
      whole point of compaction (this is what makes the "tiny output"
      regression test rows pass).
    """
    if tier == Tier.FULL and not layers and handle is None and not ctx.verbose:
        return None
    saved = max(0, in_bytes - out_bytes)
    saved_pct = (100.0 * saved / in_bytes) if in_bytes else 0.0
    parts = [f"tier={tier.name.lower()}"]
    if layers:
        parts.append(f"layers={','.join(layers)}")
    if handle:
        parts.append(f"handle={handle}")
    parts.append(f"in={in_bytes}B")
    parts.append(f"out={out_bytes}B")
    if saved > 0:
        parts.append(f"saved={saved_pct:.0f}%")
    footer = f"[tx-pane:compact {' '.join(parts)}]"
    # If the footer would make the response net-larger than the raw
    # input, suppress it. Tier 2/3 footers are kept regardless because
    # they convey diagnostic info the agent needs.
    if tier == Tier.FULL and not ctx.verbose and len(footer) + 1 > saved:
        return None
    return footer
