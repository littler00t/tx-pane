"""Generic compaction layers — Phase 1 ships L1 hygiene + L2 whitespace.

L3 RLE arrives in P2, L4 budget/handle in P3, L5 cross-call dedup in P5.

Every layer is a pure function: ``(text: str, ctx: CompactCtx) -> str``
(or ``-> tuple[str, ...]`` when it needs to report what fired). No file
I/O, no global mutation. This is the property that makes the layers
trivially testable from `tests/test_compact_layers.py`.

The contract: a layer never removes content that has information value.
"L1 hygiene strips a banner" means the registry has been carefully
audited — every regex must have a positive AND negative test case.
"""

from __future__ import annotations

import re
from typing import Pattern

from .api import CompactCtx


# ---------------------------------------------------------------------
# L1 — Built-in banner registry
# ---------------------------------------------------------------------
#
# Shipped patterns. Each entry is a (name, compiled regex) pair so the
# telemetry footer can report which banner fired. Patterns are anchored
# at line boundaries; they match a *whole* line (after rstrip) only.
#
# To add a new banner: append below AND add positive+negative tests in
# tests/test_compact_layers.py::TestL1Banners. The user-extensible
# `[compact.banners.exclude]` config (P4) lets users disable individual
# entries without recompiling.

BUILTIN_BANNERS: list[tuple[str, Pattern[str]]] = [
    # smartctl / smartmontools
    ("smartctl-version",   re.compile(r"^smartctl \d+\.\d+ \d{4}-\d{2}-\d{2}.*$")),
    ("smartmontools-copy", re.compile(r"^Copyright \(C\) .*smartmontools.*$")),
    ("smartctl-section",   re.compile(r"^=== START OF [A-Z ]+ SECTION ===$")),

    # apt / dpkg
    ("apt-reading-pkgs",   re.compile(r"^Reading package lists\.\.\.( Done)?$")),
    ("apt-building-tree",  re.compile(r"^Building dependency tree\.\.\.( Done)?$")),
    ("apt-reading-state",  re.compile(r"^Reading state information\.\.\.( Done)?$")),
    ("apt-warn-cli",       re.compile(r"^WARNING: apt does not have a stable CLI interface\..*$")),
    ("apt-listing",        re.compile(r"^Listing\.\.\.( Done)?$")),

    # journalctl / last
    ("journal-no-entries", re.compile(r"^-- No entries --$")),
    ("wtmp-begins",        re.compile(r"^wtmp begins .*$")),
    ("btmp-begins",        re.compile(r"^btmp begins .*$")),
    ("lastlog-never",      re.compile(r"^\S+\s+\*\*Never logged in\*\*\s*$")),

    # systemctl (legend/footer)
    ("systemctl-legend-load",   re.compile(r"^\s*LOAD\s+=\s+Reflects whether the unit definition.*$")),
    ("systemctl-legend-active", re.compile(r"^\s*ACTIVE\s+=\s+The high-level unit activation.*$")),
    ("systemctl-legend-sub",    re.compile(r"^\s*SUB\s+=\s+The low-level unit activation.*$")),
    ("systemctl-listed",        re.compile(r"^\d+\s+loaded units listed\..*$")),
]


# Compiled set of just the regexes — used in hot path.
_BUILTIN_BANNER_RES: list[Pattern[str]] = [r for _, r in BUILTIN_BANNERS]


# Exit-code marker lines emitted by the shell fallback when the v2 hook
# isn't installed. Stripped from agent-facing text (the structured exit
# code lives in the run record). Match whole-line only.
_EXIT_CODE_LINE = re.compile(r"^\[exit:-?\d+\]$")


def _matches_any(line: str, patterns: list[Pattern[str]]) -> bool:
    for p in patterns:
        if p.match(line):
            return True
    return False


def _matches_must_keep(line: str, must_keep: list[Pattern[str]]) -> bool:
    for p in must_keep:
        if p.search(line):
            return True
    return False


# ---------------------------------------------------------------------
# L1 — Hygiene
# ---------------------------------------------------------------------

