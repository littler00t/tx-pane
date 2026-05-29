"""Pure unit tests for tx_core.log byte-processing helpers."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tx_core.log import (
    _clean_line,
    _logs_cfg,
    _split_raw_by_newlines,
    process_raw_log,
)

from tx_core.output import stamp_lines


class TestStampLines:
    def test_prefixes_each_line_with_a_timestamp(self):
        out = stamp_lines(["a", "b", "c"])
        assert len(out) == 3
        for line in out:
            # Format: [HH:MM:SS] body
            assert line.startswith("[") and "] " in line

    def test_same_batch_shares_one_stamp(self):
        out = stamp_lines(["x", "y"])
        prefix_x, _ = out[0].split("] ", 1)
        prefix_y, _ = out[1].split("] ", 1)
        # All lines in a single call get the SAME read-time stamp.
        assert prefix_x == prefix_y

    def test_empty_input_returns_empty(self):
        assert stamp_lines([]) == []


class TestSplitRawByNewlines:
    def test_empty(self):
        assert _split_raw_by_newlines(b"") == []

    def test_no_trailing_newline(self):
        assert _split_raw_by_newlines(b"abc") == [(b"abc", 3)]

    def test_two_lines(self):
        assert _split_raw_by_newlines(b"a\nb\n") == [(b"a", 2), (b"b", 4)]

    def test_blank_line_preserved(self):
        # Empty middle line should still produce a tuple.
        assert _split_raw_by_newlines(b"a\n\nb\n") == [(b"a", 2), (b"", 3), (b"b", 5)]


class TestCleanLine:
    def test_plain_ascii(self):
        assert _clean_line(b"hello") == "hello"

    def test_strips_ansi(self):
        assert _clean_line(b"\x1b[31mred\x1b[0m") == "red"

    def test_strips_cr(self):
        assert _clean_line(b"abc\rdef") == "abcdef"

    def test_replaces_bad_utf8(self):
        # \xff alone is invalid UTF-8 → replacement char, not exception.
        out = _clean_line(b"hi\xffthere")
        assert "hi" in out and "there" in out


class TestProcessRawLog:
    def test_simple(self):
        kept, truncated, remaining, consumed = process_raw_log(
            b"a\nb\nc\n", max_lines=10, strip_blanks=True
        )
        assert kept == ["a", "b", "c"]
        assert not truncated
        assert remaining == 0
        assert consumed == 6

    def test_truncation(self):
        kept, truncated, remaining, _ = process_raw_log(
            b"a\nb\nc\nd\ne\n", max_lines=2, strip_blanks=False
        )
        assert kept == ["a", "b"]
        assert truncated
        assert remaining == 3

    def test_strip_blanks_collapses_runs(self):
        # Three or more blank lines should collapse to two.
        kept, _, _, _ = process_raw_log(
            b"a\n\n\n\nb\n", max_lines=10, strip_blanks=True
        )
        assert "a" in kept and "b" in kept
        blanks = sum(1 for line in kept if line.strip() == "")
        assert blanks <= 2

    def test_no_strip_keeps_blanks(self):
        kept, _, _, _ = process_raw_log(
            b"\n\n\n\n", max_lines=10, strip_blanks=False
        )
        assert kept == ["", "", "", ""]


class TestLogsCfg:
    def test_defaults_when_no_section(self):
        out = _logs_cfg({})
        assert out["max_size_mb"] == 100
        assert out["max_age_days"] == 30
        assert out["max_keep"] == 10
        assert out["sweep_interval_hours"] == 24

    def test_user_overrides(self):
        out = _logs_cfg({"logs": {"max_size_mb": 50, "max_keep": 5}})
        assert out["max_size_mb"] == 50
        assert out["max_keep"] == 5
        # Untouched defaults remain.
        assert out["max_age_days"] == 30

    def test_bad_value_ignored(self):
        out = _logs_cfg({"logs": {"max_size_mb": "not-a-number"}})
        assert out["max_size_mb"] == 100
