"""Contract tests keeping docs/tx-doc-reference.md aligned with CLI help."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


TX_SCRIPT = Path(__file__).resolve().parent.parent / "tx"
REFERENCE_DOC = Path(__file__).resolve().parent.parent / "docs" / "tx-doc-reference.md"


DOCUMENTED_COMMAND_FLAGS = {
    "run": {
        "--max", "--timeout", "--no-strip", "--queue", "--max-wait", "--stdin",
        "--no-enter", "--kill-and-run", "--on-timeout", "--keep-ansi", "--json",
        "--yes", "--wait-for", "--fail-for", "--no-normalize",
        "--no-collapse-repeats", "--no-strip-banners", "--token-budget",
        "--terse", "--raw",
    },
    "exec": {
        "--timeout", "--queue", "--max-wait", "--kill-and-run", "--json", "--yes",
    },
    "wait-run": {
        "--timeout", "--max", "--no-strip", "--on-timeout", "--keep-ansi",
        "--json", "--no-normalize", "--no-collapse-repeats", "--no-strip-banners",
        "--token-budget", "--terse", "--raw",
    },
    "tail": {
        "--max", "--continue", "--all", "--from", "--no-strip", "--keep-ansi",
        "--timestamps", "--no-normalize", "--no-collapse-repeats",
        "--no-strip-banners", "--token-budget", "--terse", "--raw",
    },
    "dump": {
        "--max", "--tail", "--head", "--from", "--continue", "--no-strip",
        "--keep-ansi", "--timestamps", "--no-normalize", "--no-collapse-repeats",
        "--no-strip-banners", "--token-budget", "--terse", "--raw",
    },
    "wait": {
        "--timeout", "--max", "--no-strip", "--no-normalize",
        "--no-collapse-repeats", "--no-strip-banners", "--token-budget",
        "--terse", "--raw",
    },
    "log": {
        "--max", "--tail", "--head", "--since-run", "--no-strip", "--keep-ansi",
        "--no-normalize", "--no-collapse-repeats", "--no-strip-banners",
        "--token-budget", "--terse", "--raw",
    },
    "grep": {
        "-A", "-B", "-C", "--max", "--no-strip", "--keep-ansi",
        "--no-normalize", "--no-collapse-repeats", "--no-strip-banners",
        "--token-budget", "--terse", "--raw",
    },
    "output": {
        "--max", "--last", "--since-run", "--no-strip", "--keep-ansi", "--json",
        "--handle", "--range", "--grep", "--grep-context", "--full",
        "--no-normalize", "--no-collapse-repeats", "--no-strip-banners",
        "--token-budget", "--terse", "--raw",
    },
    "stream": {
        "--duration", "--lines", "--until", "--timeout", "--max", "--no-strip",
        "--keep-ansi", "--yes", "--no-normalize", "--no-collapse-repeats",
        "--no-strip-banners", "--token-budget", "--terse", "--raw",
    },
}


def _help_flags(command: str, env: dict[str, str]) -> set[str]:
    res = subprocess.run(
        [str(TX_SCRIPT), command, "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    flags: set[str] = set()
    for line in res.stdout.splitlines():
        match = re.match(r"^  ((?:--[a-z0-9-]+)|(?:-[ABC]))\b", line)
        if match:
            flags.add(match.group(1))
    flags.discard("--help")
    return flags


def test_reference_command_flags_match_help(tmp_path):
    env = os.environ.copy()
    env["TX_HOME"] = str(tmp_path / "tx_home")
    doc = REFERENCE_DOC.read_text()

    assert "--ignore-case" not in doc

    for command, expected in DOCUMENTED_COMMAND_FLAGS.items():
        assert _help_flags(command, env) == expected
        for flag in expected:
            assert flag in doc, f"{flag} missing from reference doc for {command}"
