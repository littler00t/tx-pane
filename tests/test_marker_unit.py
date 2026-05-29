"""Pure unit tests for tx_core.marker (no subprocess, no tmux)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tx_core.marker import (
    SHELL_INIT_SETUP,
    SHELL_INIT_SETUP_FISH,
    find_run_marker,
    make_run_id,
    shell_init_setup_for,
    strip_run_markers,
    wrap_command,
)


class TestMakeRunId:
    def test_shape(self):
        rid = make_run_id()
        assert rid.startswith("r-")
        assert re.fullmatch(r"r-[0-9a-f]+", rid)

    def test_unique(self):
        ids = {make_run_id() for _ in range(100)}
        assert len(ids) == 100


class TestShellInitSetupFor:
    @pytest.mark.parametrize("shell", [None, "bash", "zsh", "sh", "dash", "ksh"])
    def test_default_shell_form(self, shell):
        out = shell_init_setup_for(shell)
        assert "PROMPT_COMMAND" in out
        assert "precmd" in out
        assert out == SHELL_INIT_SETUP

    def test_fish_form(self):
        out = shell_init_setup_for("fish")
        assert "fish_postexec" in out
        assert out == SHELL_INIT_SETUP_FISH


class TestWrapCommand:
    def test_sets_run_id(self):
        out = wrap_command("echo hi", "r-abc123")
        assert out == "__tx_run_id=r-abc123; echo hi"


class TestFindRunMarker:
    def test_present(self):
        raw = b"prelude\n\x01TX_END r-abc 0\x01\nafter\n"
        result = find_run_marker(raw, "r-abc")
        assert result is not None
        line_start, line_end, exit_code = result
        assert exit_code == 0
        assert raw[line_start:line_end].endswith(b"\n")
        assert b"TX_END r-abc" in raw[line_start:line_end]

    def test_absent(self):
        assert find_run_marker(b"no marker here\n", "r-abc") is None

    def test_nonzero_exit(self):
        raw = b"\x01TX_END r-xyz 137\x01\n"
        result = find_run_marker(raw, "r-xyz")
        assert result is not None
        _, _, exit_code = result
        assert exit_code == 137

    def test_negative_exit(self):
        raw = b"\x01TX_END r-abc -1\x01\n"
        result = find_run_marker(raw, "r-abc")
        assert result is not None
        _, _, exit_code = result
        assert exit_code == -1

    def test_wrong_id_ignored(self):
        raw = b"\x01TX_END r-other 0\x01\n"
        assert find_run_marker(raw, "r-abc") is None

    def test_no_trailing_newline(self):
        raw = b"\x01TX_END r-abc 0\x01"
        result = find_run_marker(raw, "r-abc")
        assert result is not None
        _, line_end, _ = result
        assert line_end == len(raw)


class TestStripRunMarkers:
    def test_strips_marker_bytes(self):
        text = "hello\n\x01TX_END r-abc 0\x01\nworld\n"
        out = strip_run_markers(text)
        assert "TX_END" not in out
        assert "hello" in out and "world" in out

    def test_strips_echoed_wrap_line(self):
        text = "printf '\\001TX_END r-abc %s\\001\\n' \"$__tx_run_id\" \"$__tx_st\"\nhello\n"
        out = strip_run_markers(text)
        assert "TX_END r-abc" not in out
        assert "hello" in out

    def test_idempotent(self):
        text = "hello\nworld\n"
        assert strip_run_markers(strip_run_markers(text)) == strip_run_markers(text)