def apply_l1_hygiene(text: str, ctx: CompactCtx) -> tuple[str, list[str]]:
    """Strip banners, boundary prompt fragments, command echo, exit-code lines.

    Returns (cleaned_text, fired_names) where fired_names is a list of
    banner names that matched at least once (for telemetry). Order of
    fired_names matches BUILTIN_BANNERS.

    Boundary-only behavior for prompt patterns: a prompt-like line is
    dropped only if it appears at the very start or very end of the
    text (after blank-line trimming). An interior `>>>` line inside a
    Python REPL transcript is real content — never dropped.

    Command-echo elision: if ctx.cleaned_cmd_echo is set and the first
    non-blank line equals that string, drop that one line. The marker
    protocol already gives us the byte range starting at the echo, so
    this is a single deterministic line drop.

    Must-keep wins: any line matching a ctx.must_keep regex is preserved
    verbatim regardless of every rule in this function.
    """
    lines = text.split("\n")
    fired: list[str] = []

    # Track which banner names matched (for telemetry).
    seen_names: set[str] = set()

    def banner_match_name(line: str) -> str | None:
        for name, pat in BUILTIN_BANNERS:
            if pat.match(line):
                return name
        return None

    # Pass 1: drop unambiguous banners and exit-code lines, anywhere in
    # the body. These are zero-information by definition.
    out: list[str] = []
    for line in lines:
        stripped = line.rstrip()
        if _matches_must_keep(stripped, ctx.must_keep):
            out.append(line)
            continue
        if _EXIT_CODE_LINE.match(stripped):
            if "exit-code-line" not in seen_names:
                seen_names.add("exit-code-line")
                fired.append("exit-code-line")
            continue
        name = banner_match_name(stripped)
        if name is not None:
            if name not in seen_names:
                seen_names.add(name)
                fired.append(name)
            continue
        out.append(line)

    # Pass 2: command-echo elision — first non-blank line, exact match.
    if ctx.cleaned_cmd_echo:
        target = ctx.cleaned_cmd_echo.strip()
        for i, line in enumerate(out):
            if line.strip() == "":
                continue
            if line.strip() == target and not _matches_must_keep(line, ctx.must_keep):
                out.pop(i)
                if "cmd-echo" not in seen_names:
                    seen_names.add("cmd-echo")
                    fired.append("cmd-echo")
            break

    # Pass 3: boundary prompt elision — first/last non-blank line.
    if ctx.prompt_patterns:
        def is_prompt(line: str) -> bool:
            s = line.rstrip()
            if _matches_must_keep(s, ctx.must_keep):
                return False
            for p in ctx.prompt_patterns:
                if p.search(s):
                    return True
            return False

        # Leading
        while out:
            i = 0
            while i < len(out) and out[i].strip() == "":
                i += 1
            if i < len(out) and is_prompt(out[i]):
                del out[i]
                if "boundary-prompt" not in seen_names:
                    seen_names.add("boundary-prompt")
                    fired.append("boundary-prompt")
            else:
                break
        # Trailing
        while out:
            j = len(out) - 1
            while j >= 0 and out[j].strip() == "":
                j -= 1
            if j >= 0 and is_prompt(out[j]):
                del out[j]
                if "boundary-prompt" not in seen_names:
                    seen_names.add("boundary-prompt")
                    fired.append("boundary-prompt")
            else:
                break

    return "\n".join(out), fired


# ---------------------------------------------------------------------
# L2 — Whitespace normalisation
# ---------------------------------------------------------------------
#
# Allowlist of commands whose output preserves indentation/blank-line
# semantics. When the command head matches, L2 is skipped entirely. This
# is the "Python REPL / YAML / diff" exclusion from the design plan §4.2.

_L2_PRESERVE_CMD_HEADS = (
    "python", "python3", "py", "ipython",
    "yq", "diff", "git diff", "cat",
)


def _command_preserves_whitespace(cmd: str) -> bool:
    """True iff this command's output should bypass L2 normalisation.

    Match on the *first word* of the command (the binary name). For
    `cat`, additionally check the file extension hint — `cat foo.py` /
    `cat conf.yml` should bypass; `cat /etc/hosts` should normalize.
    """
    s = cmd.strip()
    if not s:
        return False
    head = s.split()[0] if s.split() else s
    if head in ("python", "python3", "py", "ipython", "yq", "diff"):
        return True
    if head == "cat":
        for ext in (".py", ".yml", ".yaml", ".md", ".diff", ".patch", ".rst"):
            if ext in s:
                return True
    if s.startswith("git diff"):
        return True
    return False


def apply_l2_whitespace(text: str, ctx: CompactCtx) -> str:
    """Tighten blank-run cap to 1 and strip per-line trailing whitespace.

    The existing `_strip_lines` in tx caps blank runs at 2 already; L2
    tightens to 1 (single blank between content blocks). Skipped entirely
    if ctx.cmd matches the preserve-whitespace allowlist (Python REPL,
    YAML, diff, `cat foo.py`, etc.) — for those, indentation and blank
    spacing carry semantic content.

    Must-keep lines (ctx.must_keep) are emitted verbatim.
    """
    if _command_preserves_whitespace(ctx.cmd):
        return text

    lines = text.split("\n")
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if _matches_must_keep(line, ctx.must_keep):
            out.append(line)
            blank_run = 0
            continue
        s = line.rstrip()
        if s == "":
            blank_run += 1
            if blank_run <= 1:
                out.append(s)
        else:
            blank_run = 0
            out.append(s)
    # Strip leading and trailing blanks.
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


# ---------------------------------------------------------------------
# L3 — Local repeated-line collapse (RLE)
# ---------------------------------------------------------------------
#
# Two collapse modes operate over the same single-pass line iterator:
#
#   - Identical-line runs of ≥ ctx.repeat_threshold (default 3):
#       collapse to one representative line + a summary marker
#       `[× N identical lines elided]`.
#
#   - Near-identical runs (same first-40-char prefix AND same
#       length-class bucket, default ≥ 3 in a row): emit 1 sample +
#       `[× N similar lines elided]`. Cheaper than Levenshtein, deals
#       with timestamp-prefixed log lines.
#
# Must-keep wins: any matching line is emitted verbatim and breaks the
# current run. This is non-negotiable — `tx wait` and `must_keep_patterns`
# (error/commit-sha/etc.) seed regexes that *must* not be summarised away.


