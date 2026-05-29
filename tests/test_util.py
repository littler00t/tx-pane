"""Pure unit tests for tx_core.util — no tmux, no subprocess (except tmux -V)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tx_core.util import (
    _detect_tmux_version,
    _duration_str,
    _parse_duration,
    _running_for_seconds,
    _sudo_prefix,
)


class TestDurationStr:
    def test_no_end_returns_dash(self):
        assert _duration_str("2024-01-01T00:00:00Z", None) == "-"

    def test_milliseconds(self):
        assert _duration_str("2024-01-01T00:00:00Z", "2024-01-01T00:00:00.500Z") == "500ms"

    def test_seconds_with_decimal(self):
        out = _duration_str("2024-01-01T00:00:00Z", "2024-01-01T00:00:05.500Z")
        assert out == "5.5s"

    def test_minutes_and_seconds(self):
        out = _duration_str("2024-01-01T00:00:00Z", "2024-01-01T00:02:07Z")
        assert out == "2m7s"

    def test_malformed_returns_dash(self):
        assert _duration_str("not-a-date", "also-not-a-date") == "-"


class TestParseDuration:
    @pytest.mark.parametrize("spec,expected", [
        ("5", 5.0),
        ("5s", 5.0),
        ("2m", 120.0),
        ("1h", 3600.0),
        ("0.5s", 0.5),
        ("1.5m", 90.0),
    ])
    def test_valid(self, spec, expected):
        assert _parse_duration(spec) == expected

    def test_whitespace_and_case(self):
        assert _parse_duration("  2M ") == 120.0

    @pytest.mark.parametrize("spec", ["", "   ", "abc", "5x", "ms5"])
    def test_invalid_raises(self, spec):
        with pytest.raises(ValueError):
            _parse_duration(spec)

    def test_invalid_number_in_unit_suffix(self):
        # The unit char matches /smh/ but the prefix isn't a number — the
        # inner ValueError carries the "invalid number in '<spec>'" message,
        # distinct from the bare "invalid duration" path.
        with pytest.raises(ValueError, match="invalid number in"):
            _parse_duration("abcs")
        with pytest.raises(ValueError, match="invalid number in"):
            _parse_duration(".m")
        with pytest.raises(ValueError, match="invalid number in"):
            _parse_duration("nothing-h")


class TestDetectTmuxVersion:
    def test_returns_string(self):
        # tmux is installed in the dev container; the result is a non-empty
        # string regardless of the exact version.
        out = _detect_tmux_version()
        assert isinstance(out, str) and out

    def test_falls_back_when_tmux_missing(self, monkeypatch):
        def _raise(*a, **kw):
            raise FileNotFoundError("tmux: not found")
        monkeypatch.setattr("subprocess.run", _raise)
        assert _detect_tmux_version() == "unknown"

    def test_falls_back_on_empty_output(self, monkeypatch):
        class _Result:
            stdout = ""
            stderr = ""
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: _Result())
        assert _detect_tmux_version() == "unknown"


class TestRunningForSeconds:
    def test_missing_started(self):
        assert _running_for_seconds({}) is None

    def test_malformed_started(self):
        assert _running_for_seconds({"started": "not-a-date"}) is None

    def test_valid_returns_positive(self):
        out = _running_for_seconds({"started": "2020-01-01T00:00:00Z"})
        assert out is not None and out > 0


class TestSudoPrefix:
    def test_true_returns_prefix(self):
        assert _sudo_prefix(True) == "sudo -n "

    def test_false_returns_empty(self):
        assert _sudo_prefix(False) == ""
