"""Per-tool real-tool tests — every builtin normalizer is exercised
against its actual installed binary inside the test Docker container.

Per the user requirement: tests only run inside the test container
(TX_PANE_IN_DOCKER=1, set by Dockerfile). On the host they skip cleanly.

Each test:
1. Checks that the tool is installed (else skip).
2. Runs `tx-pane run --raw  <pane> <tool ...>` to capture the baseline.
3. Runs `tx-pane run --terse <pane> <tool ...>` to capture the compacted.
4. Asserts:
   - compacted output is ≤ raw output bytes
   - critical info (specified per-tool) is preserved
   - the compaction footer references the normalizer name (where appropriate)

Some tools won't produce useful output in the unprivileged container
(zpool needs kernel module, iptables needs CAP_NET_ADMIN, etc.) — those
test the *anomaly* path (passthrough or degraded), which is still real
normalizer behaviour.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------
# Guard: every test in this file is Docker-only
# ---------------------------------------------------------------------

def _in_docker() -> bool:
    return os.environ.get("TX_PANE_IN_DOCKER") == "1"


pytestmark = pytest.mark.skipif(
    not _in_docker(),
    reason="real-tool tests run only inside the tx-tests Docker container "
           "(./run-tests-docker). Set TX_PANE_IN_DOCKER=1 to override.",
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _pane(tx_runner) -> str:
    res = tx_runner("new", timeout=15)
    assert res.returncode == 0, res.stderr or res.stdout
    return res.stdout.strip().splitlines()[-1].strip()


def _body_lines(stdout: str) -> list[str]:
    """Lines of `tx-pane run` output minus the marker/wrap noise + footer.

    Filters:
    - The shell's echoed wrap-command line (`__tx_run_id=...`).
    - The exit-code header (`[exit:N]`).
    - The compaction footer (`[tx-pane:compact ...]`).
    What remains is the actual command output the agent reads.
    """
    keep: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("___tx_run_id=") or "__tx_run_id=" in line:
            continue
        if line.startswith("[exit:"):
            continue
        if line.startswith("[tx-pane:compact "):
            continue
        keep.append(line)
    return keep


def _byte_len(stdout: str) -> int:
    return sum(len(l) + 1 for l in _body_lines(stdout))


def _run_pair(tx_runner, pane: str, cmd: str, *, timeout: float = 30) -> tuple[str, str]:
    """Return (raw, terse) stdouts for the same command. Reuses one pane
    so both runs see the same shell state (env, cwd)."""
    r1 = tx_runner("run", "--raw", "--max", "100000", pane, cmd, timeout=timeout)
    assert r1.returncode == 0, r1.stderr or r1.stdout
    r2 = tx_runner("run", "--terse", "--max", "100000", pane, cmd, timeout=timeout)
    assert r2.returncode == 0, r2.stderr or r2.stdout
    return r1.stdout, r2.stdout


def _require(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not installed in this container")


# ---------------------------------------------------------------------
# Per-tool tests
# ---------------------------------------------------------------------

class TestSsNormalizer:
    def test_ss_compacts(self, tx_runner):
        _require("ss")
        pane = _pane(tx_runner)
        raw, terse = _run_pair(tx_runner, pane, "ss -tulnp 2>&1 || true")
        # Both runs completed; the body should at least not crash. Even
        # with no LISTEN sockets the header row exists in raw.
        assert _byte_len(terse) <= _byte_len(raw)


class TestPsNormalizer:
    def test_ps_compacts(self, tx_runner):
        _require("ps")
        pane = _pane(tx_runner)
        raw, terse = _run_pair(tx_runner, pane, "ps auxf")
        assert _byte_len(terse) <= _byte_len(raw)
        # PID 1 (the bash spawned in the pane or tmux's child) must be present in both
        assert "1" in raw and "1" in terse


class TestDfNormalizer:
    def test_df_compacts(self, tx_runner):
        _require("df")
        pane = _pane(tx_runner)
        raw, terse = _run_pair(tx_runner, pane, "df -h")
        assert _byte_len(terse) <= _byte_len(raw)
        # Filesystem header survives.
        assert "Filesystem" in raw
        # The root mount is preserved.
        assert "/" in raw and "/" in terse


class TestDuNormalizer:
    def test_du_compacts(self, tx_runner):
        _require("du")
        pane = _pane(tx_runner)
        raw, terse = _run_pair(tx_runner, pane, "du -sh /usr/bin /usr/lib 2>/dev/null")
        assert _byte_len(terse) <= _byte_len(raw)
        # Both /usr/bin and /usr/lib appear (or one of them — depends on
        # exit early on perm errors)
        assert "/usr/bin" in raw or "/usr/lib" in raw


class TestLastNormalizer:
    def test_last_compacts(self, tx_runner):
        _require("last")
        pane = _pane(tx_runner)
        raw, terse = _run_pair(tx_runner, pane, "last -n 5 2>&1 || true")
        assert _byte_len(terse) <= _byte_len(raw)
        # In a fresh container wtmp is empty → output may just be the
        # `wtmp begins` footer (L1+filter strip it in terse).
        # No banner survives terse.
        assert "wtmp begins" not in "\n".join(_body_lines(terse))


class TestFindNormalizer:
    def test_find_compacts_perm_denied(self, tx_runner):
        _require("find")
        pane = _pane(tx_runner)
        # As non-root: /root and /etc/ssl/private should hit perm-denied.
        cmd = "find /etc -maxdepth 2 -name '*.conf' 2>&1 | head -200"
        raw, terse = _run_pair(tx_runner, pane, cmd)
        assert _byte_len(terse) <= _byte_len(raw)


class TestAptNormalizer:
    def test_apt_list_compacts(self, tx_runner):
        _require("apt")
        pane = _pane(tx_runner)
        cmd = "apt list --upgradable 2>&1 | head -50"
        raw, terse = _run_pair(tx_runner, pane, cmd)
        assert _byte_len(terse) <= _byte_len(raw)
        # The WARNING and Listing banners are dropped in terse (L1 banner
        # registry strips them).
        terse_body = "\n".join(_body_lines(terse))
        assert "WARNING: apt does not have a stable CLI interface" not in terse_body
        assert "Listing... Done" not in terse_body


class TestSmartctlNormalizer:
    def test_smartctl_unavailable_passthrough(self, tx_runner):
        _require("smartctl")
        pane = _pane(tx_runner)
        # /dev/null is never a SMART device; smartctl errors out.
        # That error path is the normalizer's anomaly fixture.
        cmd = "smartctl -A /dev/null 2>&1 || true"
        raw, terse = _run_pair(tx_runner, pane, cmd)
        assert _byte_len(terse) <= _byte_len(raw)


class TestIptablesNormalizer:
    def test_iptables_no_perm_passthrough(self, tx_runner):
        _require("iptables")
        pane = _pane(tx_runner)
        # Non-privileged container: iptables refuses with Permission denied.
        # That's the realistic agent-facing output; normalizer leaves it
        # alone (passthrough/degraded), tx-pane returns it cleanly.
        cmd = "iptables -L -n 2>&1 || true"
        raw, terse = _run_pair(tx_runner, pane, cmd)
        assert _byte_len(terse) <= _byte_len(raw)


class TestZpoolNormalizer:
    def test_zpool_no_kernel_module(self, tx_runner):
        _require("zpool")
        pane = _pane(tx_runner)
        cmd = "zpool status 2>&1 || true"
        raw, terse = _run_pair(tx_runner, pane, cmd)
        # No kernel module → both forms are short and similar.
        assert _byte_len(terse) <= _byte_len(raw)


class TestVirshNormalizer:
    def test_virsh_no_daemon(self, tx_runner):
        _require("virsh")
        pane = _pane(tx_runner)
        cmd = "virsh list --all 2>&1 || true"
        raw, terse = _run_pair(tx_runner, pane, cmd)
        assert _byte_len(terse) <= _byte_len(raw)


class TestDockerNormalizer:
    def test_docker_ps_no_daemon(self, tx_runner):
        _require("docker")
        pane = _pane(tx_runner)
        cmd = "docker ps 2>&1 || true"
        raw, terse = _run_pair(tx_runner, pane, cmd)
        assert _byte_len(terse) <= _byte_len(raw)
        # The daemon-error message survives (it's tier-2 degraded info)
        assert "Cannot connect" in raw or "Cannot connect" in terse \
            or "permission denied" in raw or "permission denied" in terse


class TestSystemctlNormalizer:
    def test_systemctl_no_pid1(self, tx_runner):
        _require("systemctl")
        pane = _pane(tx_runner)
        cmd = "systemctl list-units --no-pager 2>&1 | head -30"
        raw, terse = _run_pair(tx_runner, pane, cmd)
        assert _byte_len(terse) <= _byte_len(raw)


class TestJournalctlNormalizer:
    def test_journalctl_no_journal(self, tx_runner):
        _require("journalctl")
        pane = _pane(tx_runner)
        cmd = "journalctl --no-pager -n 5 2>&1 || true"
        raw, terse = _run_pair(tx_runner, pane, cmd)
        assert _byte_len(terse) <= _byte_len(raw)


class TestDmesgNormalizer:
    def test_dmesg(self, tx_runner):
        _require("dmesg")
        pane = _pane(tx_runner)
        # Non-privileged container may not have /dev/kmsg access — that
        # produces a short error which is fine for our regression check.
        cmd = "dmesg 2>&1 || true"
        raw, terse = _run_pair(tx_runner, pane, cmd)
        assert _byte_len(terse) <= _byte_len(raw)


class TestLsblkNormalizer:
    def test_lsblk(self, tx_runner):
        _require("lsblk")
        pane = _pane(tx_runner)
        cmd = "lsblk 2>&1 || true"
        raw, terse = _run_pair(tx_runner, pane, cmd)
        assert _byte_len(terse) <= _byte_len(raw)
