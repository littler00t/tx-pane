"""Tests for tail/dump pending-line logic without needing a live tmux pane.

These exercise the cmd_tail and cmd_dump code paths by manually seeding
offsets.json and the log file, then running the CLI as a subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

TX_SCRIPT = Path(__file__).resolve().parent.parent / "tx"


def _run_tx(env, *args, timeout=30.0):
    return subprocess.run(
        [str(TX_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _seed_pane(home: Path, pane_id: str, content: str, tail_offset: int = 0, pending=None):
    logs = home / "logs"
    logs.mkdir(exist_ok=True)
    (logs / f"{pane_id}.log").write_text(content)
    offsets = {
        "_next_id": 2,
        "_panes": {pane_id: "%9999"},
        pane_id: {
            "tmux_id": "%9999",
            "tail_offset": tail_offset,
            "continue_offset": None,
            "status": "idle",
        },
    }
    if pending is not None:
        offsets[pane_id]["pending_lines"] = pending
    (home / "offsets.json").write_text(json.dumps(offsets))


@pytest.fixture
def env_and_home(tmp_path):
    home = tmp_path / "tx_home"
    home.mkdir()
    env = os.environ.copy()
    env["TX_HOME"] = str(home)
    return env, home


def test_tail_returns_new_lines(env_and_home):
    env, home = env_and_home
    content = "line1\nline2\nline3\n"
    _seed_pane(home, "p1", content)
    res = _run_tx(env, "tail", "p1")
    assert res.returncode == 0
    assert res.stdout.strip().splitlines() == ["line1", "line2", "line3"]
    # tail_offset should advance to file size.
    state = json.loads((home / "offsets.json").read_text())
    assert state["p1"]["tail_offset"] == len(content)
    assert "pending_lines" not in state["p1"]


def test_tail_default_compacts_and_raw_preserves_banner(env_and_home):
    env, home = env_and_home
    content = "Reading package lists... Done\nreal content\n"
    _seed_pane(home, "p1", content)
    compacted = _run_tx(env, "tail", "p1")
    assert compacted.returncode == 0
    assert "real content" in compacted.stdout
    assert "Reading package lists... Done" not in compacted.stdout

    _seed_pane(home, "p2", content)
    raw = _run_tx(env, "tail", "--raw", "p2")
    assert raw.returncode == 0
    assert "Reading package lists... Done" in raw.stdout


def test_tail_truncates_and_stores_pending(env_and_home):
    env, home = env_and_home
    content = "\n".join(f"line{i}" for i in range(20)) + "\n"
    _seed_pane(home, "p1", content)
    res = _run_tx(env, "tail", "--raw", "p1", "--max", "5")
    assert res.returncode == 0
    lines = res.stdout.strip().splitlines()
    assert lines[:5] == [f"line{i}" for i in range(5)]
    assert any("truncated:" in l and "15 lines remain" in l for l in lines)
    state = json.loads((home / "offsets.json").read_text())
    assert state["p1"]["pending_lines"] == [f"line{i}" for i in range(5, 20)]
    assert state["p1"]["tail_offset"] == len(content)


def test_tail_continue_consumes_pending(env_and_home):
    env, home = env_and_home
    pending = [f"r{i}" for i in range(12)]
    _seed_pane(home, "p1", "ignored\n", tail_offset=99, pending=pending)
    res = _run_tx(env, "tail", "p1", "--max", "5", "--continue")
    assert res.returncode == 0
    lines = res.stdout.strip().splitlines()
    assert lines[:5] == [f"r{i}" for i in range(5)]
    assert any("7 lines remain" in l for l in lines)
    state = json.loads((home / "offsets.json").read_text())
    assert state["p1"]["pending_lines"] == [f"r{i}" for i in range(5, 12)]


def test_tail_continue_until_end(env_and_home):
    env, home = env_and_home
    pending = [f"r{i}" for i in range(3)]
    _seed_pane(home, "p1", "x\n", tail_offset=2, pending=pending)
    res = _run_tx(env, "tail", "p1", "--max", "5", "--continue")
    assert res.returncode == 0
    lines = res.stdout.strip().splitlines()
    assert lines[:3] == ["r0", "r1", "r2"]
    assert lines[-1] == "[end of output]"
    state = json.loads((home / "offsets.json").read_text())
    assert "pending_lines" not in state["p1"]


def test_tail_no_continue_with_pending_reshows_chunk(env_and_home):
    env, home = env_and_home
    pending = [f"r{i}" for i in range(8)]
    _seed_pane(home, "p1", "x\n", tail_offset=2, pending=pending)
    res = _run_tx(env, "tail", "p1", "--max", "5")
    assert res.returncode == 0
    lines = res.stdout.strip().splitlines()
    assert lines[:5] == [f"r{i}" for i in range(5)]
    assert any("3 lines remain" in l for l in lines)
    # State should NOT have been advanced (no --continue).
    state = json.loads((home / "offsets.json").read_text())
    assert state["p1"]["pending_lines"] == pending


def test_dump_does_not_affect_tail_offset(env_and_home):
    env, home = env_and_home
    content = "alpha\nbeta\n"
    _seed_pane(home, "p1", content, tail_offset=0)
    res = _run_tx(env, "dump", "p1")
    assert res.returncode == 0
    assert "alpha" in res.stdout
    assert "beta" in res.stdout
    state = json.loads((home / "offsets.json").read_text())
    assert state["p1"]["tail_offset"] == 0  # unchanged


def test_dump_truncation_message(env_and_home):
    env, home = env_and_home
    content = "\n".join(f"line{i}" for i in range(50)) + "\n"
    _seed_pane(home, "p1", content)
    res = _run_tx(env, "dump", "--raw", "p1", "--max", "10")
    assert res.returncode == 0
    assert "[truncated:" in res.stdout
    assert "tx dump p1 --continue" in res.stdout


def test_dump_tail_returns_last_n(env_and_home):
    env, home = env_and_home
    content = "\n".join(f"line{i}" for i in range(50)) + "\n"
    _seed_pane(home, "p1", content, tail_offset=5)
    res = _run_tx(env, "dump", "--raw", "p1", "--tail", "5")
    assert res.returncode == 0
    lines = [l for l in res.stdout.strip().splitlines() if l.startswith("line")]
    assert lines == ["line45", "line46", "line47", "line48", "line49"]
    # tail_offset must not be mutated.
    state = json.loads((home / "offsets.json").read_text())
    assert state["p1"]["tail_offset"] == 5


def test_dump_continue_consumes_dump_pending(env_and_home):
    env, home = env_and_home
    content = "\n".join(f"line{i}" for i in range(30)) + "\n"
    _seed_pane(home, "p1", content)
    res = _run_tx(env, "dump", "--raw", "p1", "--max", "10")
    assert res.returncode == 0
    assert "[truncated:" in res.stdout
    res2 = _run_tx(env, "dump", "p1", "--max", "10", "--continue")
    assert res2.returncode == 0
    assert "line10" in res2.stdout
    res3 = _run_tx(env, "dump", "p1", "--max", "10", "--continue")
    assert res3.returncode == 0
    assert "[end of output]" in res3.stdout
    # Tail offset still untouched throughout.
    state = json.loads((home / "offsets.json").read_text())
    assert state["p1"]["tail_offset"] == 0


def test_dump_continue_without_pending_errors(env_and_home):
    env, home = env_and_home
    _seed_pane(home, "p1", "short\n")
    res = _run_tx(env, "dump", "p1", "--continue")
    assert res.returncode == 1
    assert "no dump truncation" in res.stdout


def test_dump_and_tail_pending_are_independent(env_and_home):
    """dump --continue must not advance tail_offset or consume tail's pending_lines."""
    env, home = env_and_home
    content = "\n".join(f"x{i}" for i in range(40)) + "\n"
    _seed_pane(home, "p1", content)
    # Truncate via tail to populate pending_lines.
    res = _run_tx(env, "tail", "--raw", "p1", "--max", "5")
    assert res.returncode == 0
    # And via dump to populate dump_pending_lines.
    res2 = _run_tx(env, "dump", "--raw", "p1", "--max", "5")
    assert res2.returncode == 0
    state = json.loads((home / "offsets.json").read_text())
    assert "pending_lines" in state["p1"]
    assert "dump_pending_lines" in state["p1"]
    # dump --continue should not touch pending_lines.
    _run_tx(env, "dump", "p1", "--max", "5", "--continue")
    state2 = json.loads((home / "offsets.json").read_text())
    assert state2["p1"]["pending_lines"] == state["p1"]["pending_lines"]


