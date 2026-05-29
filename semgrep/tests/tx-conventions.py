"""Fixture file for tx-conventions.yaml.

Each rule has positive and negative annotations; the negative examples
mirror the canonical idiomatic form named in the rule's message.
"""

import os
import secrets
import sys
from pathlib import Path

import click
import libtmux

# Stand-in helpers — only the call shapes matter for pattern-matching.
from tx_core.config import load_offsets, offsets_lock, save_offsets
from tx_core.output import err, warn
from tx_core.marker import find_run_marker, make_run_id, wrap_command
from tx_core.tmux import get_server


# =====================================================================
# tx-error-via-err-helper
# =====================================================================


def bad_error_fstring() -> None:
    msg = "config not found"
    # ruleid: tx-error-via-err-helper
    click.echo(f"[error: {msg}]")
    sys.exit(1)


def bad_error_static() -> None:
    # ruleid: tx-error-via-err-helper
    click.echo("[error: required arg missing]")
    sys.exit(2)


def good_via_err_helper() -> None:
    # ok: tx-error-via-err-helper
    err("required arg missing")


def good_unrelated_echo() -> None:
    # ok: tx-error-via-err-helper
    click.echo("[exit:0]")


# =====================================================================
# tx-warn-via-warn-helper
# =====================================================================


def bad_warning_fstring(name: str) -> None:
    # ruleid: tx-warn-via-warn-helper
    click.echo(f"[warning: skipping {name}]")


def bad_warning_static() -> None:
    # ruleid: tx-warn-via-warn-helper
    click.echo("[warning: noisy]")


def good_via_warn_helper(name: str) -> None:
    # ok: tx-warn-via-warn-helper
    warn(f"skipping {name}")


def good_unrelated_warning_echo() -> None:
    # ok: tx-warn-via-warn-helper
    click.echo("[killed: pane killed]")


# =====================================================================
# tx-offsets-mutation-locked
# =====================================================================


def bad_save_outside_lock() -> None:
    offsets = load_offsets()
    offsets["x"] = 1
    # ruleid: tx-offsets-mutation-locked
    save_offsets(offsets)


def good_save_inside_lock() -> None:
    with offsets_lock():
        offsets = load_offsets()
        offsets["x"] = 1
        # ok: tx-offsets-mutation-locked
        save_offsets(offsets)


def good_save_inside_named_lock() -> None:
    with offsets_lock() as _lock:
        offsets = load_offsets()
        offsets["x"] = 1
        # ok: tx-offsets-mutation-locked
        save_offsets(offsets)


# =====================================================================
# tx-tmux-via-wrappers
# =====================================================================


def bad_libtmux_server_direct() -> None:
    # ruleid: tx-tmux-via-wrappers
    server = libtmux.Server()
    _ = server


def good_via_wrapper() -> None:
    # ok: tx-tmux-via-wrappers
    server = get_server()
    _ = server


# =====================================================================
# tx-marker-via-protocol
# =====================================================================


def bad_inline_run_id() -> str:
    # ruleid: tx-marker-via-protocol
    return f"r-{secrets.token_hex(3)}"


def bad_inline_run_id_concat() -> str:
    # ruleid: tx-marker-via-protocol
    return "r-" + secrets.token_hex(3)


def bad_marker_byte_sequence(rid: str, exit_code: int) -> bytes:
    # ruleid: tx-marker-via-protocol
    return f"\x01TX_END {rid} {exit_code}\x01".encode("ascii")


def bad_marker_bytes_literal() -> bytes:
    # ruleid: tx-marker-via-protocol
    return b"\x01TX_END r-abc123 0\x01"


def good_via_make_run_id() -> str:
    # ok: tx-marker-via-protocol
    return make_run_id()


def good_via_wrap_command(cmd: str) -> str:
    rid = make_run_id()
    # ok: tx-marker-via-protocol
    return wrap_command(cmd, rid)


def good_via_find_run_marker(raw: bytes, rid: str):
    # ok: tx-marker-via-protocol
    return find_run_marker(raw, rid)


# =====================================================================
# tx-no-hardcoded-paths
# =====================================================================


def bad_hardcoded_path_str() -> str:
    # ruleid: tx-no-hardcoded-paths
    return "~/.tx/offsets.json"


def bad_expanduser_tx_dir() -> str:
    # ruleid: tx-no-hardcoded-paths
    return os.path.expanduser("~/.tx")


def bad_expanduser_tx_subpath() -> str:
    # ruleid: tx-no-hardcoded-paths
    return os.path.expanduser("~/.tx/logs")


def bad_path_expanduser_tx_dir() -> Path:
    # ruleid: tx-no-hardcoded-paths
    return Path("~/.tx").expanduser()


def bad_path_expanduser_tx_subpath() -> Path:
    # ruleid: tx-no-hardcoded-paths
    return Path("~/.tx/logs").expanduser()


def good_via_constants() -> None:
    from tx_core.constants import LOGS_DIR, OFFSETS_PATH, TX_DIR
    # ok: tx-no-hardcoded-paths
    _ = TX_DIR
    _ = LOGS_DIR
    _ = OFFSETS_PATH


def good_unrelated_path() -> Path:
    # ok: tx-no-hardcoded-paths
    return Path("~/Downloads/foo.txt").expanduser()
