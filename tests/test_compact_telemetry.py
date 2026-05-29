"""Tests for tx_compact.telemetry — JSON-lines record format,
privacy filter, rotation, and aggregation.

Pure-function unit tests. No tmux, no subprocess.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tx_compact import CompactCtx, CompactResult, Tier  # noqa: E402
from tx_compact import telemetry as _tel  # noqa: E402


@pytest.fixture
def tx_home_iso(tmp_path, monkeypatch):
    """Point telemetry at an isolated TX_PANE_HOME so tests don't pollute ~/.tx-pane."""
    home = tmp_path / "tx_home"
    home.mkdir()
    monkeypatch.setenv("TX_PANE_HOME", str(home))
    yield home


def _mk_result(in_b=1000, out_b=300, tier=Tier.FULL, layers=("L1", "L2")):
    return CompactResult(
        text="x", tier=tier, applied_layers=list(layers),
        notes=[], handle=None, footer=None,
        in_bytes=in_b, out_bytes=out_b,
    )


# ---------------------------------------------------------------------
# Privacy: cmd_head must only contain the first token
# ---------------------------------------------------------------------

class TestPrivacy:
    @pytest.mark.parametrize("cmd,expected", [
        ("smartctl -A /dev/sda", "smartctl"),
        ("ls /etc/secret", "ls"),
        ("docker ps -a --filter 'name=mysql'", "docker"),
        ("", ""),
        ("zpool status tank", "zpool"),
    ])
    def test_simple_first_token(self, cmd, expected):
        assert _tel._cmd_head(cmd) == expected

    def test_sudo_prefix_records_sudo_and_head(self):
        # We want stats to attribute to the real command, not all to "sudo".
        assert _tel._cmd_head("sudo apt update") == "sudo:apt"

    def test_env_prefix_skips_assignments(self):
        assert _tel._cmd_head("env DEBUG=1 FOO=bar python script.py") == "python"

    def test_does_not_leak_arg_path_or_value(self, tx_home_iso):
        ctx = CompactCtx(mode="terse", cmd="grep secret_token /etc/passwd",
                         pane="p1", run_id="r-abc")
        _tel.record(ctx, _mk_result())
        records = list(_tel.read_all())
        assert len(records) == 1
        rec = records[0]
        assert rec["cmd_head"] == "grep"
        # No way "secret_token" or "/etc/passwd" makes it to disk
        raw = _tel.telemetry_path().read_text()
        assert "secret_token" not in raw
        assert "/etc/passwd" not in raw

    def test_unbalanced_quotes_falls_back_to_whitespace(self):
        # shlex.split would raise on this; the privacy filter still emits a head
        assert _tel._cmd_head("echo 'unclosed") == "echo"


# ---------------------------------------------------------------------
# Record schema & file layout
# ---------------------------------------------------------------------

class TestRecord:
    def test_writes_one_jsonl_line_per_call(self, tx_home_iso):
        ctx = CompactCtx(mode="terse", cmd="df -h", pane="p1", run_id="r-1")
        _tel.record(ctx, _mk_result(in_b=2000, out_b=500))
        _tel.record(ctx, _mk_result(in_b=1500, out_b=300))
        path = _tel.telemetry_path()
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        for ln in lines:
            rec = json.loads(ln)
            assert set(rec.keys()) >= {
                "ts", "pane", "run_id", "cmd_head", "tier",
                "mode", "layers", "in_bytes", "out_bytes", "saved_pct",
            }
            assert rec["pane"] == "p1"
            assert rec["cmd_head"] == "df"
            assert rec["tier"] == 1

    def test_disabled_flag_skips_write(self, tx_home_iso):
        ctx = CompactCtx(mode="terse", cmd="df -h")
        _tel.record(ctx, _mk_result(), enabled=False)
        assert not _tel.telemetry_path().exists()

    def test_env_kill_switch_skips_write(self, tx_home_iso, monkeypatch):
        monkeypatch.setenv("TX_PANE_NO_TELEMETRY", "1")
        ctx = CompactCtx(mode="terse", cmd="df -h")
        _tel.record(ctx, _mk_result())
        assert not _tel.telemetry_path().exists()

    def test_io_failure_is_swallowed(self, tx_home_iso, monkeypatch):
        """Telemetry must never break the agent-facing call path."""
        def boom(*a, **kw):
            raise OSError("disk full")
        # Patch json.dumps inside the module so the write path raises
        monkeypatch.setattr(_tel, "json", type("J", (), {"dumps": boom}))
        ctx = CompactCtx(mode="terse", cmd="df -h")
        # Should NOT raise
        _tel.record(ctx, _mk_result())


