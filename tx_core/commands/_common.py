"""Kitchen-sink imports shared by every `tx_core.commands.*` submodule.

The 34 `cmd_*` handlers each reference many helpers across tx_core.
Rather than enumerate identical import blocks in five files, every
command submodule does `from tx_core.commands._common import *`. The
underscore-aliased `tx_compact` imports mirror the names used by the
original monolithic `tx` script so we can move command bodies verbatim.
"""

from __future__ import annotations

import difflib  # noqa: F401
import hashlib  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import secrets  # noqa: F401
import shlex  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import tempfile  # noqa: F401
import time  # noqa: F401
import tomllib  # noqa: F401
from datetime import datetime, timezone  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401

import click  # noqa: F401
import libtmux  # noqa: F401
import libtmux.exc  # noqa: F401
import tomli_w  # noqa: F401

from tx_compact import (  # noqa: F401
    HANDLE_PLACEHOLDER as _HANDLE_PLACEHOLDER,
    CompactCtx,
    CompactResult,
    compact as _compact,
    dedup as _dedup,
    handle_store as _handle_store,
    is_compaction_disabled,
    telemetry_record as _telemetry_record,
)
from tx_core.cli import cli  # noqa: F401
from tx_core.config import (  # noqa: F401
    _OffsetsLock,
    _deepcopy,
    ensure_dirs,
    find_run_record,
    load_config,
    load_offsets,
    now_iso,
    offsets_lock,
    record_run_end,
    record_run_start,
    save_offsets,
)
from tx_core.constants import (  # noqa: F401
    ANSI_RE,
    CONFIG_PATH,
    DEFAULT_CONFIG,
    LOCK_PATH,
    LOGS_DIR,
    MARKER_RE,
    MARKER_RE_STR,
    OFFSETS_PATH,
    PROTOCOL_VERSION,
    SHELL_NAMES,
    TX_DIR,
    VERSION,
)
from tx_core.help_text import HELP_TEXT  # noqa: F401
from tx_core.log import (  # noqa: F401
    _clean_line,
    _logs_cfg,
    _rotated_log_paths,
    _split_raw_by_newlines,
    maybe_rotate_log,
    maybe_sweep_aged_logs,
    process_raw_log,
    rotate_log,
    sweep_aged_logs,
)
from tx_core.marker import (  # noqa: F401
    _ECHO_WRAP_RE,
    SHELL_INIT_SETUP,
    SHELL_INIT_SETUP_FISH,
    find_run_marker,
    make_run_id,
    shell_init_setup_for,
    strip_run_markers,
    wrap_command,
)
from tx_core.output import (  # noqa: F401
    _REDACT_COMPILED_CACHE,
    _compile_redactions,
    apply_redactions,
    err,
    resolve_strip_ansi,
    stamp_lines,
    warn,
)
from tx_core.proc import (  # noqa: F401
    _lsof_cwd,
    _proc_children,
    _proc_comm,
    _read_proc_cwd,
    pane_alt_screen,
    read_pane_cwd,
    tmux_attached_clients,
    walk_foreground,
)
from tx_core.render import (  # noqa: F401
    _apply_range_grep,
    _build_compact_ctx,
    _compact_options,
    _duration_ms,
    _emit_handle_buffer,
    _emit_run_json,
    _emit_telemetry,
    _maybe_apply_dedup,
    _maybe_attach_handle,
    _per_call_compact_mode,
    _read_cleaned_text,
    _render_buffer_output,
    _render_run_output,
    _resolve_compact_mode,
    _resolve_pane_for_input,
    _strip_lines,
)
from tx_core.runner import (  # noqa: F401
    _apply_on_timeout,
    _internal_marker_run,
    _internal_paste_then_marker,
    _maybe_reinstall_hook,
    _start_run,
)
from tx_core.security import (  # noqa: F401
    ConfirmDenied,
    _DEPRECATION_WARNED,
    _check_one_allowlist,
    _confirm_match,
    _resolve_allowlist,
    _resolve_pane_allowlist,
    check_allowlist,
    check_confirm,
)
from tx_core.state import (  # noqa: F401
    _matches_waiting,
    _tail_text,
    finalize_runs,
    pane_log_path,
    pane_state,
    pane_status,
    poll_until_idle,
    render_log_range,
    require_pane,
)
from tx_core.tmux import (  # noqa: F401
    allocate_pane,
    find_pane_anywhere,
    get_or_create_session,
    get_server,
    start_pipe_pane,
    stop_pipe_pane,
)
from tx_core.util import (  # noqa: F401
    _detect_tmux_version,
    _duration_str,
    _parse_duration,
    _resolve_bookmark,
    _running_for_seconds,
    _sudo_prefix,
)
from tx_core.wait import (  # noqa: F401
    _last_non_empty_line,
    busy_error_message,
    truthful_timeout_message,
    wait_for_idle,
    wait_for_marker,
    wait_for_marker_or_bound,
)


# Export every name (including underscore-prefixed ones) so command modules
# can do `from tx_core.commands._common import *` and pick up the helpers
# the monolithic `tx` script exposed at module level.
__all__ = [n for n in dir() if not n.startswith("__")]