def test_reset_clears_pending_and_advances_offset(env_and_home):
    env, home = env_and_home
    pending = ["x", "y", "z"]
    content = "some logs\n"
    _seed_pane(home, "p1", content, tail_offset=0, pending=pending)
    res = _run_tx(env, "reset", "p1")
    assert res.returncode == 0
    assert "[reset:" in res.stdout
    state = json.loads((home / "offsets.json").read_text())
    assert state["p1"]["tail_offset"] == len(content)
    assert "pending_lines" not in state["p1"]


def test_tail_no_strip_preserves_blank_runs(env_and_home):
    env, home = env_and_home
    content = "a\n\n\n\n\nb\n"
    _seed_pane(home, "p1", content)
    res = _run_tx(env, "tail", "--raw", "p1", "--no-strip")
    assert res.returncode == 0
    assert res.stdout.count("\n") >= 4  # blanks preserved


def test_tail_strips_ansi(env_and_home):
    env, home = env_and_home
    content = "\x1b[31mred\x1b[0m text\n"
    _seed_pane(home, "p1", content)
    res = _run_tx(env, "tail", "p1")
    assert "red text" in res.stdout
    assert "\x1b" not in res.stdout


def test_tail_rejects_stale_offset_update(monkeypatch, tmp_path):
    import tx_core.commands.read as read_mod

    log_path = tmp_path / "p1.log"
    log_path.write_text("alpha\n")
    base = {"p1": {"tmux_id": "%1", "tail_offset": 0, "continue_offset": None}}
    stale = {"p1": {"tmux_id": "%1", "tail_offset": 2, "continue_offset": None}}
    loads = [base, base, stale]
    saves = []

    class DummyLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(read_mod, "load_config", lambda: read_mod.DEFAULT_CONFIG)
    monkeypatch.setattr(read_mod, "offsets_lock", lambda: DummyLock())
    monkeypatch.setattr(read_mod, "load_offsets", lambda: loads.pop(0))
    monkeypatch.setattr(read_mod, "save_offsets", lambda offsets: saves.append(offsets))
    monkeypatch.setattr(read_mod, "pane_log_path", lambda _pane: log_path)

    res = CliRunner().invoke(read_mod.cmd_tail, ["p1"])
    assert res.exit_code == 1
    assert "offsets changed while reading" in res.output
    assert saves == []


