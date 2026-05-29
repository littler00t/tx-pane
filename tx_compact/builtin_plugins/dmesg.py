"""dmesg normalizer — preserve error/warning lines verbatim, summarise rest.

dmesg output is highly repetitive (USB enumeration, scsi bus scans).
Strategy: pass through L3 RLE-like grouping but with explicit
must-keep for any error/warning/oom/segfault/panic mention. The
core compact() already runs L3 generically — this plugin adds the
must-keep seed and tail-biases for very long outputs.
"""

from __future__ import annotations

import re

from tx_compact.api import NormalizeCtx, NormalizeResult


SCHEMA_VERSION = 1
NAME = "dmesg"
MATCH_COMMAND = r"^dmesg\b"


_CRITICAL = re.compile(
    r"(?i)\b(error|warning|warn|hard reset|oom|out of memory|segfault|panic|"
    r"fail(ed|ure)?|critical|hung_task|bug|stack|kernel: BUG)\b"
)


def normalize(text: str, ctx: NormalizeCtx) -> NormalizeResult:
    lines = text.split("\n")
    critical_count = sum(1 for l in lines if _CRITICAL.search(l))
    # If there are no critical events, the agent rarely needs the full
    # bootlog. Keep first 5 + last 30 + a summary marker.
    if critical_count == 0 and len(lines) > 50:
        head = lines[:5]
        tail = lines[-30:]
        marker = f"[× {len(lines) - 35} routine dmesg lines elided — no errors/warnings]"
        return NormalizeResult.full("\n".join(head + [marker] + tail))
    # Otherwise: passthrough so L3 RLE handles repetition while keeping
    # the critical lines (must_keep machinery in the core compact() layer).
    if critical_count > 0:
        return NormalizeResult.degraded(
            text, warnings=[f"dmesg: {critical_count} critical lines detected"]
        )
    return NormalizeResult.full(text)
