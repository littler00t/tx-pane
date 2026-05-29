"""Command allowlist + confirm-pattern policy.

`check_allowlist` is enforced before any pane-bound command is sent; it
combines the global `[security] command_allowlist` with the per-pane
`[panes.<id>] command_allowlist` (AND-merged). `check_confirm` overlays
an interactive (or scripted) yes/no prompt for patterns the operator
flagged as risky.
"""

from __future__ import annotations

import re
import sys
from typing import Any

import click

from tx_core.output import err, warn


# Tracks which deprecation warnings have been shown so we don't spam the
# user on every invocation in a script loop.
_DEPRECATION_WARNED: set[str] = set()


def _resolve_allowlist(cfg: dict[str, Any]) -> str | list[str]:
    """Return the active global allowlist setting, applying back-compat for the
    old 'allowed_commands' key. Emits the deprecation warning at most once per
    process per key.
    """
    security = cfg.get("security", {}) or {}
    if "command_allowlist" in security:
        return security["command_allowlist"]
    if "allowed_commands" in security:
        if "allowed_commands" not in _DEPRECATION_WARNED:
            warn(
                "'allowed_commands' is deprecated — rename to 'command_allowlist' "
                "in ~/.tx-pane/config.toml ('all' | 'none' | [patterns])"
            )
            _DEPRECATION_WARNED.add("allowed_commands")
        legacy = security["allowed_commands"] or []
        return "all" if not legacy else list(legacy)
    return "all"


def _resolve_pane_allowlist(cfg: dict[str, Any], pane_id: str | None) -> str | list[str]:
    """Return the per-pane allowlist setting under [panes.<id>].

    Defaults to "all" when no per-pane config exists. The shape is the same
    as the global allowlist: "all" | "none" | [patterns].
    """
    if pane_id is None:
        return "all"
    panes = cfg.get("panes")
    if not isinstance(panes, dict):
        return "all"
    pane_cfg = panes.get(pane_id)
    if not isinstance(pane_cfg, dict):
        return "all"
    if "command_allowlist" in pane_cfg:
        return pane_cfg["command_allowlist"]
    return "all"


def _allowlist_config_error(message: str) -> None:
    err(f"invalid command_allowlist config: {message}")


def _check_one_allowlist(rule: str | list[str], text: str) -> str | None:
    """Return offending token if `text` is not allowed by `rule`, else None."""
    if rule == "all":
        return None
    stripped = text.lstrip()
    if not stripped:
        return None
    first = stripped.split(None, 1)[0]
    if rule == "none":
        return first
    if not isinstance(rule, list):
        _allowlist_config_error("expected 'all', 'none', or a list of string patterns")
    if not rule:
        _allowlist_config_error("list must contain at least one token or /regex/ entry")
    for pat in rule:
        if not isinstance(pat, str):
            _allowlist_config_error("list entries must be strings")
        if len(pat) >= 2 and pat.startswith("/") and pat.endswith("/"):
            try:
                if re.search(pat[1:-1], stripped):
                    return None
            except re.error:
                _allowlist_config_error(f"invalid regex entry {pat!r}")
        elif pat == first:
            return None
    return first


def check_allowlist(text: str, cfg: dict[str, Any], pane_id: str | None = None) -> str | None:
    """Return the offending token if the command is not allowed, else None.

    Per-pane allowlists AND-merge with the global one: a command must satisfy
    *both* lists. Either list may be "all" (the permissive default) or "none"
    (blanket deny) or a list of patterns. Patterns wrapped in `/.../` are
    treated as regex over the full command; everything else is matched as the
    first whitespace-delimited token.
    """
    global_offender = _check_one_allowlist(_resolve_allowlist(cfg), text)
    if global_offender is not None:
        return global_offender
    return _check_one_allowlist(_resolve_pane_allowlist(cfg, pane_id), text)


class ConfirmDenied(Exception):
    """Raised when a confirm-pattern fires and the user declines."""

    def __init__(self, pattern: str, mode: str) -> None:
        super().__init__(pattern)
        self.pattern = pattern
        self.mode = mode


def _confirm_match(cmd: str, cfg: dict[str, Any]) -> str | None:
    """Return the first matching confirm_pattern, or None."""
    sec = cfg.get("security") if isinstance(cfg, dict) else None
    patterns = (sec or {}).get("confirm_patterns") or []
    for p in patterns:
        if not isinstance(p, str):
            continue
        try:
            if re.search(p, cmd):
                return p
        except re.error:
            continue
    return None


def check_confirm(cmd: str, cfg: dict[str, Any], yes: bool) -> None:
    """Apply confirm-pattern policy. Raises ConfirmDenied (via err) on deny.

    Mode semantics (`[security] confirm_mode`):
      - "interactive" (default): prompt the user when stdin+stderr are a TTY,
        else refuse with an instructive error pointing at --yes / confirm_mode.
      - "deny": always refuse on match (agents must opt in via --yes).
      - "allow": always permit on match (logs a warning).
    """
    if yes:
        return
    matched = _confirm_match(cmd, cfg)
    if matched is None:
        return
    sec = cfg.get("security") if isinstance(cfg, dict) else None
    mode = ((sec or {}).get("confirm_mode") or "interactive").lower()
    if mode == "allow":
        warn(f"command matches confirm_pattern '{matched}' — allowed by confirm_mode=allow")
        return
    if mode == "deny":
        err(
            f"command matches confirm_pattern '{matched}' and confirm_mode=deny; "
            f"pass --yes to proceed, or change [security] confirm_mode."
        )
    # interactive: only prompt when we have a real TTY in both directions.
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        err(
            f"command matches confirm_pattern '{matched}' — confirmation required. "
            f"Pass --yes for non-interactive use, or set [security] confirm_mode = "
            f"'allow'|'deny' for a deterministic policy."
        )
    click.echo(
        f"[confirm: command matches '{matched}' — type 'yes' to proceed]",
        err=True,
    )
    try:
        answer = input("Proceed? [yes/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "yes":
        err(f"confirmation declined — refusing to send '{cmd}'")
