"""docker ps / docker images normalizer.

Parses the default column-aligned table and emits a TSV:
    <id-12> <image> <status> <names> <ports>
Drops COMMAND (usually `"sh -c ..."` or truncated junk) and CREATED.

If the daemon is unreachable (`Cannot connect to the Docker daemon`),
emit Tier 2 — the error is what the agent needs, no need to alter it.
"""

from __future__ import annotations

import re

from tx_compact.api import NormalizeCtx, NormalizeResult


SCHEMA_VERSION = 1
NAME = "docker-ps"
MATCH_COMMAND = r"^docker\s+(ps|images|container\s+ls)\b"


def normalize(text: str, ctx: NormalizeCtx) -> NormalizeResult:
    # Daemon error short-circuit.
    if "Cannot connect to the Docker daemon" in text or \
       "permission denied while trying to connect" in text:
        return NormalizeResult.degraded(
            text.strip(),
            warnings=["docker: daemon unreachable"],
        )

    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return NormalizeResult.full("(no containers)")

    # First line is the header. Pre-compute column boundaries from it.
    header = lines[0]
    # Header columns are whitespace-separated; widths vary. Easiest
    # approach: split on 2+ spaces, mapped by header position.
    cols = [c.strip() for c in re.split(r"\s{2,}", header)]
    if not cols:
        return NormalizeResult.passthrough(text, reason="docker: empty header")

    # Build per-row tuples by splitting the rest of the lines the same way.
    out_lines: list[str] = []
    for row in lines[1:]:
        if not row.strip():
            continue
        cells = [c.strip() for c in re.split(r"\s{2,}", row)]
        # Drop unwanted columns. Keep CONTAINER ID, IMAGE, STATUS, NAMES, PORTS.
        keep_indices = []
        for i, name in enumerate(cols):
            up = name.upper()
            if up in ("CONTAINER ID", "IMAGE", "STATUS", "NAMES", "PORTS",
                       "REPOSITORY", "TAG", "SIZE"):
                keep_indices.append(i)
        if not keep_indices:
            keep_indices = list(range(len(cells)))
        out_lines.append("\t".join(cells[i] for i in keep_indices if i < len(cells)))
    if out_lines:
        return NormalizeResult.full(
            "\t".join(cols[i] for i in keep_indices) + "\n" + "\n".join(out_lines)
        )
    return NormalizeResult.full("(no containers)")
