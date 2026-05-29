"""Tests for config and offsets state management."""

from __future__ import annotations

import json
import os
from pathlib import Path

from conftest import patch_tx_paths


def test_load_offsets_missing_returns_default(tx_module, tx_home, monkeypatch):
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    data = tx_module.load_offsets()
    assert data == {"_next_id": 1, "_panes": {}}


def test_save_load_roundtrip(tx_module, tx_home, monkeypatch):
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    payload = {
        "_next_id": 5,
        "_panes": {"p1": "%1"},
        "p1": {"tmux_id": "%1", "tail_offset": 100, "continue_offset": None, "status": "idle"},
    }
    tx_module.save_offsets(payload)
    loaded = tx_module.load_offsets()
    assert loaded == payload


def test_save_offsets_is_atomic(tx_module, tx_home, monkeypatch):
    """The replace operation should leave only one offsets.json behind (no .tmp leftovers)."""
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    tx_module.save_offsets({"_next_id": 2, "_panes": {}})
    leftovers = [p for p in tx_home.iterdir() if p.name.startswith(".offsets.")]
    assert leftovers == []
    assert (tx_home / "offsets.json").exists()


def test_load_config_creates_default(tx_module, tx_home, monkeypatch):
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    cfg = tx_module.load_config()
    assert (tx_home / "config.toml").exists()
    assert cfg["defaults"]["tmux_session"] == "tx-pane"
    assert cfg["security"]["command_allowlist"] == "all"
    assert cfg["protocol"]["version"] == tx_module.PROTOCOL_VERSION
    assert cfg["compact"]["default_mode"] == "terse"


def test_load_config_merges_user_overrides(tx_module, tx_home, monkeypatch):
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "config.toml").write_text(
        '[defaults]\nmax_lines = 50\n\n[security]\ncommand_allowlist = ["echo", "ls"]\n'
    )
    cfg = tx_module.load_config()
    assert cfg["defaults"]["max_lines"] == 50
    # Untouched defaults should still come through:
    assert cfg["defaults"]["timeout"] == 30
    assert cfg["security"]["command_allowlist"] == ["echo", "ls"]
    # Legacy configs without [compact] pick up the new terse default.
    assert cfg["compact"]["default_mode"] == "terse"


def test_load_config_preserves_explicit_raw_compact_mode(tx_module, tx_home, monkeypatch):
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "config.toml").write_text(
        '[compact]\ndefault_mode = "raw"\n'
    )
    cfg = tx_module.load_config()
    assert cfg["compact"]["default_mode"] == "raw"


def test_load_offsets_corrupt_returns_default(tx_module, tx_home, monkeypatch):
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "offsets.json").write_text("this is not json")
    data = tx_module.load_offsets()
    assert data == {"_next_id": 1, "_panes": {}}


def test_record_run_start_sets_active_run(tx_module):
    state: dict = {}
    tx_module.record_run_start(state, "r-abc123", "echo hi", 1024, 30.0)
    active = state["active_run"]
    assert active["id"] == "r-abc123"
    assert active["cmd"] == "echo hi"
    assert active["start_offset"] == 1024
    assert "started" in active


def test_record_run_end_appends_to_runs_and_clears_active(tx_module):
    state: dict = {}
    tx_module.record_run_start(state, "r-abc123", "echo hi", 1024, 30.0)
    tx_module.record_run_end(state, "r-abc123", 0, 1100, 100)
    assert state["active_run"] is None
    assert len(state["runs"]) == 1
    entry = state["runs"][0]
    assert entry["id"] == "r-abc123"
    assert entry["exit"] == 0
    assert entry["start_offset"] == 1024
    assert entry["end_offset"] == 1100
    assert entry["cmd"] == "echo hi"


def test_record_run_end_respects_history_cap(tx_module):
    state: dict = {"runs": [{"id": f"r-{i:06x}"} for i in range(105)]}
    tx_module.record_run_start(state, "r-new001", "cmd", 0, 30.0)
    tx_module.record_run_end(state, "r-new001", 0, 100, 100)
    assert len(state["runs"]) == 100
    assert state["runs"][-1]["id"] == "r-new001"


