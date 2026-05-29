"""Output helpers: error/warning printing, redaction, ANSI policy, timestamps.

These functions write to stdout via `click.echo` and read security config
from the loaded TOML dict. They are leaves — no `tx_core` imports — so
they can be used freely by every other module.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from typing import Any

import click


def err(msg: str, exit_code: int = 1) -> None:
    click.echo(f"[error: {msg}]")
    sys.exit(exit_code)


def warn(msg: str) -> None:
    click.echo(f"[warning: {msg}]")


# Compiled-regex cache. Keyed by the tuple of pattern strings so distinct
# pattern lists keep distinct entries; cleared only on process exit.
_REDACT_COMPILED_CACHE: dict[tuple[str, ...], list[re.Pattern[str]]] = {}


def _compile_redactions(patterns: list[str]) -> list[re.Pattern[str]]:
    key = tuple(patterns)
    cached = _REDACT_COMPILED_CACHE.get(key)
    if cached is not None:
        return cached
    compiled: list[re.Pattern[str]] = []
    for p in patterns:
        if not isinstance(p, str):
            continue
        try:
            compiled.append(re.compile(p, re.DOTALL))
        except re.error:
            continue
    _REDACT_COMPILED_CACHE[key] = compiled
    return compiled


def apply_redactions(text: str, cfg: dict[str, Any]) -> str:
    """Apply configured `[security] redact_patterns` to text.

    Each match is replaced with '[redacted]'. Patterns that fail to compile
    are silently skipped — bad regex shouldn't break command output entirely.
    Operates on the returned string only; the on-disk log is never rewritten.
    """
    sec = cfg.get("security") if isinstance(cfg, dict) else None
    patterns = (sec or {}).get("redact_patterns") or []
    if not patterns:
        return text
    for pat in _compile_redactions(list(patterns)):
        text = pat.sub("[redacted]", text)
    return text


def resolve_strip_ansi(cfg: dict[str, Any], keep_ansi_flag: bool) -> bool:
    """Return whether ANSI should be stripped. CLI `--keep-ansi` overrides."""
    if keep_ansi_flag:
        return False
    defaults = cfg.get("defaults") if isinstance(cfg, dict) else None
    return bool((defaults or {}).get("strip_ansi", True))


def stamp_lines(lines: list[str]) -> list[str]:
    """Prefix each line with the wall-clock '[hh:mm:ss]' at read time.

    Best-effort: the pipe-pane log carries no per-line timestamps, so all
    lines emitted by a single read share the same stamp. Useful as a
    coarse marker for "when did this batch reach me" rather than per-line
    history.
    """
    ts = datetime.now().strftime("[%H:%M:%S]")
    return [f"{ts} {line}" for line in lines]
