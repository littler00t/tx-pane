"""Pure fixture coverage for builtin Python normalizers.

These tests intentionally avoid Docker and subprocess command capture. They
exercise representative output shapes directly through each plugin's
normalize() function.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tx_compact import NormalizeCtx, Tier
from tx_compact.builtin_plugins import dmesg, docker_ps, journalctl, lsblk


def test_docker_ps_table_keeps_operational_columns():
    text = (
        "CONTAINER ID   IMAGE          COMMAND                  CREATED       STATUS          PORTS                  NAMES\n"
        "abc123def456   nginx:latest   \"nginx -g daemon\"       2 hours ago   Up 2 hours      0.0.0.0:80->80/tcp     web\n"
        "fed456abc123   redis:7        \"docker-entrypoint\"     1 hour ago    Exited (0)      6379/tcp               cache\n"
    )
    result = docker_ps.normalize(text, NormalizeCtx(cmd="docker ps"))
    assert result.tier == Tier.FULL
    assert "CONTAINER ID\tIMAGE\tSTATUS\tPORTS\tNAMES" in result.text
    assert "abc123def456\tnginx:latest\tUp 2 hours\t0.0.0.0:80->80/tcp\tweb" in result.text
    assert "COMMAND" not in result.text
    assert "CREATED" not in result.text


def test_docker_ps_empty_and_daemon_error_paths():
    empty = docker_ps.normalize("", NormalizeCtx(cmd="docker ps"))
    assert empty.tier == Tier.FULL
    assert empty.text == "(no containers)"

    error = docker_ps.normalize(
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
        NormalizeCtx(cmd="docker ps"),
    )
    assert error.tier == Tier.DEGRADED
    assert "daemon unreachable" in error.warnings[0]


def test_lsblk_tree_removes_box_drawing_and_normalizes_spaces():
    text = (
        "NAME   MAJ:MIN RM  SIZE RO TYPE MOUNTPOINTS\n"
        "sda      8:0    0   50G  0 disk\n"
        "├─sda1   8:1    0  512M  0 part /boot\n"
        "└─sda2   8:2    0 49.5G  0 part /\n"
    )
    result = lsblk.normalize(text, NormalizeCtx(cmd="lsblk"))
    assert result.tier == Tier.FULL
    assert "sda 8:0 0 50G 0 disk" in result.text
    assert "sda1 8:1 0 512M 0 part /boot" in result.text
    assert "├" not in result.text
    assert "└" not in result.text


def test_lsblk_json_flattens_children_and_parse_errors_passthrough():
    text = (
        '{"blockdevices":[{"name":"sda","size":"50G","children":['
        '{"name":"sda1","size":"512M","fstype":"vfat","mountpoint":"/boot"},'
        '{"name":"sda2","size":"49.5G","fstype":"ext4","mountpoints":["/"]}'
        ']}]}'
    )
    result = lsblk.normalize(text, NormalizeCtx(cmd="lsblk -J"))
    assert result.tier == Tier.FULL
    assert "sda 50G" in result.text
    assert "  sda1 512M [vfat] @/boot" in result.text
    assert "  sda2 49.5G [ext4] @/" in result.text

    bad = lsblk.normalize("{not json", NormalizeCtx(cmd="lsblk --json"))
    assert bad.tier == Tier.PASSTHROUGH
    assert "failed to parse JSON" in bad.warnings[0]


def test_journalctl_empty_no_journal_and_critical_paths():
    empty = journalctl.normalize("-- No entries --\n", NormalizeCtx(cmd="journalctl -u nginx"))
    assert empty.tier == Tier.FULL
    assert empty.text == "journalctl: (no entries)"

    unavailable = journalctl.normalize(
        "No journal files were found.\n",
        NormalizeCtx(cmd="journalctl"),
    )
    assert unavailable.tier == Tier.DEGRADED
    assert "not available" in unavailable.warnings[0]

    critical_text = (
        "May 01 host app[1]: started\n"
        "May 01 host app[1]: ERROR could not bind port\n"
        "May 01 host app[1]: failed to start worker\n"
    )
    critical = journalctl.normalize(critical_text, NormalizeCtx(cmd="journalctl -u app"))
    assert critical.tier == Tier.DEGRADED
    assert critical.text == critical_text
    assert "2 critical lines" in critical.warnings[0]

    normal = journalctl.normalize("May 01 host app[1]: started\n", NormalizeCtx(cmd="journalctl"))
    assert normal.tier == Tier.FULL
    assert normal.text == "May 01 host app[1]: started\n"


def test_dmesg_long_routine_output_is_elided():
    lines = [f"[    {i}.000000] routine device message {i}" for i in range(60)]
    result = dmesg.normalize("\n".join(lines), NormalizeCtx(cmd="dmesg"))
    assert result.tier == Tier.FULL
    assert "routine dmesg lines elided" in result.text
    assert lines[0] in result.text
    assert lines[-1] in result.text
    assert lines[10] not in result.text


def test_dmesg_critical_lines_degrade_and_preserve_text():
    text = (
        "[    1.000000] usb 1-1: new high-speed USB device\n"
        "[    2.000000] kernel: BUG: unable to handle page fault\n"
        "[    3.000000] Out of memory: Killed process 123\n"
    )
    result = dmesg.normalize(text, NormalizeCtx(cmd="dmesg"))
    assert result.tier == Tier.DEGRADED
    assert result.text == text
    assert "2 critical lines" in result.warnings[0]