def test_find_run_record_active_and_historical(tx_module):
    state: dict = {}
    tx_module.record_run_start(state, "r-active", "running", 0, 30.0)
    assert tx_module.find_run_record(state, "r-active")["id"] == "r-active"
    tx_module.record_run_end(state, "r-active", 0, 100, 100)
    assert tx_module.find_run_record(state, "r-active")["exit"] == 0
    assert tx_module.find_run_record(state, "r-missing") is None


def test_pane_state_dead_when_pane_missing(tx_module, monkeypatch):
    """If find_pane_anywhere returns None, status is 'dead'."""
    import tx_core.state as _tcs
    monkeypatch.setattr(_tcs, "find_pane_anywhere", lambda s, tid: None)
    state = {"tmux_id": "%999", "active_run": {"id": "r-aaa111", "start_offset": 0}}
    info = tx_module.pane_state(server=None, state=state, pane_id="p1")
    assert info["status"] == "dead"
    assert info["active_run_id"] == "r-aaa111"


def test_finalize_runs_promotes_completed_active(tx_module, tx_home, monkeypatch):
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "logs").mkdir()
    log_path = tx_home / "logs" / "p1.log"
    log_path.write_bytes(b"some output\n\x01TX_END r-finzzz 0\x01\nprompt$ ")

    offsets = {
        "_panes": {"p1": "%9"},
        "p1": {
            "tmux_id": "%9",
            "tail_offset": 0,
            "active_run": {"id": "r-finzzz", "cmd": "echo hi", "start_offset": 0,
                           "started": "2026-05-14T00:00:00Z"},
        },
    }
    finalized = tx_module.finalize_runs(offsets, "p1", 100)
    assert finalized is not None
    assert finalized["id"] == "r-finzzz"
    assert finalized["exit"] == 0
    assert offsets["p1"]["active_run"] is None
    assert offsets["p1"]["runs"][-1]["id"] == "r-finzzz"


def test_finalize_runs_fallback_records_unknown_exit(tx_module, tx_home, monkeypatch):
    """When the marker is absent but the log ends in a shell prompt and has
    been silent for idle_silence_ms, finalize_runs should record exit=None."""
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "logs").mkdir()
    log_path = tx_home / "logs" / "p1.log"
    log_path.write_bytes(b"some output without marker\nuser@host$ ")
    # Backdate the mtime so silence threshold is satisfied.
    old = os.stat(log_path).st_mtime - 5.0
    os.utime(log_path, (old, old))
    offsets = {
        "_panes": {"p1": "%9"},
        "p1": {
            "tmux_id": "%9",
            "active_run": {"id": "r-fbk0001", "cmd": "ssh remote", "start_offset": 0,
                           "started": "2026-05-14T00:00:00Z"},
        },
    }
    cfg_defaults = {
        "prompt_patterns": [r"\$\s*$"],
        "idle_silence_ms": 300,
    }
    finalized = tx_module.finalize_runs(offsets, "p1", 100, cfg_defaults)
    assert finalized is not None
    assert finalized["exit"] is None
    assert offsets["p1"]["active_run"] is None


def test_finalize_runs_fallback_disabled_without_cfg(tx_module, tx_home, monkeypatch):
    """No cfg_defaults → no fallback → run stays active."""
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "logs").mkdir()
    log_path = tx_home / "logs" / "p1.log"
    log_path.write_bytes(b"prompt$ ")
    offsets = {
        "p1": {
            "tmux_id": "%9",
            "active_run": {"id": "r-nofb01", "cmd": "x", "start_offset": 0,
                           "started": "2026-05-14T00:00:00Z"},
        },
    }
    assert tx_module.finalize_runs(offsets, "p1", 100) is None
    assert offsets["p1"]["active_run"]["id"] == "r-nofb01"


def test_pane_state_paused_when_paused_at_set(tx_module, monkeypatch):
    class _FakePane:
        pane_current_command = "zsh"
        pane_pid = "1234"
        def refresh(self): pass
        def cmd(self, *a, **kw):
            class R:
                stdout = ["0"]
            return R()
    import tx_core.state as _tcs
    monkeypatch.setattr(_tcs, "find_pane_anywhere", lambda s, tid: _FakePane())
    state = {"tmux_id": "%9", "paused_at": "2026-05-14T00:00:00Z"}
    info = tx_module.pane_state(server=None, state=state, pane_id="p1")
    assert info["status"] == "paused"


