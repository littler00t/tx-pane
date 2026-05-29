"""lsblk normalizer — emit per-device summary with critical fields.

The default `lsblk` output is a multi-line tree with NAME / MAJ:MIN /
RM / SIZE / RO / TYPE / MOUNTPOINTS. We keep one line per device
without the tree-drawing characters and only include MOUNTPOINTS when
non-empty.

If the user passed `-J/--json`, we parse the JSON and emit `name size
[mountpoint fstype]` per blockdevice (walking children recursively).
"""

from __future__ import annotations

import json
import re

from tx_compact.api import NormalizeCtx, NormalizeResult


SCHEMA_VERSION = 1
NAME = "lsblk"
MATCH_COMMAND = r"^lsblk\b"


def normalize(text: str, ctx: NormalizeCtx) -> NormalizeResult:
    if "-J" in ctx.cmd.split() or "--json" in ctx.cmd:
        try:
            data = json.loads(text)
            return _normalize_json(data)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            return NormalizeResult.passthrough(
                text, reason=f"lsblk: failed to parse JSON: {e}"
            )
    return _normalize_tree(text)


def _normalize_json(data: dict) -> NormalizeResult:
    """Walk a `lsblk -J` JSON document, emit a flat per-device summary."""
    out: list[str] = []

    def walk(node: dict, depth: int) -> None:
        name = node.get("name", "?")
        size = node.get("size", "")
        fstype = node.get("fstype", "") or ""
        mp = node.get("mountpoint", "") or ""
        # Prefer mountpoints (plural, newer lsblk) if present.
        if "mountpoints" in node:
            mps = [m for m in (node.get("mountpoints") or []) if m]
            if mps:
                mp = ",".join(mps)
        prefix = "  " * depth
        parts = [f"{prefix}{name}", size]
        if fstype:
            parts.append(f"[{fstype}]")
        if mp:
            parts.append(f"@{mp}")
        out.append(" ".join(p for p in parts if p))
        for child in node.get("children") or []:
            walk(child, depth + 1)

    for d in data.get("blockdevices") or []:
        walk(d, 0)
    return NormalizeResult.full("\n".join(out) if out else "(no block devices)")


_TREE_LINE = re.compile(
    r"^(?P<tree>[│├└─\s]*)(?P<name>\S+)\s+(?P<rest>.*?)$"
)


def _normalize_tree(text: str) -> NormalizeResult:
    """Best-effort cleanup of the default `lsblk` table output."""
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        # Drop tree-drawing characters: │ ├ └ ─
        clean = re.sub(r"[│├└─]+", " ", line).strip()
        clean = re.sub(r"\s+", " ", clean)
        out.append(clean)
    return NormalizeResult.full("\n".join(out))