_NEAR_PREFIX_LEN = 40


# Leading-timestamp patterns to *erase* before fingerprinting. This is
# what lets dmesg / journal / docker-log style lines collapse despite
# their per-line wall-clock timestamps. Order: longest first.
_LEADING_TS = re.compile(
    r"^("
    # bracketed ISO: [2026-05-14 03:14:01]
    r"\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]"
    # ISO without brackets
    r"|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    # bracketed kernel-style float seconds: [   12.345678]
    r"|\[\s*\d+\.\d+\]"
    # bracketed HH:MM:SS
    r"|\[\d{2}:\d{2}:\d{2}\]"
    # bracketed run-id: [r-abc123]
    r"|\[r-[0-9a-f]+\]"
    r")\s*"
)


def _length_bucket(n: int) -> int:
    """Coarse length classification for near-identical matching.

    Same bucket ≡ "approximately the same length". Cheaper than edit-
    distance and good enough for log-line collapsing.
    """
    if n < 32:
        return n // 8
    if n < 128:
        return 4 + (n // 32)
    if n < 512:
        return 8 + (n // 128)
    return 12 + (n // 512)


# Number-like runs are collapsed to '#' before fingerprinting so that
# "processed item 1234" and "processed item 1235" map to the same key.
# Critical for collapsing per-row log lines that only differ by id.
_DIGIT_RUN = re.compile(r"\d+")


def _fingerprint(line: str) -> tuple[str, int]:
    """Cheap near-identical fingerprint: (prefix, length_bucket).

    A leading timestamp (ISO, bracketed kernel float seconds, etc.) is
    stripped before taking the prefix so that ``dmesg`` / ``journalctl``
    style "same payload, different timestamp" lines compare equal.

    Runs of digits inside the line are replaced with a single ``#``
    sentinel — this is what makes "INFO processed item 1234" and
    "INFO processed item 1235" share a fingerprint without resorting
    to Levenshtein-distance.
    """
    s = line.rstrip()
    s = _LEADING_TS.sub("", s, count=1)
    canonical = _DIGIT_RUN.sub("#", s)
    return canonical[:_NEAR_PREFIX_LEN], _length_bucket(len(canonical))


def apply_l3_rle(text: str, ctx: CompactCtx) -> tuple[str, int]:
    """Collapse runs of identical or near-identical lines.

    Returns (text, collapsed_count). collapsed_count is the total number
    of lines elided (the sum of "N-1" across each collapsed run, so the
    L1+L2 idempotency property holds: re-running L3 on its own output
    leaves it unchanged).

    Disabled if ctx.collapse_repeats is False or ctx.repeat_threshold
    is <= 1.
    """
    if not ctx.collapse_repeats or ctx.repeat_threshold <= 1:
        return text, 0
    if _command_preserves_whitespace(ctx.cmd):
        # Same rationale as L2 — these commands' output has structural
        # meaning per line (REPL transcripts, diff hunks, YAML).
        return text, 0

    threshold = max(2, int(ctx.repeat_threshold))
    lines = text.split("\n")
    out: list[str] = []
    collapsed_total = 0

    i = 0
    n = len(lines)
    while i < n:
        cur = lines[i]
        if _matches_must_keep(cur, ctx.must_keep):
            out.append(cur)
            i += 1
            continue

        # Phase 1: try identical run.
        j = i + 1
        while j < n and lines[j] == cur and not _matches_must_keep(lines[j], ctx.must_keep):
            j += 1
        run_len = j - i

        # Phase 2: if identical run is below threshold, try the
        # near-identical fingerprint match starting at i.
        if run_len < threshold:
            fp = _fingerprint(cur)
            k = i + 1
            while k < n and not _matches_must_keep(lines[k], ctx.must_keep) \
                    and _fingerprint(lines[k]) == fp:
                k += 1
            near_len = k - i
            if near_len >= threshold:
                elided = near_len - 1
                marker = f"[× {elided} similar lines elided]"
                # Only collapse if it actually saves bytes.
                elided_bytes = sum(len(lines[m]) + 1 for m in range(i + 1, k))
                if len(marker) + 1 < elided_bytes:
                    out.append(cur)
                    collapsed_total += elided
                    out.append(marker)
                    i = k
                    continue

        if run_len >= threshold:
            elided = run_len - 1
            marker = f"[× {elided} identical lines elided]"
            # Only collapse if it actually saves bytes (marker overhead
            # can exceed the elided content for very short repeated lines).
            elided_bytes = elided * (len(cur) + 1)
            if len(marker) + 1 < elided_bytes:
                out.append(cur)
                collapsed_total += elided
                out.append(marker)
                i = j
                continue

        out.append(cur)
        i += 1

    return "\n".join(out), collapsed_total
