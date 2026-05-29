"""Unit tests for wait_for_idle (legacy idle-detection helper).

The newer wait_for_marker path is exercised heavily via subprocess
integration tests, but wait_for_idle is only used by a couple of legacy
code paths (`tx send`, `tx wait` when no marker is in flight) and slips
through the integration net.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tx_core.wait import wait_for_idle


class TestWaitForIdleSilence:
    def test_returns_true_after_silence_elapses(self, tmp_path):
        log = tmp_path / "p1.log"
        log.write_bytes(b"some output that already landed\n")
        cfg = {"idle_method": "silence", "idle_silence_ms": 200}
        # start_offset = 0 means the existing bytes count as growth, then
        # silence elapses (~200ms of no further writes).
        success, size = wait_for_idle(log, start_offset=0, cfg_defaults=cfg, timeout=2.0)
        assert success is True
        assert size == log.stat().st_size

    def test_returns_false_on_timeout_when_log_keeps_growing(self, tmp_path, monkeypatch):
        log = tmp_path / "p1.log"
        log.write_bytes(b"initial\n")
        cfg = {"idle_method": "silence", "idle_silence_ms": 5_000}
        # Each call to stat() returns a growing size — simulate a pane that
        # never goes idle within the timeout window.
        sizes = iter(range(8, 1_000_000, 13))
        original_stat = Path.stat
        def _grow_stat(self, *a, **kw):
            if self == log:
                class S:
                    st_size = next(sizes)
                    st_mtime = time.time()
                return S()
            return original_stat(self, *a, **kw)
        monkeypatch.setattr(Path, "stat", _grow_stat)
        success, _ = wait_for_idle(log, start_offset=0, cfg_defaults=cfg, timeout=0.3)
        assert success is False


class TestWaitForIdlePromptMode:
    def test_returns_true_when_prompt_pattern_matches(self, tmp_path):
        log = tmp_path / "p1.log"
        log.write_bytes(b"command echo hi\nhi\nuser@host$ ")
        cfg = {"idle_method": "prompt", "prompt_patterns": [r"\$\s*$"]}
        success, size = wait_for_idle(log, start_offset=0, cfg_defaults=cfg, timeout=1.0)
        assert success is True
        assert size == log.stat().st_size

    def test_returns_false_when_prompt_never_appears(self, tmp_path):
        log = tmp_path / "p1.log"
        log.write_bytes(b"running... still running... nope no prompt here")
        cfg = {"idle_method": "prompt", "prompt_patterns": [r"\$\s*$"]}
        success, _ = wait_for_idle(log, start_offset=0, cfg_defaults=cfg, timeout=0.3)
        assert success is False

    def test_no_prompt_patterns_means_only_timeout(self, tmp_path):
        log = tmp_path / "p1.log"
        log.write_bytes(b"anything")
        cfg = {"idle_method": "prompt", "prompt_patterns": []}
        # Without any prompt patterns the loop has no way to declare idle;
        # it will run to the timeout.
        success, _ = wait_for_idle(log, start_offset=0, cfg_defaults=cfg, timeout=0.3)
        assert success is False

    def test_missing_log_file(self, tmp_path):
        log = tmp_path / "missing.log"
        cfg = {"idle_method": "prompt", "prompt_patterns": [r"\$\s*$"]}
        success, size = wait_for_idle(log, start_offset=0, cfg_defaults=cfg, timeout=0.3)
        assert success is False
        assert size == 0
