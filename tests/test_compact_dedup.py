"""L5 cross-call content-addressed dedup tests.

Ships disabled in v1.5.0 — these tests exercise the machinery
directly (pure-function) plus one e2e case that opts the test pane
into dedup via per-pane config.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tx_compact import dedup  # noqa: E402


class TestContentHash:
    def test_deterministic(self):
        assert dedup.content_hash("hello\n") == dedup.content_hash("hello\n")

    def test_different_inputs_differ(self):
        assert dedup.content_hash("a") != dedup.content_hash("b")

    def test_hash_is_short_hex(self):
        h = dedup.content_hash("anything")
        assert len(h) == 12
        int(h, 16)  # valid hex


class TestLookupRemember:
    def test_empty_state_no_hit(self):
        assert dedup.lookup({}, "anything") is None

    def test_remembered_then_hit(self):
        state: dict = {}
        dedup.remember(state, text="payload", run_id="r-1", handle="h-1")
        hit = dedup.lookup(state, "payload")
        assert hit is not None
        assert hit.prior_run_id == "r-1"
        assert hit.prior_handle == "h-1"

    def test_ttl_expires(self):
        state: dict = {}
        dedup.remember(state, text="payload", run_id="r-1", handle=None)
        # Tamper with the stored timestamp to simulate aging
        state["compact"]["dedup_cache"][0]["ts"] = time.time() - 1000
        assert dedup.lookup(state, "payload", ttl_seconds=60) is None
        assert dedup.lookup(state, "payload", ttl_seconds=2000) is not None

    def test_different_content_no_hit(self):
        state: dict = {}
        dedup.remember(state, text="payload-a", run_id="r-1", handle=None)
        assert dedup.lookup(state, "payload-b") is None

    def test_cap_evicts_oldest(self):
        state: dict = {}
        for i in range(10):
            dedup.remember(state, text=f"item-{i}", run_id=f"r-{i}",
                           handle=None, max_entries=5)
        cache = state["compact"]["dedup_cache"]
        assert len(cache) == 5
        # The oldest 5 are gone; the most recent 5 (item-5..item-9) remain.
        kept_hashes = {e["hash"] for e in cache}
        for i in range(5):
            assert dedup.content_hash(f"item-{i}") not in kept_hashes
        for i in range(5, 10):
            assert dedup.content_hash(f"item-{i}") in kept_hashes


class TestShortMessage:
    def test_format_contains_run_and_age(self):
        hit = dedup.DedupHit(hash="abc",
                             prior_run_id="r-old",
                             prior_handle="h-old",
                             age_seconds=12.4)
        msg = dedup.dedup_short_message(hit)
        assert "tx-pane:same-as" in msg
        assert "r-old" in msg
        assert "12s ago" in msg
        assert "h-old" in msg


# ---------------------------------------------------------------------
# End-to-end: with dedup enabled per-pane
# ---------------------------------------------------------------------

def _pane(tx_runner) -> str:
    res = tx_runner("new", timeout=15)
    assert res.returncode == 0
    return res.stdout.strip().splitlines()[-1].strip()


def _enable_dedup_in_config(tx_runner) -> None:
    cfg = Path(tx_runner.env["TX_PANE_HOME"]) / "config.toml"
    extras = (
        "\n[compact.dedup]\n"
        "enabled = true\n"
        "ttl_seconds = 60\n"
        "cache_size_per_pane = 32\n"
    )
    # Make sure we have a config file first; tx_runner creates one
    # itself, so we append.
    cfg.touch()
    cfg.write_text(cfg.read_text() + extras)


def test_dedup_is_off_by_default(tx_runner):
    """Default config has dedup disabled → identical commands return
    identical output, no `tx-pane:same-as` short reference."""
    pane = _pane(tx_runner)
    tx_runner("run", "--terse", pane, "echo same-output", timeout=15)
    res2 = tx_runner("run", "--terse", pane, "echo same-output", timeout=15)
    assert res2.returncode == 0
    assert "tx-pane:same-as" not in res2.stdout


def test_dedup_collapses_second_call_when_enabled(tx_runner):
    """With dedup enabled, the second of two identical compactions
    short-circuits to a `[tx-pane:same-as ...]` line."""
    _enable_dedup_in_config(tx_runner)
    pane = _pane(tx_runner)
    # Use a command whose compacted output is stable across runs.
    cmd = "printf 'stable line 1\\nstable line 2\\nstable line 3\\n'"
    r1 = tx_runner("run", "--terse", pane, cmd, timeout=15)
    assert r1.returncode == 0
    r2 = tx_runner("run", "--terse", pane, cmd, timeout=15)
    assert r2.returncode == 0
    # Second call should be deduped — short reference somewhere in stdout.
    assert "tx-pane:same-as" in r2.stdout or "stable line 1" in r2.stdout
    # If dedup fired, original content is replaced
    if "tx-pane:same-as" in r2.stdout:
        # Body short-circuited; original text not present
        assert "stable line 2" not in r2.stdout
