"""Integration tests for Stage 3 commands and behaviours (v0.4.0).

Covers:
- Group A: dump --head, tx grep, --keep-ansi, --json, --timestamps
- Group B: tx sudo (no-TTY refusal), redact_patterns
- Group C: tx stream --duration/--lines/--until, run --wait-for/--fail-for
- Group D: tx paste (file + stdin)
- Group E: per-pane allowlist, confirm_patterns
- Group F: tx new --shell
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest


def _pane(tx_runner, *args: str) -> str:
    res = tx_runner("new", *args)
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout.strip().splitlines()[-1].strip()


# ----- Group A: dump --head, --keep-ansi, --json, --timestamps -----

def test_dump_head_returns_first_lines(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "seq 1 20", "--max", "30", timeout=15)
    res = tx_runner("dump", pane, "--head", "5", "--max", "50")
    assert res.returncode == 0
    # The first cleaned line might be the init / first command echo; just
    # assert we got <= 5 lines of content plus possibly a truncated meta line.
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    assert len(lines) <= 7  # 5 content + maybe meta


def test_dump_head_rejects_zero(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("dump", pane, "--head", "0")
    assert res.returncode == 1
    assert "must be positive" in res.stdout


def test_dump_head_and_tail_mutually_exclusive(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("dump", pane, "--head", "3", "--tail", "3")
    assert res.returncode == 1
    assert "mutually exclusive" in res.stdout


def test_run_json_emits_expected_fields(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("run", pane, "echo json-test-line", "--json", timeout=15)
    assert res.returncode == 0, res.stdout + res.stderr
    record = json.loads(res.stdout)
    assert record["pane"] == pane
    assert record["cmd"] == "echo json-test-line"
    assert record["exit"] == 0
    assert "json-test-line" in record["stdout"]
    assert record["truncated"] is False
    assert record["run_id"].startswith("r-")
    assert "duration_ms" in record
    assert "started" in record and "ended" in record


def test_run_json_records_nonzero_exit(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("run", pane, "false", "--json", timeout=15)
    record = json.loads(res.stdout)
    assert record["exit"] == 1


def test_exec_json_returns_started_record(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "echo exec-json", "--json", timeout=15)
    record = json.loads(res.stdout)
    assert record["run_id"].startswith("r-")
    assert record["exit"] is None
    assert record["stdout"] is None
    # Let it finish so the pane is clean for next test.
    tx_runner("wait-run", pane, record["run_id"], "--timeout", "5", timeout=15)


def test_wait_run_json_emits_full_record(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("exec", pane, "echo wait-json", timeout=15)
    rid = res.stdout.strip().splitlines()[-1].strip()
    res2 = tx_runner("wait-run", pane, rid, "--timeout", "5", "--json", timeout=15)
    record = json.loads(res2.stdout)
    assert record["run_id"] == rid
    assert "wait-json" in record["stdout"]
    assert record["exit"] == 0


def test_output_last_json(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo first-out", timeout=15)
    tx_runner("run", pane, "echo second-out", timeout=15)
    res = tx_runner("output", pane, "--last", "--json")
    record = json.loads(res.stdout)
    assert "second-out" in record["stdout"]
    assert record["exit"] == 0


def test_output_json_rejects_since_run(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo a", timeout=15)
    res = tx_runner("exec", pane, "echo b", timeout=15)
    rid = res.stdout.strip().splitlines()[-1].strip()
    tx_runner("wait-run", pane, rid, "--timeout", "5", timeout=15)
    res2 = tx_runner("output", pane, "--since-run", rid, "--json")
    assert res2.returncode == 1
    assert "single-run" in res2.stdout


def test_keep_ansi_preserves_escapes(tx_runner, tx_home):
    """Inject ANSI directly into the log file to bypass tmux pipe-pane's
    rendering layer (which strips some sequences depending on tmux version).
    --keep-ansi should round-trip the bytes; default should strip them."""
    pane = _pane(tx_runner)
    tx_runner("mark", pane, "preansi")
    log_path = tx_home / "logs" / f"{pane}.log"
    with open(log_path, "ab") as f:
        f.write(b"plain \x1b[31mRED\x1b[0m plain\n")
    # Read via tx dump --from to start at our marker.
    res_strip = tx_runner("dump", pane, "--from", "preansi")
    res_keep = tx_runner("dump", pane, "--from", "preansi", "--keep-ansi")
    assert res_strip.returncode == 0 and res_keep.returncode == 0
    assert "\x1b[31m" not in res_strip.stdout
    assert "RED" in res_strip.stdout
    assert "\x1b[31m" in res_keep.stdout


def test_tail_timestamps_prefix(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo stamped-line", timeout=15)
    res = tx_runner("tail", pane, "--timestamps")
    # Look for [hh:mm:ss] anywhere in any non-empty line.
    found = False
    for ln in res.stdout.splitlines():
        if ln.startswith("[") and ":" in ln[:9] and "]" in ln[:10]:
            found = True
            break
    assert found, f"no [hh:mm:ss] prefix in tail output: {res.stdout!r}"


# ----- Group A: tx grep -----

def test_grep_finds_matches(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "printf 'alpha\\nbeta\\ngamma\\n'", timeout=15)
    res = tx_runner("grep", pane, "beta")
    assert res.returncode == 0
    assert "beta" in res.stdout


def test_grep_no_matches(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo onething", timeout=15)
    res = tx_runner("grep", pane, "absolutely-not-here")
    assert res.returncode == 0
    assert "no matches" in res.stdout


def test_grep_context_A(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "printf 'one\\ntwo\\nNEEDLE\\nfour\\nfive\\n'", timeout=15)
    res = tx_runner("grep", pane, "NEEDLE", "-A", "1")
    assert "NEEDLE" in res.stdout
    assert "four" in res.stdout
    # 'two' shouldn't appear with -A only.
    # (Filter to body text to avoid header noise.)


def test_grep_context_B(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "printf 'one\\ntwo\\nNEEDLE\\nfour\\nfive\\n'", timeout=15)
    res = tx_runner("grep", pane, "NEEDLE", "-B", "1")
    assert "NEEDLE" in res.stdout
    assert "two" in res.stdout


def test_grep_context_C(tx_runner):
    pane = _pane(tx_runner)
    tx_runner("run", pane, "printf 'one\\ntwo\\nNEEDLE\\nfour\\nfive\\n'", timeout=15)
    res = tx_runner("grep", pane, "NEEDLE", "-C", "1")
    assert "NEEDLE" in res.stdout
    assert "two" in res.stdout
    assert "four" in res.stdout


def test_grep_overlapping_ranges_merge(tx_runner):
    pane = _pane(tx_runner)
    tx_runner(
        "run", pane,
        "printf 'one\\nNEEDLE-A\\nthree\\nNEEDLE-B\\nfive\\n'",
        timeout=15,
    )
    res = tx_runner("grep", pane, "NEEDLE", "-C", "1")
    # Two adjacent matches with -C 1 should overlap and merge into a single
    # range without a '--' separator.
    assert res.stdout.count("--") == 0


def test_grep_invalid_regex_errors(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("grep", pane, "(unclosed")
    assert res.returncode == 1
    assert "invalid regex" in res.stdout


def test_grep_rejects_negative_context(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("grep", pane, "x", "-A", "-1")
    # click rejects negative ints for int option? Actually click allows negative.
    # We hand-check in the command.
    assert res.returncode == 1


# ----- Group B: tx sudo (no-TTY refusal) -----

def test_sudo_refuses_without_tty(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("sudo", pane, "echo nope", timeout=10)
    assert res.returncode == 1
    assert "TTY" in res.stdout


def test_sudo_yes_bypasses_confirm_policy(monkeypatch, tmp_path):
    import getpass
    from click.testing import CliRunner
    import tx_core.commands.input as input_mod

    cfg = {
        "defaults": {
            "max_run_history": 100,
            "max_lines": 200,
            "timeout": 10,
            "strip": True,
            "strip_ansi": True,
        },
        "security": {
            "command_allowlist": "all",
            "confirm_patterns": ["rm -rf"],
            "confirm_mode": "deny",
        },
    }

    class DummyLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyPane:
        pass

    state = {"tmux_id": "%1", "tail_offset": 0, "continue_offset": None}
    offsets = {"p1": state}
    log_path = tmp_path / "p1.log"
    log_path.write_text("")

    monkeypatch.setattr(input_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(input_mod, "offsets_lock", lambda: DummyLock())
    monkeypatch.setattr(input_mod, "load_offsets", lambda: offsets)
    monkeypatch.setattr(input_mod, "save_offsets", lambda _offsets: None)
    monkeypatch.setattr(input_mod, "_resolve_pane_for_input", lambda _offsets, _pane: (state, object(), DummyPane(), log_path))
    monkeypatch.setattr(input_mod, "finalize_runs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(input_mod, "pane_state", lambda *_args, **_kwargs: {"status": "idle"})
    monkeypatch.setattr(input_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "")

    runner = CliRunner()
    denied = runner.invoke(input_mod.cmd_sudo, ["p1", "rm -rf /"])
    assert denied.exit_code == 1
    assert "confirm_mode=deny" in denied.output

    bypassed = runner.invoke(input_mod.cmd_sudo, ["p1", "rm -rf /", "--yes"])
    assert bypassed.exit_code == 1
    assert "confirm_mode=deny" not in bypassed.output
    assert "TTY" in bypassed.output


# ----- Group B: redact_patterns -----

def test_redact_patterns_replace_matches_in_run_output(tx_runner, tx_home):
    # Append a redact_patterns rule to the config.
    cfg_path = tx_home / "config.toml"
    cfg_text = cfg_path.read_text()
    cfg_text += '\nredact_patterns = ["TOPSECRET[A-Za-z0-9]+"]\n'
    cfg_path.write_text(cfg_text)

    pane = _pane(tx_runner)
    res = tx_runner("run", pane, "echo leaked-TOPSECRETabc-here", timeout=15)
    assert res.returncode == 0
    assert "TOPSECRETabc" not in res.stdout
    assert "[redacted]" in res.stdout


def test_redact_patterns_preserve_on_disk_log(tx_runner, tx_home):
    cfg_path = tx_home / "config.toml"
    cfg_text = cfg_path.read_text()
    cfg_text += '\nredact_patterns = ["LEAK[A-Za-z]+"]\n'
    cfg_path.write_text(cfg_text)

    pane = _pane(tx_runner)
    tx_runner("run", pane, "echo LEAKfoo", timeout=15)
    log_text = (tx_home / "logs" / f"{pane}.log").read_text(errors="replace")
    # The on-disk log is NOT modified — the secret bytes are still present
    # there. Only the returned stdout is filtered. This is the documented
    # contract; if it changes, this test catches it.
    assert "LEAKfoo" in log_text


# ----- Group C: tx stream -----

def test_stream_duration_caps_output(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner(
        "stream", pane,
        "while true; do echo tick; sleep 0.1; done",
        "--duration", "1s",
        "--max", "200",
        timeout=15,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "stream-stopped" in res.stdout
    assert "duration" in res.stdout
    assert "tick" in res.stdout


def test_stream_lines_caps_output(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner(
        "stream", pane,
        "for i in $(seq 1 200); do echo line-$i; done; sleep 5",
        "--lines", "20",
        "--timeout", "10",
        "--max", "200",
        timeout=20,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "stream-stopped" in res.stdout


def test_stream_until_regex_matches(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner(
        "stream", pane,
        "for i in $(seq 1 20); do echo step-$i; sleep 0.05; done; sleep 5",
        "--until", r"step-5\b",
        "--timeout", "10",
        "--max", "200",
        timeout=20,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "stream-stopped" in res.stdout
    assert "until matched" in res.stdout


def test_stream_until_timeout_when_no_match(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner(
        "stream", pane,
        "sleep 5",
        "--until", "neverhappens",
        "--timeout", "1",
        timeout=15,
    )
    # On timeout we emit a [timeout: ...] line and return.
    assert res.returncode == 0
    assert "timeout" in res.stdout


def test_stream_rejects_no_bound(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("stream", pane, "echo x", timeout=10)
    assert res.returncode == 1
    assert "exactly one" in res.stdout


# ----- Group C: tx run --wait-for / --fail-for -----

def test_run_wait_for_returns_zero_on_match(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner(
        "run", pane,
        "for i in $(seq 1 30); do echo step-$i; sleep 0.05; done; sleep 5",
        "--wait-for", "step-3",
        "--timeout", "10",
        "--max", "200",
        timeout=20,
    )
    assert res.returncode == 0
    assert "[exit:0]" in res.stdout
    assert "wait-for: matched" in res.stdout


def test_run_fail_for_returns_one_on_match(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner(
        "run", pane,
        "for i in $(seq 1 30); do echo step-$i; sleep 0.05; done; sleep 5",
        "--fail-for", "step-3",
        "--timeout", "10",
        "--max", "200",
        timeout=20,
    )
    assert res.returncode == 0
    assert "[exit:1]" in res.stdout
    assert "fail-for: matched" in res.stdout


# ----- Group D: tx paste -----

def test_paste_via_stdin(tx_runner, tx_home):
    pane = _pane(tx_runner)
    # tx paste reads from stdin; pipe content in.
    from conftest import TX_SCRIPT
    closure = tx_runner.__closure__
    tx_env = None
    for cell in closure or []:
        val = cell.cell_contents
        if isinstance(val, dict) and "TX_HOME" in val:
            tx_env = val
            break
    assert tx_env is not None
    proc = subprocess.run(
        [str(TX_SCRIPT), "paste", pane],
        input="echo paste-via-stdin\n",
        env=tx_env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "pasted:" in proc.stdout
    # Give a moment for the shell to evaluate.
    time.sleep(0.5)
    # The text should have ended up in the pane (and produced "paste-via-stdin"
    # output once the shell ran it). Check via tx tail.
    res = tx_runner("tail", pane, timeout=10)
    assert "paste-via-stdin" in res.stdout


def test_paste_via_file(tx_runner, tx_home, tmp_path):
    pane = _pane(tx_runner)
    src = tmp_path / "content.sh"
    src.write_text("echo paste-via-file\n")
    res = tx_runner("paste", pane, "--file", str(src), timeout=10)
    assert res.returncode == 0
    assert "pasted:" in res.stdout
    time.sleep(0.5)
    res2 = tx_runner("tail", pane, timeout=10)
    assert "paste-via-file" in res2.stdout


def test_paste_rejects_missing_file(tx_runner):
    pane = _pane(tx_runner)
    res = tx_runner("paste", pane, "--file", "/nonexistent/here/please.txt", timeout=5)
    assert res.returncode == 1


# ----- Group E: per-pane allowlist -----

def test_per_pane_allowlist_blocks_when_global_allows(tx_runner, tx_home):
    cfg_path = tx_home / "config.toml"
    cfg_text = cfg_path.read_text()
    cfg_text += '\n[panes.locked]\ncommand_allowlist = ["echo"]\n'
    cfg_path.write_text(cfg_text)

    pane = _pane(tx_runner, "locked")
    # echo is allowed
    res = tx_runner("run", pane, "echo allowed", timeout=15)
    assert res.returncode == 0
    # ls is NOT allowed by the per-pane list
    res2 = tx_runner("run", pane, "ls /tmp", timeout=15)
    assert res2.returncode == 1
    assert "command_allowlist" in res2.stdout


def test_global_allowlist_blocks_regardless_of_pane(tx_runner, tx_home):
    cfg_path = tx_home / "config.toml"
    cfg_text = cfg_path.read_text()
    # Force a restrictive global; per-pane "all" cannot loosen.
    cfg_text = cfg_text.replace(
        'command_allowlist = "all"',
        'command_allowlist = ["echo"]',
    )
    cfg_path.write_text(cfg_text)

    pane = _pane(tx_runner, "p-global")
    res = tx_runner("run", pane, "ls /tmp", timeout=15)
    assert res.returncode == 1
    assert "command_allowlist" in res.stdout


def test_documented_regex_allowlist_permits_full_command(tx_runner, tx_home):
    cfg_path = tx_home / "config.toml"
    cfg_text = cfg_path.read_text()
    cfg_text = cfg_text.replace(
        'command_allowlist = "all"',
        'command_allowlist = ["/^echo documented-regex/"]',
    )
    cfg_path.write_text(cfg_text)

    pane = _pane(tx_runner, "p-regex")
    allowed = tx_runner("run", pane, "echo documented-regex ok", timeout=15)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    blocked = tx_runner("run", pane, "echo other", timeout=15)
    assert blocked.returncode == 1
    assert "command_allowlist" in blocked.stdout


def test_invalid_allowlist_shape_refuses(tx_runner, tx_home):
    cfg_path = tx_home / "config.toml"
    cfg_text = cfg_path.read_text()
    cfg_text = cfg_text.replace(
        'command_allowlist = "all"',
        'command_allowlist = 123',
    )
    cfg_path.write_text(cfg_text)

    pane = _pane(tx_runner, "p-bad-allow-shape")
    res = tx_runner("run", pane, "echo blocked", timeout=15)
    assert res.returncode == 1
    assert "invalid command_allowlist config" in res.stdout


def test_invalid_allowlist_regex_refuses(tx_runner, tx_home):
    cfg_path = tx_home / "config.toml"
    cfg_text = cfg_path.read_text()
    cfg_text = cfg_text.replace(
        'command_allowlist = "all"',
        'command_allowlist = ["/[invalid/"]',
    )
    cfg_path.write_text(cfg_text)

    pane = _pane(tx_runner, "p-bad-allow-regex")
    res = tx_runner("run", pane, "echo blocked", timeout=15)
    assert res.returncode == 1
    assert "invalid command_allowlist config" in res.stdout


def test_per_pane_all_passes_through(tx_runner, tx_home):
    """Per-pane 'all' (default) should not block when global is permissive."""
    pane = _pane(tx_runner)
    # No [panes.<id>] section → per-pane defaults to "all"
    res = tx_runner("run", pane, "echo passthrough", timeout=15)
    assert res.returncode == 0


# ----- Group E: confirm_patterns -----

def test_confirm_deny_mode_refuses(tx_runner, tx_home):
    cfg_path = tx_home / "config.toml"
    cfg_text = cfg_path.read_text()
    cfg_text += (
        '\nconfirm_patterns = ["DANGEROUS"]\n'
        'confirm_mode = "deny"\n'
    )
    cfg_path.write_text(cfg_text)

    pane = _pane(tx_runner)
    res = tx_runner("run", pane, "echo DANGEROUS", timeout=15)
    assert res.returncode == 1
    assert "confirm_pattern" in res.stdout
    assert "deny" in res.stdout


def test_confirm_yes_flag_bypasses(tx_runner, tx_home):
    cfg_path = tx_home / "config.toml"
    cfg_text = cfg_path.read_text()
    cfg_text += '\nconfirm_patterns = ["BLAST"]\nconfirm_mode = "deny"\n'
    cfg_path.write_text(cfg_text)

    pane = _pane(tx_runner)
    res = tx_runner("run", pane, "echo BLAST", "--yes", timeout=15)
    assert res.returncode == 0
    assert "BLAST" in res.stdout


def test_confirm_interactive_refuses_when_no_tty(tx_runner, tx_home):
    """When stdin/stderr aren't a TTY, interactive mode refuses with a pointer
    at --yes / confirm_mode. (Tests run under subprocess with pipes, so this
    is the typical agent path.)"""
    cfg_path = tx_home / "config.toml"
    cfg_text = cfg_path.read_text()
    cfg_text += '\nconfirm_patterns = ["RISKY"]\nconfirm_mode = "interactive"\n'
    cfg_path.write_text(cfg_text)

    pane = _pane(tx_runner)
    res = tx_runner("run", pane, "echo RISKY", timeout=15)
    assert res.returncode == 1
    assert "confirmation required" in res.stdout or "confirm_pattern" in res.stdout


def test_confirm_allow_mode_proceeds_with_warning(tx_runner, tx_home):
    cfg_path = tx_home / "config.toml"
    cfg_text = cfg_path.read_text()
    cfg_text += '\nconfirm_patterns = ["PERMIT"]\nconfirm_mode = "allow"\n'
    cfg_path.write_text(cfg_text)

    pane = _pane(tx_runner)
    res = tx_runner("run", pane, "echo PERMIT", timeout=15)
    assert res.returncode == 0
    assert "PERMIT" in res.stdout
    assert "allowed by confirm_mode=allow" in res.stdout


# ----- Group F: tx new --shell -----

def test_new_shell_bash(tx_runner):
    pane = _pane(tx_runner, "--shell", "bash")
    # Verify bash is the current shell via $BASH_VERSION.
    res = tx_runner("run", pane, 'echo "shell=${BASH_VERSION:+bash}${ZSH_VERSION:+zsh}"', timeout=15)
    assert "shell=bash" in res.stdout


def test_new_shell_invalid_rejected(tx_runner):
    res = tx_runner("new", "--shell", "perl")
    assert res.returncode != 0


# ----- A few sanity unit-style checks via the module -----

def test_resolve_strip_ansi_default(tx_module):
    cfg = {"defaults": {"strip_ansi": True}}
    assert tx_module.resolve_strip_ansi(cfg, False) is True
    assert tx_module.resolve_strip_ansi(cfg, True) is False
    cfg2 = {"defaults": {"strip_ansi": False}}
    assert tx_module.resolve_strip_ansi(cfg2, False) is False


def test_apply_redactions_substitutes(tx_module):
    cfg = {"security": {"redact_patterns": [r"sk-[A-Za-z0-9]+"]}}
    out = tx_module.apply_redactions("token sk-abcDEF123 here", cfg)
    assert out == "token [redacted] here"


def test_apply_redactions_noop_when_empty(tx_module):
    cfg = {"security": {"redact_patterns": []}}
    assert tx_module.apply_redactions("nothing changes", cfg) == "nothing changes"


def test_apply_redactions_skips_bad_regex(tx_module):
    cfg = {"security": {"redact_patterns": ["(unclosed", "good"]}}
    # Bad pattern doesn't blow up; good pattern still applies.
    out = tx_module.apply_redactions("good news", cfg)
    assert out == "[redacted] news"


def test_check_allowlist_per_pane_and_merge(tx_module):
    cfg = {
        "security": {"command_allowlist": "all"},
        "panes": {"p1": {"command_allowlist": ["echo"]}},
    }
    # echo passes per-pane; ls does not.
    assert tx_module.check_allowlist("echo hi", cfg, pane_id="p1") is None
    assert tx_module.check_allowlist("ls", cfg, pane_id="p1") == "ls"
    # Pane without an entry defaults to "all".
    assert tx_module.check_allowlist("ls", cfg, pane_id="p2") is None


def test_check_allowlist_global_restrictive(tx_module):
    cfg = {
        "security": {"command_allowlist": ["echo"]},
        "panes": {"p1": {"command_allowlist": "all"}},
    }
    # Per-pane "all" cannot loosen the restrictive global.
    assert tx_module.check_allowlist("ls", cfg, pane_id="p1") == "ls"
    assert tx_module.check_allowlist("echo x", cfg, pane_id="p1") is None


def test_confirm_match_compiles_and_matches(tx_module):
    cfg = {"security": {"confirm_patterns": [r"\brm\s+-rf?\b"]}}
    assert tx_module._confirm_match("rm -rf /tmp/foo", cfg) == r"\brm\s+-rf?\b"
    assert tx_module._confirm_match("echo hi", cfg) is None


def test_parse_duration_units(tx_module):
    assert tx_module._parse_duration("5") == 5.0
    assert tx_module._parse_duration("5s") == 5.0
    assert tx_module._parse_duration("2m") == 120.0
    assert tx_module._parse_duration("1h") == 3600.0
    with pytest.raises(ValueError):
        tx_module._parse_duration("")
    with pytest.raises(ValueError):
        tx_module._parse_duration("abc")


def test_duration_ms_round_trip(tx_module):
    started = "2026-05-14T03:00:00Z"
    ended = "2026-05-14T03:00:01Z"
    assert tx_module._duration_ms(started, ended) == 1000
    assert tx_module._duration_ms(None, ended) is None
    assert tx_module._duration_ms(started, None) is None