def test_dump_continue_rejects_stale_pending_update(monkeypatch, tmp_path):
    import tx_core.commands.read as read_mod

    log_path = tmp_path / "p1.log"
    log_path.write_text("alpha\n")
    base = {"p1": {"tmux_id": "%1", "tail_offset": 0, "continue_offset": None, "dump_pending_lines": ["a", "b"]}}
    stale = {"p1": {"tmux_id": "%1", "tail_offset": 0, "continue_offset": None, "dump_pending_lines": ["other"]}}
    loads = [base, base, stale]
    saves = []

    class DummyLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(read_mod, "load_config", lambda: read_mod.DEFAULT_CONFIG)
    monkeypatch.setattr(read_mod, "offsets_lock", lambda: DummyLock())
    monkeypatch.setattr(read_mod, "load_offsets", lambda: loads.pop(0))
    monkeypatch.setattr(read_mod, "save_offsets", lambda offsets: saves.append(offsets))
    monkeypatch.setattr(read_mod, "pane_log_path", lambda _pane: log_path)

    res = CliRunner().invoke(read_mod.cmd_dump, ["p1", "--continue", "--max", "1"])
    assert res.exit_code == 1
    assert "offsets changed while reading" in res.output
    assert saves == []


def test_wait_rejects_stale_offset_update(monkeypatch, tmp_path):
    import tx_core.commands.read as read_mod

    log_path = tmp_path / "p1.log"
    log_path.write_text("needle\n")
    base = {"p1": {"tmux_id": "%1", "tail_offset": 0, "continue_offset": None}}
    stale = {"p1": {"tmux_id": "%1", "tail_offset": 3, "continue_offset": None}}
    loads = [base, stale]
    saves = []

    class DummyLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(read_mod, "load_config", lambda: read_mod.DEFAULT_CONFIG)
    monkeypatch.setattr(read_mod, "offsets_lock", lambda: DummyLock())
    monkeypatch.setattr(read_mod, "load_offsets", lambda: loads.pop(0))
    monkeypatch.setattr(read_mod, "save_offsets", lambda offsets: saves.append(offsets))
    monkeypatch.setattr(read_mod, "pane_log_path", lambda _pane: log_path)

    res = CliRunner().invoke(read_mod.cmd_wait, ["p1", "needle", "--timeout", "1"])
    assert res.exit_code == 1
    assert "offsets changed while reading" in res.output
    assert saves == []