def test_pane_state_waiting_input(tx_module, tx_home, monkeypatch):
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "logs").mkdir()
    log = tx_home / "logs" / "p1.log"
    log.write_text("some context\nPassword: ")
    class _FakePane:
        pane_current_command = "zsh"
        pane_pid = "1234"
        def refresh(self): pass
        def cmd(self, *a, **kw):
            class R: stdout = ["0"]
            return R()
    import tx_core.state as _tcs
    monkeypatch.setattr(_tcs, "find_pane_anywhere", lambda s, tid: _FakePane())
    cfg_defaults = {
        "waiting_patterns": [r"(?i)password:?\s*$"],
        "prompt_patterns": [],
    }
    info = tx_module.pane_state(
        server=None, state={"tmux_id": "%9"}, pane_id="p1", cfg_defaults=cfg_defaults
    )
    assert info["status"] == "waiting-input"
    assert "password" in info["waiting_pattern"].lower()


def test_pane_state_running_fallback_to_idle_when_prompt_seen(tx_module, tx_home, monkeypatch):
    """The pane_state fallback should surface idle when a nested-shell run
    completed via prompt-pattern (no marker)."""
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "logs").mkdir()
    log = tx_home / "logs" / "p1.log"
    # Active-run start was 0; log has the command + a returned prompt.
    log.write_bytes(b"echo hi\nhi\nuser@host$ ")
    class _FakePane:
        pane_current_command = "zsh"
        pane_pid = "1234"
        def refresh(self): pass
        def cmd(self, *a, **kw):
            class R: stdout = ["0"]
            return R()
    import tx_core.state as _tcs
    monkeypatch.setattr(_tcs, "find_pane_anywhere", lambda s, tid: _FakePane())
    cfg_defaults = {"prompt_patterns": [r"\$\s*$"], "waiting_patterns": []}
    state = {
        "tmux_id": "%9",
        "active_run": {"id": "r-pcoaaa", "start_offset": 0, "cmd": "echo hi",
                       "started": "2026-05-14T00:00:00Z"},
    }
    info = tx_module.pane_state(server=None, state=state, pane_id="p1", cfg_defaults=cfg_defaults)
    assert info["status"] == "idle"


def test_finalize_runs_noop_when_marker_absent(tx_module, tx_home, monkeypatch):
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "logs").mkdir()
    log_path = tx_home / "logs" / "p1.log"
    log_path.write_bytes(b"no marker yet")
    offsets = {
        "_panes": {"p1": "%9"},
        "p1": {
            "tmux_id": "%9",
            "active_run": {"id": "r-pending", "cmd": "long", "start_offset": 0,
                           "started": "2026-05-14T00:00:00Z"},
        },
    }
    assert tx_module.finalize_runs(offsets, "p1", 100) is None
    assert offsets["p1"]["active_run"]["id"] == "r-pending"


def test_offsets_lock_round_trip(tx_module, tx_home, monkeypatch):
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    with tx_module.offsets_lock():
        # Lock file exists after enter; inside the block we own exclusive access.
        assert (tx_home / ".lock").exists()
    # After exit, the lock file remains (we never delete it) but no fd is held.
    assert (tx_home / ".lock").exists()


# ---------- pane_state remaining branches ----------

class _FakePane:
    def __init__(self, current_cmd="zsh", pid="1234", alt=False):
        self.pane_current_command = current_cmd
        self.pane_pid = pid
        self._alt = alt
    def refresh(self): pass
    def cmd(self, *a, **kw):
        class R:
            stdout = ["1" if "alternate_on" in a else "0"]
            # Make alt screen flag match self._alt at call time. tx_core.proc
            # reads via pane.cmd("display-message", "-p", "#{alternate_on}").
        # The above class scope can't see self; build via closure:
        outer_self = self
        class _R:
            stdout = ["1" if outer_self._alt else "0"]
        return _R()


