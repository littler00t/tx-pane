"""Unit tests for runner helpers (_maybe_reinstall_hook + _apply_on_timeout).

These paths are only triggered in specific runtime conditions (hook
missing, --on-timeout cancel/kill) that the integration suite doesn't
reliably reach.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tx_core.runner import _apply_on_timeout, _maybe_reinstall_hook


class _FakePane:
    """A minimal libtmux.Pane stand-in that records send_keys / cmd calls."""

    def __init__(self, send_raises=False, cmd_raises=False):
        self.send_calls: list[tuple[str, dict]] = []
        self.cmd_calls: list[tuple[tuple, dict]] = []
        self._send_raises = send_raises
        self._cmd_raises = cmd_raises

    def send_keys(self, snippet, **kw):
        self.send_calls.append((snippet, kw))
        if self._send_raises:
            raise RuntimeError("tmux unreachable")

    def cmd(self, *a, **kw):
        self.cmd_calls.append((a, kw))
        if self._cmd_raises:
            raise RuntimeError("tmux unreachable")
        class _R:
            stdout = []
            stderr = []
        return _R()


class TestMaybeReinstallHook:
    def test_skips_when_hook_ok(self, tmp_path):
        pane = _FakePane()
        state = {"hook_ok": True}
        cfg = {"defaults": {"auto_reinstall_hook": True}}
        assert _maybe_reinstall_hook(pane, tmp_path / "p1.log", state, cfg) is False
        assert pane.send_calls == []

    def test_skips_when_auto_reinstall_disabled(self, tmp_path):
        pane = _FakePane()
        state = {"hook_ok": False}
        cfg = {"defaults": {"auto_reinstall_hook": False}}
        assert _maybe_reinstall_hook(pane, tmp_path / "p1.log", state, cfg) is False
        assert pane.send_calls == []

    def test_sends_bash_snippet_when_hook_missing(self, tmp_path):
        pane = _FakePane()
        log = tmp_path / "p1.log"
        log.write_bytes(b"prior content\n")
        state = {"hook_ok": False, "shell": "bash"}
        cfg = {"defaults": {"auto_reinstall_hook": True}}
        out = _maybe_reinstall_hook(pane, log, state, cfg)
        assert out is True
        # Snippet was sent with literal=True / enter=True.
        assert len(pane.send_calls) == 1
        snippet, kw = pane.send_calls[0]
        assert "PROMPT_COMMAND" in snippet
        assert kw["literal"] is True and kw["enter"] is True
        # State flipped optimistic-OK + tail_offset advanced past the snippet.
        assert state["hook_ok"] is True
        assert state["tail_offset"] == log.stat().st_size

    def test_sends_fish_snippet_when_shell_is_fish(self, tmp_path):
        pane = _FakePane()
        log = tmp_path / "p1.log"
        log.touch()
        state = {"hook_ok": False, "shell": "fish"}
        cfg = {"defaults": {"auto_reinstall_hook": True}}
        _maybe_reinstall_hook(pane, log, state, cfg)
        snippet, _ = pane.send_calls[0]
        assert "fish_postexec" in snippet

    def test_returns_false_when_send_keys_raises(self, tmp_path):
        pane = _FakePane(send_raises=True)
        log = tmp_path / "p1.log"
        log.touch()
        state = {"hook_ok": False}
        cfg = {"defaults": {"auto_reinstall_hook": True}}
        out = _maybe_reinstall_hook(pane, log, state, cfg)
        assert out is False
        # Optimistic flip didn't happen because the send failed.
        assert state.get("hook_ok") is False

    def test_missing_log_doesnt_crash(self, tmp_path):
        pane = _FakePane()
        state = {"hook_ok": False}
        cfg = {"defaults": {"auto_reinstall_hook": True}}
        # Log file doesn't exist — stat() raises OSError; the helper
        # swallows it but still flips hook_ok True.
        assert _maybe_reinstall_hook(pane, tmp_path / "missing.log", state, cfg) is True
        assert state["hook_ok"] is True


class TestApplyOnTimeout:
    def test_policy_report_is_pure_readback(self, tmp_path):
        pane = _FakePane()
        log = tmp_path / "p1.log"
        log.write_bytes(b"" * 10 + b"x" * 50)
        found, exit_code, end, idle, note = _apply_on_timeout(
            "report", pane, log, start_offset=0, run_id="r-abc", cfg_defaults={}
        )
        assert found is False
        assert exit_code is None
        assert end == log.stat().st_size
        assert note is None
        # report must not send any keys.
        assert pane.send_calls == []
        assert pane.cmd_calls == []

    def test_policy_cancel_sends_ctrl_c_then_waits(self, tmp_path, monkeypatch):
        pane = _FakePane()
        log = tmp_path / "p1.log"
        log.write_bytes(b"")
        # Stub wait_for_marker so the test doesn't actually poll for 3s.
        import tx_core.runner as _tcr
        monkeypatch.setattr(
            _tcr,
            "wait_for_marker",
            lambda *a, **kw: (True, 0, 42, 0.1),
        )
        found, exit_code, end, _idle, note = _apply_on_timeout(
            "cancel", pane, log, start_offset=0, run_id="r-abc", cfg_defaults={}
        )
        assert found is True
        assert exit_code == 0
        assert end == 42
        assert note == "cancelled via C-c"
        # Exactly one C-c send before re-waiting.
        assert pane.send_calls == [("C-c", {"enter": False, "suppress_history": False, "literal": False})]

    def test_policy_kill_sends_two_ctrl_cs_then_kills_pane(self, tmp_path):
        pane = _FakePane()
        log = tmp_path / "p1.log"
        log.write_bytes(b"some bytes")
        found, exit_code, end, _idle, note = _apply_on_timeout(
            "kill", pane, log, start_offset=0, run_id="r-abc", cfg_defaults={}
        )
        assert found is False
        assert exit_code is None
        assert end == log.stat().st_size
        assert "pane killed" in note
        # Two C-c sends.
        assert [c[0] for c in pane.send_calls] == ["C-c", "C-c"]
        # And a kill-pane via .cmd().
        assert pane.cmd_calls == [(("kill-pane",), {})]

    def test_policy_kill_swallows_tmux_errors(self, tmp_path):
        pane = _FakePane(send_raises=True, cmd_raises=True)
        log = tmp_path / "p1.log"
        log.touch()
        # Even when every tmux op raises, the helper still returns cleanly.
        found, _, end, _, note = _apply_on_timeout(
            "kill", pane, log, start_offset=0, run_id="r-abc", cfg_defaults={}
        )
        assert found is False
        assert note == "pane killed (tmux pane destroyed)"

    def test_unknown_policy_returns_dead_tuple(self, tmp_path):
        pane = _FakePane()
        log = tmp_path / "p1.log"
        log.touch()
        out = _apply_on_timeout(
            "BOGUS", pane, log, start_offset=0, run_id="r-x", cfg_defaults={}
        )
        assert out == (False, None, 0, 0.0, None)

    def test_policy_is_case_insensitive(self, tmp_path):
        pane = _FakePane()
        log = tmp_path / "p1.log"
        log.write_bytes(b"123")
        found, _, end, _, note = _apply_on_timeout(
            "REPORT", pane, log, start_offset=0, run_id="r-x", cfg_defaults={}
        )
        assert found is False and note is None
        assert end == 3
