"""Module-wide constants for tx-pane.

Holds version strings, on-disk paths (resolved from TX_PANE_HOME), the v2 marker
regex, the ANSI strip regex, the set of recognised shell names, and the
DEFAULT_CONFIG dict that seeds ~/.tx-pane/config.toml.

Path constants are captured at import time from os.environ; subprocess
invocations of `tx-pane` therefore see TX_PANE_HOME, while in-process re-imports
(test fixtures) inherit whatever was set when this module first loaded.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

VERSION = "1.5.0"
PROTOCOL_VERSION = "v2"

TX_DIR = Path(os.environ.get("TX_PANE_HOME") or str(Path.home() / ".tx-pane"))
CONFIG_PATH = TX_DIR / "config.toml"
OFFSETS_PATH = TX_DIR / "offsets.json"
LOGS_DIR = TX_DIR / "logs"
LOCK_PATH = TX_DIR / ".lock"

# Marker protocol (v2): every tx-pane run/exec wraps the user command in a sentinel
# that prints a unique run-id and the wrapped command's exit code. We detect
# completion by spotting the marker line in the log, not by prompt regex.
MARKER_RE = re.compile(rb"\x01TX_END (r-[0-9a-f]+) (-?\d+)\x01")
MARKER_RE_STR = re.compile(r"\x01TX_END (r-[0-9a-f]+) (-?\d+)\x01")

ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)

SHELL_NAMES = {
    "bash", "zsh", "sh", "fish", "dash", "ksh",
    "csh", "tcsh", "nu", "pwsh", "elvish", "xonsh",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "defaults": {
        "max_lines": 200,
        "timeout": 30,
        "idle_method": "prompt",
        "idle_silence_ms": 300,
        "prompt_patterns": [
            r"\$\s*$",
            r"❯\s*$",
            r">>>\s*$",
            r"#\s*$",
        ],
        "waiting_patterns": [
            r"(?i)password( for [^:]+)?:?\s*$",
            r"(?i)passphrase[^:]*:?\s*$",
            r"\(yes/no\)\s*\??\s*$",
            r"(?i)\[y/n\]\s*\??\s*$",
            r"(?i)are you sure\??\s*$",
            r"(?i)continue\??\s*$",
        ],
        "strip": True,
        "strip_ansi": True,
        "tmux_session": "tx-pane",
        "max_run_history": 100,
        "history_limit": 100000,
        # auto_reinstall_hook: when a run finalizes with exit_code=None (hook
        # missing), the next run automatically resends SHELL_INIT_SETUP before
        # sending the wrap. Disable if the overhead is unwanted or the user's
        # .bashrc/.zshrc deliberately replaces PROMPT_COMMAND every prompt.
        "auto_reinstall_hook": True,
    },
    "protocol": {
        "version": PROTOCOL_VERSION,
    },
    "security": {
        "command_allowlist": "all",
        "redact_patterns": [],
        "confirm_patterns": [],
        "confirm_mode": "interactive",
    },
    # Log-file rotation (~/.tx-pane/logs/<pane>.log). Rotation triggers when
    # pipe-pane (re)starts and the existing log exceeds max_size_mb. Aged
    # rotated logs are swept by `tx-pane maintain` or lazily on `tx-pane ls`.
    "logs": {
        "max_size_mb": 100,
        "max_age_days": 30,
        "max_keep": 10,
        "sweep_interval_hours": 24,
    },
    # Output compaction.
    # default_mode = "terse" → compaction is on for output commands unless
    # a caller passes --raw or sets a per-pane mode explicitly. Env-var
    # escape hatch: TX_PANE_NO_COMPACT=1 short-circuits to identity at every call.
    #
    # must_keep_patterns seed lines that L3 RLE *must not* collapse, even
    # if they look "near-identical". Defaults err on the side of safety:
    # any error/fail/fatal mention, git-log commit headers (the canonical
    # near-identical pitfall), and tx-pane's own marker echoes.
    "compact": {
        "default_mode": "terse",
        "default_token_budget": 4000,
        "strip_banners": True,
        "collapse_repeats": True,
        "collapse_repeats_threshold": 3,
        "must_keep_patterns": [
            r"(?i)\berror\b",
            r"(?i)\bfail(ed|ure|ing)?\b",
            r"(?i)\bfatal\b",
            r"(?i)\bcritical\b",
            r"(?i)\bpanic\b",
            r"(?i)\btraceback\b",
            r"^commit [0-9a-f]{7,40}\b",
        ],
        "telemetry": {
            "enabled": True,
            "max_size_mb": 10,
        },
        # L5 cross-call dedup — ships disabled. After a release of
        # `tx-pane compact-stats --dedup-would-hit` telemetry, an opt-in
        # flip is safe. ttl_seconds bounds staleness; cache_size_per_pane
        # bounds offsets.json growth.
        "dedup": {
            "enabled": False,
            "ttl_seconds": 60,
            "cache_size_per_pane": 32,
        },
    },
}