def test_pane_state_running_when_marker_absent(tx_module, tx_home, monkeypatch):
    """active_run present + no marker in the log → status='running'."""
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "logs").mkdir()
    log = tx_home / "logs" / "p1.log"
    log.write_bytes(b"prelude\nstill executing\n")  # no marker
    import tx_core.state as _tcs
    monkeypatch.setattr(_tcs, "find_pane_anywhere", lambda s, tid: _FakePane())
    state = {
        "tmux_id": "%9",
        "active_run": {"id": "r-runabc", "start_offset": 0, "cmd": "x",
                       "started": "2026-05-14T00:00:00Z"},
    }
    info = tx_module.pane_state(server=None, state=state, pane_id="p1")
    assert info["status"] == "running"
    assert info["active_run_id"] == "r-runabc"


def test_pane_state_tui_when_alt_screen(tx_module, monkeypatch):
    """alt_screen=True + no active_run → status='tui'."""
    import tx_core.state as _tcs
    monkeypatch.setattr(_tcs, "find_pane_anywhere", lambda s, tid: _FakePane(alt=True))
    info = tx_module.pane_state(server=None, state={"tmux_id": "%9"}, pane_id="p1")
    assert info["status"] == "tui"
    assert info["alt_screen"] is True


def test_pane_state_idle_when_nothing_special(tx_module, monkeypatch):
    """Live pane, no active run, no alt screen, no waiting pattern → idle."""
    import tx_core.state as _tcs
    monkeypatch.setattr(_tcs, "find_pane_anywhere", lambda s, tid: _FakePane())
    info = tx_module.pane_state(server=None, state={"tmux_id": "%9"}, pane_id="p1")
    assert info["status"] == "idle"
    assert info["waiting_pattern"] is None


# ---------- pane_status (tx-pane ls back-compat tuple) ----------

def test_pane_status_dead(tx_module, monkeypatch):
    import tx_core.state as _tcs
    monkeypatch.setattr(_tcs, "find_pane_anywhere", lambda s, tid: None)
    assert tx_module.pane_status(server=None, state={"tmux_id": "%nope"},
                                 pane_id="p1") == ("exited", "-", "-")


def test_pane_status_unread_when_log_grew_past_tail_offset(tx_module, tx_home, monkeypatch):
    """Idle pane with file_size > tail_offset → 'unread'."""
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "logs").mkdir()
    (tx_home / "logs" / "p1.log").write_bytes(b"some output the agent hasn't read yet\n")
    import tx_core.state as _tcs
    monkeypatch.setattr(_tcs, "find_pane_anywhere", lambda s, tid: _FakePane())
    state = {"tmux_id": "%9", "tail_offset": 0}
    status, cmd, pid = tx_module.pane_status(server=None, state=state, pane_id="p1")
    assert status == "unread"
    assert cmd == "zsh" and pid == "1234"


def test_pane_status_idle_when_tail_caught_up(tx_module, tx_home, monkeypatch):
    """Idle pane with tail_offset == file_size → stays 'idle'."""
    patch_tx_paths(tx_module, tx_home, monkeypatch)
    (tx_home / "logs").mkdir()
    log = tx_home / "logs" / "p1.log"
    log.write_bytes(b"already seen\n")
    import tx_core.state as _tcs
    monkeypatch.setattr(_tcs, "find_pane_anywhere", lambda s, tid: _FakePane())
    state = {"tmux_id": "%9", "tail_offset": log.stat().st_size}
    status, _, _ = tx_module.pane_status(server=None, state=state, pane_id="p1")
    assert status == "idle"


# ---------- _matches_waiting helper ----------

def test_matches_waiting_no_match(tx_module):
    from tx_core.state import _matches_waiting
    import re
    patterns = [re.compile(r"Password:")]
    assert _matches_waiting("hello\nworld\n", patterns) is None

def test_matches_waiting_empty_patterns_returns_none(tx_module):
    from tx_core.state import _matches_waiting
    assert _matches_waiting("anything\nat all", []) is None

def test_matches_waiting_empty_text(tx_module):
    from tx_core.state import _matches_waiting
    import re
    assert _matches_waiting("", [re.compile(r".+")]) is None
