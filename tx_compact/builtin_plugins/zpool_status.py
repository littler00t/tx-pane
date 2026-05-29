"""zpool status normalizer — happy-path collapse to one line.

A healthy pool prints ~20 lines:
    pool: tank
   state: ONLINE
    scan: scrub repaired 0B in ... on Sat ...
  config:
        NAME ...
    errors: No known data errors

Anomaly path triggers FULL output: any of DEGRADED / FAULTED / UNAVAIL
in the body, errors with non-zero count, or `cannot import`.
"""

from __future__ import annotations

import re

from tx_compact.api import NormalizeCtx, NormalizeResult


SCHEMA_VERSION = 1
NAME = "zpool-status"
MATCH_COMMAND = r"^zpool\s+status(\s|$)"


_HEALTHY_STATE = re.compile(r"^\s*state:\s+ONLINE\s*$", re.MULTILINE)
_HEALTHY_ERRORS = re.compile(r"^\s*errors:\s+No known data errors\s*$", re.MULTILINE)
_SCAN_LINE = re.compile(r"^\s*scan:\s+(.+)$", re.MULTILINE)
_POOL_NAME = re.compile(r"^\s*pool:\s+(\S+)\s*$", re.MULTILINE)
_ANOMALY = re.compile(
    r"\b(DEGRADED|FAULTED|UNAVAIL|REMOVED|OFFLINE)\b|errors:\s+(?!No\b)|cannot import"
)


def normalize(text: str, ctx: NormalizeCtx) -> NormalizeResult:
    # Detect anomaly first — anything in the body that suggests trouble
    # blocks the happy-path collapse and falls through to full output
    # (Tier 2 with a warning).
    if _ANOMALY.search(text):
        return NormalizeResult.degraded(
            text, warnings=["zpool: anomaly detected — emitting full output"]
        )

    state_m = _HEALTHY_STATE.search(text)
    err_m = _HEALTHY_ERRORS.search(text)
    if not (state_m and err_m):
        # Not the canonical healthy shape — fall back to passthrough.
        return NormalizeResult.passthrough(text, reason="zpool: unrecognized shape")

    pool_m = _POOL_NAME.search(text)
    scan_m = _SCAN_LINE.search(text)
    pool = pool_m.group(1) if pool_m else "<pool>"
    scan_summary = ""
    if scan_m:
        scan_text = scan_m.group(1)
        if "scrub repaired 0B" in scan_text:
            scan_summary = ", last scrub clean"
        elif "scrub in progress" in scan_text or "resilver in progress" in scan_text:
            scan_summary = ", scrub/resilver in progress"
    return NormalizeResult.full(
        f"zpool {pool}: ONLINE, no known data errors{scan_summary}"
    )
