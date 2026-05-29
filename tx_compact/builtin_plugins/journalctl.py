"""journalctl normalizer — preserve error lines + summary count.

journalctl output sizes wildly. Strategy: pass through but always
declare "saw N total lines, M critical" so the agent has a quick
signal. Heavy-lifting compaction (L3 RLE + must_keep error patterns)
happens in the core compact() layer.
"""

from __future__ import annotations

import re

from tx_compact.api import NormalizeCtx, NormalizeResult


SCHEMA_VERSION = 1
NAME = "journalctl"
MATCH_COMMAND = r"^journalctl\b"


_CRITICAL = re.compile(
    r"(?i)\b(err(or)?|fail(ed|ure)?|fatal|critical|panic|emerg)\b"
)


def normalize(text: str, ctx: NormalizeCtx) -> NormalizeResult:
    if text.strip() == "" or "-- No entries --" in text:
        return NormalizeResult.full("journalctl: (no entries)")
    if "No journal files were found" in text:
        return NormalizeResult.degraded(
            "journalctl: no journal files found",
            warnings=["systemd journal not available in this environment"],
        )
    lines = text.split("\n")
    critical = sum(1 for l in lines if _CRITICAL.search(l))
    if critical > 0:
        # Defer to outer layers; just record the summary.
        return NormalizeResult.degraded(
            text, warnings=[f"journalctl: {critical} critical lines among {len(lines)} total"]
        )
    return NormalizeResult.full(text)
