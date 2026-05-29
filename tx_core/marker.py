"""v2 marker protocol: run-id generation, command wrapping, marker detection.

A run is bracketed by a unique run-id stored in the shell variable
`__tx_run_id`. The PROMPT_COMMAND / precmd / fish_postexec hook prints a
sentinel line `\\x01TX_END <run-id> <exit-code>\\x01` after every prompt,
which the pane log captures via pipe-pane. Detecting that marker is the
primary completion signal for `tx run` / `tx exec` / `tx wait-run`.
"""

from __future__ import annotations

import re
import secrets

from tx_core.constants import MARKER_RE_STR


def make_run_id() -> str:
    """Return a short unique run identifier like 'r-7c2a8d'."""
    return f"r-{secrets.token_hex(3)}"


SHELL_INIT_SETUP = (
    "__tx_emit() { __tx_st=$?; "
    'if [ -n "$__tx_run_id" ]; then '
    "printf '\\001TX_END %s %s\\001\\n' "
    '"$__tx_run_id" "$__tx_st"; '
    "__tx_run_id=; "
    "fi; }; "
    'if [ -n "$BASH_VERSION" ]; then '
    "PROMPT_COMMAND='__tx_emit'; "
    'elif [ -n "$ZSH_VERSION" ]; then '
    "precmd() { __tx_emit; }; "
    "fi"
)

# fish's syntax is incompatible with sh/bash, so we ship a separate setup
# for `tx new --shell fish`. fish lacks PROMPT_COMMAND; instead we hook the
# `fish_postexec` event, which fires after every interactive command (and
# preserves the previous command's $status). The wrap_command form
# `__tx_run_id=...` works in fish too — it sets a fish shell var.
SHELL_INIT_SETUP_FISH = (
    "function __tx_emit --on-event fish_postexec; "
    "set -l __tx_st $status; "
    'if set -q __tx_run_id; '
    'printf \'\\001TX_END %s %s\\001\\n\' "$__tx_run_id" "$__tx_st"; '
    "set -e __tx_run_id; "
    "end; end"
)


def shell_init_setup_for(shell: str | None) -> str:
    """Return the marker-hook installation snippet for the given shell.

    Defaults to the bash/zsh form for unknown / sh-family shells; it's a
    no-op there but a single inline expression they can swallow.
    """
    if shell == "fish":
        return SHELL_INIT_SETUP_FISH
    return SHELL_INIT_SETUP


def wrap_command(cmd: str, run_id: str) -> str:
    """Set the run-id then run the user command.

    Marker emission is handled by the shell's PROMPT_COMMAND (bash) or precmd
    (zsh) hook installed at pane creation time via SHELL_INIT_SETUP. The hook
    runs *after* every interactive command — including ones interrupted by
    C-c — so the marker is robustly emitted regardless of how the command
    ends.

    Caveats (documented):
      - Multi-line commands: only the final statement is tracked (the hook
        fires after each top-level statement).
      - Backgrounded commands (`cmd &`): marker fires when backgrounding
        succeeds; reflects backgrounding exit code (almost always 0), not
        the eventual exit of the background process.
    """
    return f"__tx_run_id={run_id}; {cmd}"


def find_run_marker(raw: bytes, run_id: str) -> tuple[int, int, int] | None:
    """Locate the marker for `run_id` in raw log bytes.

    Returns (line_start_offset, line_end_offset, exit_code) where the offsets
    cover the entire marker line including its trailing newline (if any).
    None if the marker is not present.
    """
    pattern = re.compile(
        rb"\x01TX_END " + re.escape(run_id.encode("ascii")) + rb" (-?\d+)\x01"
    )
    m = pattern.search(raw)
    if not m:
        return None
    exit_code = int(m.group(1))
    # Expand to the whole line containing the marker.
    line_start = raw.rfind(b"\n", 0, m.start()) + 1  # 0 if no preceding newline
    line_end = raw.find(b"\n", m.end())
    line_end = (line_end + 1) if line_end != -1 else len(raw)
    return line_start, line_end, exit_code


_ECHO_WRAP_RE = re.compile(r"^.*TX_END r-[0-9a-f]+.*$", re.MULTILINE)


def strip_run_markers(text: str) -> str:
    """Remove all forms of the v2 marker from returned output:

    - actual marker bytes (the `\\x01TX_END <rid> <code>\\x01` byte sequence)
    - echoed-wrap lines (lines that contain the literal text `TX_END r-XXXXXX`,
      which appears when the shell echoes the typed printf command before
      running it)

    After substitution, collapses any blank-line pairs the removals leave behind.
    """
    text = MARKER_RE_STR.sub("", text)
    text = _ECHO_WRAP_RE.sub("", text)
    text = re.sub(r"\n[ \t]*\n", "\n", text)
    return text
