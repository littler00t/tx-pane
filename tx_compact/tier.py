"""Three-tier degradation contract.

Ported from rtk's src/parser/mod.rs::ParseResult. Every compaction
operation produces one of:

    Tier 1 (FULL)        — normalizer ran cleanly, output is structured
    Tier 2 (DEGRADED)    — ran with warnings, fields missing or fallback
    Tier 3 (PASSTHROUGH) — no normalizer matched, only generic layers

The hard rule (rtk's lesson, codified): a normalizer that raises or
returns malformed output must demote to PASSTHROUGH with the raw input,
not silently produce wrong content. Wrap normalizers with
`@degrade_on_exception` so any exception flips the result to PASSTHROUGH.
"""

from __future__ import annotations

from enum import IntEnum
from functools import wraps
from typing import Callable


class Tier(IntEnum):
    FULL = 1
    DEGRADED = 2
    PASSTHROUGH = 3

    def badge(self, verbose: bool = False) -> str | None:
        """Return the agent-visible marker string for this tier.

        Tier 1 in non-verbose mode is silent (the value of normalisation
        is that output looks "natural"). Tiers 2/3 are always visible.
        """
        if self == Tier.FULL:
            return "[tx-pane:full]" if verbose else None
        if self == Tier.DEGRADED:
            return "[tx-pane:degraded]"
        return "[tx-pane:passthrough]"


def degrade_on_exception(reason_prefix: str = "normalizer raised"):
    """Decorator: wrap a normalizer so exceptions demote to PASSTHROUGH.

    Used by both the TOML engine and Python plugin engine (P4). For a
    normalizer with signature ``fn(text, ctx) -> tuple[str, Tier, list[str]]``,
    any exception becomes ``(text, Tier.PASSTHROUGH, [f"{reason_prefix}: ..."])``.
    """
    def decorator(fn: Callable):
        @wraps(fn)
        def wrapped(text, ctx, *args, **kwargs):
            try:
                return fn(text, ctx, *args, **kwargs)
            except Exception as e:
                return (text, Tier.PASSTHROUGH, [f"{reason_prefix}: {type(e).__name__}: {e}"])
        return wrapped
    return decorator
