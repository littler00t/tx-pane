"""Unit tests for the v2 marker protocol helpers."""

from __future__ import annotations

import re


def test_make_run_id_format(tx_module):
    rid = tx_module.make_run_id()
    assert rid.startswith("r-")
    assert re.fullmatch(r"r-[0-9a-f]{6}", rid), rid


def test_make_run_id_unique(tx_module):
    ids = {tx_module.make_run_id() for _ in range(200)}
    assert len(ids) >= 199  # vanishing probability of collision in 200 of 16M


def test_wrap_command_shape(tx_module):
    wrapped = tx_module.wrap_command("echo hi", "r-abc123")
    # Hook-mode wrap is a single line: set run-id, then run cmd.
    assert wrapped == "__tx_run_id=r-abc123; echo hi"


def test_shell_init_setup_contains_emit_and_hooks(tx_module):
    setup = tx_module.SHELL_INIT_SETUP
    assert "__tx_emit" in setup
    assert "PROMPT_COMMAND" in setup
    assert "precmd" in setup
    assert "TX_END" in setup


def test_find_run_marker_basic(tx_module):
    raw = b"some output\nmore output\n\x01TX_END r-abc123 0\x01\n$ "
    found = tx_module.find_run_marker(raw, "r-abc123")
    assert found is not None
    line_start, line_end, exit_code = found
    assert exit_code == 0
    # The substring between [line_start:line_end] is the marker line.
    assert raw[line_start:line_end].startswith(b"\x01TX_END r-abc123 0\x01")


def test_find_run_marker_nonzero_exit(tx_module):
    raw = b"fail trace\n\x01TX_END r-deadbe 1\x01\n"
    found = tx_module.find_run_marker(raw, "r-deadbe")
    assert found is not None
    _, _, exit_code = found
    assert exit_code == 1


def test_find_run_marker_negative_exit(tx_module):
    raw = b"\x01TX_END r-ff0011 -1\x01\n"
    found = tx_module.find_run_marker(raw, "r-ff0011")
    assert found is not None
    _, _, exit_code = found
    assert exit_code == -1


def test_find_run_marker_absent(tx_module):
    raw = b"output but no marker\n$ "
    assert tx_module.find_run_marker(raw, "r-abc123") is None


def test_find_run_marker_wrong_id_returns_none(tx_module):
    raw = b"output\n\x01TX_END r-zzzzzz 0\x01\n"
    assert tx_module.find_run_marker(raw, "r-abc123") is None


def test_find_run_marker_at_start_of_buffer(tx_module):
    # No preceding newline; line_start should be 0.
    raw = b"\x01TX_END r-aaa111 7\x01\nrest"
    found = tx_module.find_run_marker(raw, "r-aaa111")
    assert found is not None
    line_start, line_end, exit_code = found
    assert line_start == 0
    assert exit_code == 7


def test_find_run_marker_no_trailing_newline(tx_module):
    raw = b"prior\n\x01TX_END r-tail99 0\x01"
    found = tx_module.find_run_marker(raw, "r-tail99")
    assert found is not None
    line_start, line_end, _ = found
    assert line_end == len(raw)


def test_strip_run_markers_removes_marker(tx_module):
    text = "hello\n\x01TX_END r-abc123 0\x01\nworld"
    cleaned = tx_module.strip_run_markers(text)
    assert "TX_END" not in cleaned
    assert "hello" in cleaned and "world" in cleaned


def test_strip_run_markers_multiple(tx_module):
    text = "a\n\x01TX_END r-111111 0\x01\nb\n\x01TX_END r-222222 1\x01\nc"
    cleaned = tx_module.strip_run_markers(text)
    assert "TX_END" not in cleaned
    assert "a" in cleaned and "b" in cleaned and "c" in cleaned


def test_strip_run_markers_no_markers_unchanged(tx_module):
    text = "plain output\nno markers here"
    assert tx_module.strip_run_markers(text) == text


def test_strip_run_markers_removes_echoed_wrap_line(tx_module):
    # When the shell echoes the wrapped command, the typed line contains
    # 'TX_END r-XXX' as literal text (not the byte form). Strip it.
    text = (
        "$ { echo hi; }; printf '\\001TX_END r-abc123 %s\\001\\n' \"$?\"\n"
        "hi\n"
        "\x01TX_END r-abc123 0\x01\n"
    )
    cleaned = tx_module.strip_run_markers(text)
    assert "TX_END" not in cleaned
    assert "hi" in cleaned
