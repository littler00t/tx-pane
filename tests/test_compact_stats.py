"""End-to-end tests for `tx-pane compact-stats`.

Drives the actual click command via the existing tx_runner fixture and
asserts on its plain-text and --json output. Verifies the empty-state
banner, --weak filter, --passthrough banner, and --forget wipe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _tx_home(tx_runner) -> Path:
    """The TX_PANE_HOME the runner is configured against."""
    return Path(tx_runner.env["TX_PANE_HOME"])


def _seed_telemetry(tx_runner, records: list[dict]) -> None:
    """Write a known set of telemetry records to the test's TX_PANE_HOME."""
    path = _tx_home(tx_runner) / "compact.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def test_empty_state_message(tx_runner):
    res = tx_runner("compact-stats", timeout=15)
    assert res.returncode == 0
    assert "empty" in res.stdout.lower()


def test_summary_with_seeded_records(tx_runner):
    _seed_telemetry(tx_runner, [
        {"ts": "2026-05-14T01:00:00Z", "pane": "p1", "run_id": "r-1",
         "cmd_head": "df", "tier": 1, "mode": "terse",
         "layers": ["L1", "L2"], "in_bytes": 1000, "out_bytes": 200,
         "saved_pct": 80.0},
        {"ts": "2026-05-14T01:01:00Z", "pane": "p1", "run_id": "r-2",
         "cmd_head": "apt", "tier": 3, "mode": "terse",
         "layers": [], "in_bytes": 500, "out_bytes": 500,
         "saved_pct": 0.0},
    ])
    res = tx_runner("compact-stats", timeout=15)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "2 calls" in res.stdout
    assert "df" in res.stdout
    assert "apt" in res.stdout


def test_json_output(tx_runner):
    _seed_telemetry(tx_runner, [
        {"ts": "2026-05-14T01:00:00Z", "cmd_head": "df",
         "tier": 1, "in_bytes": 1000, "out_bytes": 200, "saved_pct": 80.0},
    ])
    res = tx_runner("compact-stats", "--json", timeout=15)
    assert res.returncode == 0
    payload = json.loads(res.stdout)
    assert payload["count"] == 1
    assert "df" in payload["by_cmd_head"]


def test_weak_filter(tx_runner):
    _seed_telemetry(tx_runner, [
        # df saves 80% — not weak
        {"ts": "2026-05-14T01:00:00Z", "cmd_head": "df",
         "tier": 1, "in_bytes": 1000, "out_bytes": 200, "saved_pct": 80.0},
        # ps saves 5% — weak
        {"ts": "2026-05-14T01:01:00Z", "cmd_head": "ps",
         "tier": 1, "in_bytes": 1000, "out_bytes": 950, "saved_pct": 5.0},
    ])
    res = tx_runner("compact-stats", "--weak", timeout=15)
    assert res.returncode == 0
    assert "ps" in res.stdout
    # df is not weak — the per-head table should not include it
    # (but the header line mentions all-calls total which does include df)
    body = res.stdout.split("# cmd_head", 1)[1] if "# cmd_head" in res.stdout else ""
    assert "df " not in body


def test_passthrough_filter(tx_runner):
    _seed_telemetry(tx_runner, [
        {"ts": "2026-05-14T01:00:00Z", "cmd_head": "apt",
         "tier": 3, "in_bytes": 500, "out_bytes": 500, "saved_pct": 0.0},
        {"ts": "2026-05-14T01:01:00Z", "cmd_head": "df",
         "tier": 1, "in_bytes": 1000, "out_bytes": 200, "saved_pct": 80.0},
    ])
    res = tx_runner("compact-stats", "--passthrough", timeout=15)
    assert res.returncode == 0
    assert "tier-3 passthrough" in res.stdout
    assert "apt" in res.stdout


def test_forget_wipes_records(tx_runner):
    _seed_telemetry(tx_runner, [
        {"ts": "2026-05-14T01:00:00Z", "cmd_head": "df",
         "tier": 1, "in_bytes": 1000, "out_bytes": 200, "saved_pct": 80.0},
    ])
    res = tx_runner("compact-stats", "--forget", timeout=15)
    assert res.returncode == 0
    assert "removed" in res.stdout
    # File should be gone
    assert not (_tx_home(tx_runner) / "compact.jsonl").exists()
    # Subsequent compact-stats says empty
    res2 = tx_runner("compact-stats", timeout=15)
    assert "empty" in res2.stdout.lower()


def test_since_filter(tx_runner):
    _seed_telemetry(tx_runner, [
        {"ts": "2026-04-01T00:00:00Z", "cmd_head": "old",
         "tier": 1, "in_bytes": 100, "out_bytes": 50, "saved_pct": 50.0},
        {"ts": "2026-05-14T00:00:00Z", "cmd_head": "fresh",
         "tier": 1, "in_bytes": 100, "out_bytes": 30, "saved_pct": 70.0},
    ])
    res = tx_runner("compact-stats", "--since", "2026-05-01T00:00:00Z",
                    "--json", timeout=15)
    assert res.returncode == 0
    payload = json.loads(res.stdout)
    assert payload["count"] == 1
    assert "fresh" in payload["by_cmd_head"]
    assert "old" not in payload["by_cmd_head"]


def test_telemetry_emitted_after_terse_run(tx_runner):
    """After running `tx-pane run --terse`, compact-stats should show the record."""
    # Create a pane and run a terse command that hits L1+L2
    res = tx_runner("new", timeout=15)
    assert res.returncode == 0
    pane = res.stdout.strip().splitlines()[-1].strip()
    tx_runner("run", "--terse", pane,
              "printf 'Reading package lists... Done\\nfoo\\n'", timeout=15)
    # Now compact-stats should have at least one record
    res = tx_runner("compact-stats", "--json", timeout=15)
    assert res.returncode == 0
    payload = json.loads(res.stdout)
    assert payload["count"] >= 1
    # cmd_head for `printf 'Reading...'` is `printf`
    heads = list(payload["by_cmd_head"].keys())
    assert "printf" in heads, f"got heads: {heads}"
