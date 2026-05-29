"""Public types for tx_compact callers and plugin authors.

CompactCtx is the per-call configuration (mode, budget, must-keep regexes,
flag overrides). CompactResult is what `compact()` returns: the compacted
text plus everything a caller needs to render an honest response (tier
badge, footer line, handle id, applied-layer list, byte counts for the
telemetry record).

These shapes are *stable* — later phases add fields with defaults but
never rename or remove existing ones, so the `tx-pane` script and out-of-tree
plugins can rely on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Pattern

from .tier import Tier


@dataclass
class CompactCtx:
    """Per-call compaction configuration.

    Fields:
        mode: "raw" | "terse" | "summary". "raw" is identity (escape hatch).
        cmd: the shell command the run executed (for normalizer matching).
        pane: pane id (telemetry / handle attribution).
        run_id: r-XXXX id if this is a finalized run, else None for buffer
                emissions (tail/dump/wait).
        token_budget: target output token count (L4, P3+). None = unlimited.
        strip_banners: gate for L1 banner registry.
        collapse_repeats: gate for L3 RLE (P2+).
        repeat_threshold: minimum run length for RLE collapse.
        must_keep: regex list — lines matching any pattern are preserved
                   verbatim regardless of L1/L2/L3 actions. Carries the
                   `tx-pane wait` match regex per design Q6.
        disabled_normalizers: names from the registry to skip (P4+).
        cleaned_cmd_echo: the literal echo of the command line printed by
                          the shell *before* the wrapped command's output.
                          Stripped by L1's command-echo elision when known.
        prompt_patterns: regex list, boundary-only prompt fragments to drop.
        verbose: include Tier-1 badge in the footer (TX_PANE_DEBUG / --verbose).
    """

    mode: str = "raw"
    cmd: str = ""
    pane: str | None = None
    run_id: str | None = None
    token_budget: int | None = None
    strip_banners: bool = True
    collapse_repeats: bool = True
    repeat_threshold: int = 3
    must_keep: list[Pattern] = field(default_factory=list)
    disabled_normalizers: list[str] = field(default_factory=list)
    cleaned_cmd_echo: str | None = None
    prompt_patterns: list[Pattern] = field(default_factory=list)
    verbose: bool = False


@dataclass
class CompactResult:
    """What `compact()` returns. Stable shape; later phases append fields.

    Always set:
        text: the (possibly compacted) string to emit.
        tier: Tier.FULL / DEGRADED / PASSTHROUGH.
        applied_layers: list of "L1", "L2", "L3", "L4", "L5", or normalizer
                        names — what fired. Empty list ≡ identity.
        in_bytes, out_bytes: pre/post UTF-8 byte counts (for telemetry).

    May be None:
        handle: P3+. Opaque h-XXXX id when L4 elided content.
        footer: P3+. The "[tx-pane:compact tier=... layers=... handle=...]" line
                callers append after the body. None ≡ silent (no footer).
        notes: diagnostic strings shown only in verbose mode.
    """

    text: str
    tier: Tier
    applied_layers: list[str]
    notes: list[str]
    handle: str | None
    footer: str | None
    in_bytes: int
    out_bytes: int

    @property
    def saved_bytes(self) -> int:
        return max(0, self.in_bytes - self.out_bytes)

    @property
    def saved_pct(self) -> float:
        if self.in_bytes == 0:
            return 0.0
        return 100.0 * self.saved_bytes / self.in_bytes


# ---------------------------------------------------------------------
# Normalizer surface — used by registry, toml_engine, plugin_engine.
# ---------------------------------------------------------------------

@dataclass
class NormalizeResult:
    """What a normalizer returns. Mirrors rtk's ParseResult.

    Construct via the classmethods rather than directly so the tier is
    always coherent with the text:
        NormalizeResult.full(text)
        NormalizeResult.degraded(text, warnings=[...])
        NormalizeResult.passthrough(text, reason="...")
    """
    text: str
    tier: Tier
    warnings: list[str]

    @classmethod
    def full(cls, text: str) -> "NormalizeResult":
        return cls(text=text, tier=Tier.FULL, warnings=[])

    @classmethod
    def degraded(cls, text: str, warnings: list[str] | None = None) -> "NormalizeResult":
        return cls(text=text, tier=Tier.DEGRADED, warnings=list(warnings or []))

    @classmethod
    def passthrough(cls, text: str, reason: str | None = None) -> "NormalizeResult":
        return cls(text=text, tier=Tier.PASSTHROUGH,
                   warnings=[reason] if reason else [])


@dataclass
class NormalizeCtx:
    """Per-call context passed to plugin normalizers.

    Slimmer than CompactCtx because a normalizer doesn't need the budget
    or must_keep machinery — those operate at outer layers.
    """
    cmd: str = ""
    pane: str | None = None
    run_id: str | None = None
