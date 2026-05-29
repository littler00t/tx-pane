"""TOML filter engine — 8-stage pipeline ported from rtk.

Filter file schema (loosely matches rtk's ``src/filters/*.toml`` with the
sections we need):

    schema_version = 1

    [filters.<name>]
    description       = "..."
    match_command     = "^<regex>"
    strip_ansi        = false    # ANSI is already stripped upstream
    replace           = [ { pattern = "...", with = "..." }, ... ]
    match_output      = [ { pattern = "...", message = "...", unless = "..." } ]
    strip_lines_matching = [ "^pat", ... ]
    keep_lines_matching  = [ "^pat", ... ]   # mutually exclusive with strip_*
    truncate_lines_at    = 120
    head_lines           = 10
    tail_lines           = 10
    max_lines            = 30
    on_empty             = "(no output)"
    min_savings_pct      = 30                # used by test_compact_savings

    [[tests.<name>]]
    name     = "..."
    input    = '''...'''            # triple-single-quoted strings work too
    expected = "..."                # exact match
    # or
    expected_contains = ["..."]

Pipeline order (rtk-faithful):
    1. (strip_ansi — already done upstream)
    2. replace
    3. match_output (short-circuit happy-path collapse)
    4. strip_lines_matching / keep_lines_matching
    5. truncate_lines_at
    6. head_lines / tail_lines  (with omit-message)
    7. max_lines (absolute cap, counts the omit-message)
    8. on_empty
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Pattern

from .api import NormalizeCtx, NormalizeResult
from .tier import Tier


SCHEMA_VERSION = 1


@dataclass
class ReplaceRule:
    pattern: Pattern[str]
    replacement: str


@dataclass
class MatchOutputRule:
    pattern: Pattern[str]
    message: str
    unless: Pattern[str] | None = None


@dataclass
class TomlFilter:
    """One compiled filter loaded from a .toml file."""
    name: str
    description: str
    match_command: Pattern[str]
    replace: list[ReplaceRule] = field(default_factory=list)
    match_output: list[MatchOutputRule] = field(default_factory=list)
    strip_lines_matching: list[Pattern[str]] = field(default_factory=list)
    keep_lines_matching: list[Pattern[str]] = field(default_factory=list)
    truncate_lines_at: int | None = None
    head_lines: int | None = None
    tail_lines: int | None = None
    max_lines: int | None = None
    on_empty: str | None = None
    min_savings_pct: float = 0.0
    omit_msg: str = "[omitted: %d lines]"
    inline_tests: list[dict] = field(default_factory=list)
    source_path: Path | None = None


def load_filter_file(path: Path) -> list[TomlFilter]:
    """Load one .toml file → list of compiled filters.

    A file is allowed to define multiple filters; each lives under
    ``[filters.<name>]``. The corresponding inline tests under
    ``[[tests.<name>]]`` are attached to the filter named ``<name>``.

    Raises ValueError on schema version mismatch.
    """
    with open(path, "rb") as f:
        doc = tomllib.load(f)
    version = doc.get("schema_version", 1)
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version {version} not supported (expected {SCHEMA_VERSION})"
        )

    filters_section = doc.get("filters", {}) or {}
    tests_section = doc.get("tests", {}) or {}

    out: list[TomlFilter] = []
    for name, body in filters_section.items():
        if not isinstance(body, dict):
            continue
        flt = _compile_one(name, body, path)
        # Attach inline tests if any.
        tests = tests_section.get(name) or []
        if isinstance(tests, list):
            flt.inline_tests = list(tests)
        out.append(flt)
    return out


def _compile_one(name: str, body: dict, source_path: Path) -> TomlFilter:
    match_cmd = body.get("match_command", "")
    if not match_cmd:
        raise ValueError(f"filter '{name}' missing match_command")
    try:
        cmd_re = re.compile(match_cmd)
    except re.error as e:
        raise ValueError(f"filter '{name}' match_command not a valid regex: {e}") from e

    replace_rules: list[ReplaceRule] = []
    for r in body.get("replace") or []:
        if not isinstance(r, dict):
            continue
        try:
            # MULTILINE so `^`/`$` anchors match line boundaries within
            # the whole-text substitution (this is what rtk does and is
            # almost always what authors expect).
            replace_rules.append(ReplaceRule(
                pattern=re.compile(r.get("pattern", ""), re.MULTILINE),
                replacement=str(r.get("with", "")),
            ))
        except re.error:
            continue

    match_output_rules: list[MatchOutputRule] = []
    for r in body.get("match_output") or []:
        if not isinstance(r, dict):
            continue
        try:
            unless_str = r.get("unless")
            match_output_rules.append(MatchOutputRule(
                pattern=re.compile(r.get("pattern", ""), re.MULTILINE),
                message=str(r.get("message", "")),
                unless=re.compile(unless_str) if unless_str else None,
            ))
        except re.error:
            continue

    def _compile_list(key: str) -> list[Pattern[str]]:
        out: list[Pattern[str]] = []
        for p in body.get(key) or []:
            try:
                out.append(re.compile(p))
            except re.error:
                continue
        return out

    return TomlFilter(
        name=name,
        description=str(body.get("description", "")),
        match_command=cmd_re,
        replace=replace_rules,
        match_output=match_output_rules,
        strip_lines_matching=_compile_list("strip_lines_matching"),
        keep_lines_matching=_compile_list("keep_lines_matching"),
        truncate_lines_at=body.get("truncate_lines_at"),
        head_lines=body.get("head_lines"),
        tail_lines=body.get("tail_lines"),
        max_lines=body.get("max_lines"),
        on_empty=body.get("on_empty"),
        min_savings_pct=float(body.get("min_savings_pct", 0)),
        omit_msg=str(body.get("omit_msg", "[omitted: %d lines]")),
        source_path=source_path,
    )


# ---------------------------------------------------------------------
# Pipeline execution
# ---------------------------------------------------------------------

def is_pipeline_command(cmd: str) -> bool:
    """True if `cmd` contains a shell pipeline, conjunction, or redirect
    that *transforms* the output the agent will see.

    Per design plan §9.4: when a user wrote ``zpool status | grep ONLINE``
    they have already chosen a representation; the normalizer must not
    fire.

    Stderr-only redirects (`2>&1`, `2>/dev/null`, `2>file`) do NOT count
    — they only merge / discard stderr without filtering stdout, so the
    agent is asking for the same content shape and the normalizer should
    still apply.
    """
    s = cmd.strip()
    if not s:
        return False
    # Strip stderr-only redirects: `2>&1`, `2>file`, `2>>file`, `2>/dev/null`.
    s = re.sub(r"\s*2>+(?:&\d+|\S+)\s*", " ", s)
    # What's left: pipelines (`|`, but not `||` first), conjunctions
    # (`&&`, `||`, `;`), stdout redirects (`>`, `>>`), input redirects
    # (`<`, `<<`), and trailing backgrounding (`&` at end).
    if re.search(r"(?<!\|)\|(?!\|)", s):  # pipe (not part of ||)
        return True
    if re.search(r"&&|\|\|", s):
        return True
    if re.search(r"(?<!\d)>(?!&)", s):  # > redirect not part of e.g. 2>&1
        return True
    if re.search(r"(?<!<)<(?!<)", s):  # < input redirect (not part of <<)
        return True
    if re.search(r"<<\b|<<-", s):  # heredoc
        return True
    if re.search(r";\s", s + " "):  # cmd; cmd
        return True
    return False


def filter_matches_command(flt: TomlFilter, cmd: str) -> bool:
    """Match a filter against a *bare* command. Pipeline-rejecting per §9.4."""
    if not cmd:
        return False
    if is_pipeline_command(cmd):
        return False
    return bool(flt.match_command.search(cmd))


def apply_filter(flt: TomlFilter, text: str, ctx: NormalizeCtx | None = None) -> NormalizeResult:
    """Run the 8-stage pipeline. Returns NormalizeResult.

    Per the safety contract: any exception in user-supplied regex
    substitution demotes to PASSTHROUGH rather than crashing.
    """
    try:
        return _apply_filter_inner(flt, text)
    except Exception as e:
        return NormalizeResult.passthrough(
            text, reason=f"filter '{flt.name}' raised {type(e).__name__}: {e}"
        )


def _apply_filter_inner(flt: TomlFilter, text: str) -> NormalizeResult:
    warnings: list[str] = []
    work = text

    # Stage 2: replace (line-by-line or whole-text? rtk does whole-text,
    # which matters for multi-line patterns).
    for r in flt.replace:
        work = r.pattern.sub(r.replacement, work)

    # Stage 3: match_output (happy-path short-circuit). First rule that
    # matches AND doesn't match its `unless` clause wins.
    for r in flt.match_output:
        if r.pattern.search(work):
            if r.unless is not None and r.unless.search(work):
                continue
            # Happy path collapse.
            return NormalizeResult.full(r.message)

    # Stage 4: strip / keep lines.
    lines = work.split("\n")
    if flt.keep_lines_matching:
        # Mutually exclusive — if keep is set, strip is ignored. The
        # final line list is *only* keepers, in original order.
        lines = [l for l in lines if any(p.search(l) for p in flt.keep_lines_matching)]
    elif flt.strip_lines_matching:
        lines = [l for l in lines if not any(p.search(l) for p in flt.strip_lines_matching)]

    # Stage 5: truncate per-line.
    if flt.truncate_lines_at is not None:
        n = int(flt.truncate_lines_at)
        if n > 0:
            new_lines = []
            for l in lines:
                if len(l) > n:
                    new_lines.append(l[:n] + "…")
                else:
                    new_lines.append(l)
            lines = new_lines

    # Stage 6: head/tail with omit marker.
    if flt.head_lines is not None or flt.tail_lines is not None:
        h = int(flt.head_lines or 0)
        t = int(flt.tail_lines or 0)
        if h + t < len(lines):
            omitted = len(lines) - h - t
            kept_head = lines[:h] if h else []
            kept_tail = lines[-t:] if t else []
            mid = [_safe_omit_msg(flt.omit_msg, omitted)]
            lines = kept_head + mid + kept_tail
            warnings.append(f"head/tail trimmed {omitted} lines")

    # Stage 7: absolute max_lines (counts the omit-message line).
    if flt.max_lines is not None:
        m = int(flt.max_lines)
        if m > 0 and len(lines) > m:
            omitted = len(lines) - m + 1  # +1 for the marker we're about to add
            lines = lines[: m - 1] + [_safe_omit_msg(flt.omit_msg, omitted)]
            warnings.append(f"max_lines capped at {m}")

    # Stage 8: on_empty fallback.
    out = "\n".join(lines)
    if out.strip() == "" and flt.on_empty:
        return NormalizeResult.full(flt.on_empty)

    if warnings:
        return NormalizeResult.degraded(out, warnings=warnings)
    return NormalizeResult.full(out)


def _safe_omit_msg(template: str, count: int) -> str:
    """Use a printf-style format if %d is present, else fall back to a
    literal append. Defensive against malformed templates."""
    try:
        if "%d" in template:
            return template % count
    except Exception:
        pass
    return f"{template} ({count} lines)"
