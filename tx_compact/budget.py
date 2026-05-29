"""L4 — token-budget truncation with handle.

The load-bearing piece of the compaction stage: closes the failure
mode where every call costs full output tokens regardless of how big.

Algorithm (per design plan §4.4):
1. Estimate token count via tokens.estimate.
2. If post-L1/L2/L3 text fits the budget → emit verbatim, no handle needed.
3. If overflow: pick head N + tail M lines (50/50 split by default),
   emit them around an elision marker that names the handle.

The actual handle persistence lives in the `tx-pane` script (it needs the
offsets lock); ``apply_l4_budget`` here returns the metadata needed to
construct a handle (raw line count) and the elided line range.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .api import CompactCtx
from .tokens import estimate as estimate_tokens


@dataclass
class L4Decision:
    """What L4 decided. Returned alongside the new text.

    Fields:
        elided: True iff L4 truncated.
        text: the post-truncation text (head + marker + tail, or
              the original).
        raw_lines: total line count of the pre-truncation input
                   (= what a future --range lookup would index against).
        head_lines: how many leading lines were kept.
        tail_lines: how many trailing lines were kept.
        elided_start_index: index of the first elided line (0-based, in
                            raw-line coordinates). 0 when not elided.
        elided_end_index: index *past* the last elided line. Equals
                          elided_start_index when not elided.
        tokens_in: estimated tokens of the input.
        tokens_out: estimated tokens of the emitted text.
    """
    elided: bool
    text: str
    raw_lines: int
    head_lines: int
    tail_lines: int
    elided_start_index: int
    elided_end_index: int
    tokens_in: int
    tokens_out: int


def apply_l4_budget(
    text: str,
    ctx: CompactCtx,
    *,
    head_fraction: float = 0.5,
    handle_placeholder: str = "HANDLE-PLACEHOLDER",
) -> L4Decision:
    """Truncate `text` to fit ``ctx.token_budget`` using head+tail split.

    ``handle_placeholder`` is the literal string that will appear in
    the elision marker where the caller will later substitute the real
    handle id. The caller does this swap after persisting the handle
    so the order of operations is: L4 → caller allocates handle id →
    caller does ``text.replace(placeholder, real_id)``.

    Returns an L4Decision describing what happened. When
    ``ctx.token_budget`` is None or 0 or the text fits, the input is
    returned unchanged with ``elided=False``.
    """
    if not ctx.token_budget:
        return _identity(text)

    budget = int(ctx.token_budget)
    tokens_in = estimate_tokens(text)
    if tokens_in <= budget:
        return _identity(text, tokens_in=tokens_in)

    # Split the budget between head and tail. The elision marker itself
    # costs a small handful of tokens — we reserve 20 for it.
    marker_reserve = 20
    usable = max(40, budget - marker_reserve)
    head_tokens = int(usable * head_fraction)
    tail_tokens = usable - head_tokens

    lines = text.split("\n")
    raw_lines = len(lines)
    if raw_lines == 0:
        return _identity(text, tokens_in=tokens_in)

    # Greedy line-level allocation. Walk from the top until head_tokens
    # is consumed, then from the bottom until tail_tokens is consumed.
    head_idx = 0
    head_used = 0
    while head_idx < raw_lines:
        cost = estimate_tokens(lines[head_idx]) + 1  # +1 for newline overhead
        if head_used + cost > head_tokens and head_idx > 0:
            break
        head_used += cost
        head_idx += 1

    tail_idx = raw_lines
    tail_used = 0
    while tail_idx > head_idx:
        cost = estimate_tokens(lines[tail_idx - 1]) + 1
        if tail_used + cost > tail_tokens and tail_idx < raw_lines:
            break
        tail_used += cost
        tail_idx -= 1

    elided_start = head_idx
    elided_end = tail_idx

    if elided_end <= elided_start:
        # Budget large enough to fit the whole thing line-by-line.
        return _identity(text, tokens_in=tokens_in)

    elided_count = elided_end - elided_start
    elided_lines_text = "\n".join(lines[elided_start:elided_end])
    elided_tokens = estimate_tokens(elided_lines_text)

    pane = ctx.pane or "<pane>"
    run_or_buf = ctx.run_id or "<buffer>"
    marker = (
        f"[tx-pane:elided run={run_or_buf} "
        f"raw_lines={raw_lines} elided_lines={elided_count} "
        f"~{elided_tokens}tok handle={handle_placeholder}]\n"
        f"[retrieve: tx-pane output {pane} {run_or_buf} "
        f"--handle {handle_placeholder} "
        f"--range {elided_start}-{elided_end - 1}   "
        f"(or --grep PAT / --full)]"
    )

    parts: list[str] = []
    if head_idx > 0:
        parts.append("\n".join(lines[:head_idx]))
    parts.append(marker)
    if tail_idx < raw_lines:
        parts.append("\n".join(lines[tail_idx:]))
    out_text = "\n".join(parts)

    return L4Decision(
        elided=True,
        text=out_text,
        raw_lines=raw_lines,
        head_lines=head_idx,
        tail_lines=raw_lines - tail_idx,
        elided_start_index=elided_start,
        elided_end_index=elided_end,
        tokens_in=tokens_in,
        tokens_out=estimate_tokens(out_text),
    )


def _identity(text: str, *, tokens_in: int | None = None) -> L4Decision:
    raw_lines = text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
    if text == "":
        raw_lines = 0
    if tokens_in is None:
        tokens_in = estimate_tokens(text)
    return L4Decision(
        elided=False, text=text, raw_lines=raw_lines,
        head_lines=raw_lines, tail_lines=0,
        elided_start_index=0, elided_end_index=0,
        tokens_in=tokens_in, tokens_out=tokens_in,
    )