# ---------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------

class TestRotation:
    def test_rotates_when_oversize(self, tx_home_iso):
        path = _tel.telemetry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * 2048)  # 2 KB
        ctx = CompactCtx(mode="terse", cmd="any")
        # cap at 1 MB-ish, but force using a tiny max_size_mb test path
        # The actual rotation threshold uses max_size_mb * 1024 * 1024,
        # so use a tiny value. The fn accepts max_size_mb kwarg.
        # We need to test with very small cap, so write enough bytes:
        path.write_text("y" * (1024 * 1024 + 1))  # > 1 MB
        _tel.record(ctx, _mk_result(), max_size_mb=1)
        # Backup now contains the old content; live file has just the new record
        backup = _tel.telemetry_backup_path()
        assert backup.exists()
        assert backup.read_text().startswith("y")
        # New file has one line
        live_lines = path.read_text().splitlines()
        assert len(live_lines) == 1
        json.loads(live_lines[0])  # valid JSON

    def test_no_rotation_below_cap(self, tx_home_iso):
        path = _tel.telemetry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ctx = CompactCtx(mode="terse", cmd="any")
        _tel.record(ctx, _mk_result(), max_size_mb=100)
        # First write — backup must not exist
        assert not _tel.telemetry_backup_path().exists()


# ---------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------

class TestAggregate:
    def test_empty_records(self):
        agg = _tel.aggregate(iter([]))
        assert agg["count"] == 0
        assert agg["by_cmd_head"] == {}
        assert agg["passthrough_cmd_heads"] == []

    def test_per_head_grouping(self):
        records = iter([
            {"ts": "2026-05-14T01:00:00Z", "cmd_head": "df",
             "tier": 1, "in_bytes": 1000, "out_bytes": 200},
            {"ts": "2026-05-14T02:00:00Z", "cmd_head": "df",
             "tier": 1, "in_bytes": 800, "out_bytes": 200},
            {"ts": "2026-05-14T03:00:00Z", "cmd_head": "ps",
             "tier": 1, "in_bytes": 2000, "out_bytes": 1900},
            {"ts": "2026-05-14T04:00:00Z", "cmd_head": "apt",
             "tier": 3, "in_bytes": 500, "out_bytes": 500},
        ])
        agg = _tel.aggregate(records)
        assert agg["count"] == 4
        assert "df" in agg["by_cmd_head"]
        assert agg["by_cmd_head"]["df"]["count"] == 2
        assert agg["by_cmd_head"]["df"]["in"] == 1800
        assert agg["by_cmd_head"]["df"]["saved_pct"] > 70
        # ps shows up but barely saves
        assert agg["by_cmd_head"]["ps"]["saved_pct"] < 10
        # apt hit tier 3
        assert ("apt", 1) in agg["passthrough_cmd_heads"]

    def test_since_filter(self):
        records = [
            {"ts": "2026-05-01T01:00:00Z", "cmd_head": "df",
             "tier": 1, "in_bytes": 1000, "out_bytes": 500},
            {"ts": "2026-05-14T01:00:00Z", "cmd_head": "df",
             "tier": 1, "in_bytes": 1000, "out_bytes": 500},
        ]
        agg = _tel.aggregate(iter(records), since_ts="2026-05-10T00:00:00Z")
        assert agg["count"] == 1


# ---------------------------------------------------------------------
# Wipe
# ---------------------------------------------------------------------

class TestWipe:
    def test_wipe_removes_both_files(self, tx_home_iso):
        _tel.telemetry_path().parent.mkdir(parents=True, exist_ok=True)
        _tel.telemetry_path().write_text("{}\n")
        _tel.telemetry_backup_path().write_text("{}\n")
        n = _tel.wipe()
        assert n == 2
        assert not _tel.telemetry_path().exists()
        assert not _tel.telemetry_backup_path().exists()

    def test_wipe_idempotent(self, tx_home_iso):
        n = _tel.wipe()
        assert n == 0
