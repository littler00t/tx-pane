"""Pure unit tests for tx_core.security (no subprocess, no tmux)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tx_core.security import (
    _check_one_allowlist,
    _confirm_match,
    check_allowlist,
    check_confirm,
)


class TestCheckOneAllowlist:
    def test_all_permits_anything(self):
        assert _check_one_allowlist("all", "rm -rf /") is None

    def test_none_blocks_anything(self):
        assert _check_one_allowlist("none", "ls") == "ls"

    def test_empty_command_with_none_returns_none(self):
        # Empty/whitespace input is a no-op at the policy layer.
        assert _check_one_allowlist("none", "") is None
        assert _check_one_allowlist("none", "   ") is None

    def test_token_match(self):
        assert _check_one_allowlist(["echo", "ls"], "ls -la") is None
        assert _check_one_allowlist(["echo", "ls"], "rm -rf /") == "rm"

    def test_regex_match(self):
        rule = ["/^git (status|log)/"]
        assert _check_one_allowlist(rule, "git status") is None
        assert _check_one_allowlist(rule, "git log") is None
        assert _check_one_allowlist(rule, "git push") == "git"

    def test_invalid_regex_fails_closed(self):
        rule = ["/[invalid/", "echo"]
        with pytest.raises(SystemExit) as exc:
            _check_one_allowlist(rule, "echo hi")
        assert exc.value.code == 1

    def test_empty_list_fails_closed(self):
        with pytest.raises(SystemExit) as exc:
            _check_one_allowlist([], "rm -rf /")
        assert exc.value.code == 1

    def test_invalid_rule_shape_fails_closed(self):
        with pytest.raises(SystemExit) as exc:
            _check_one_allowlist({"echo": True}, "echo hi")  # type: ignore[arg-type]
        assert exc.value.code == 1

    def test_invalid_list_entry_fails_closed(self):
        with pytest.raises(SystemExit) as exc:
            _check_one_allowlist(["echo", 123], "ls")  # type: ignore[list-item]
        assert exc.value.code == 1


class TestCheckAllowlist:
    def test_global_all_pane_all(self):
        cfg = {"security": {"command_allowlist": "all"}}
        assert check_allowlist("rm -rf /", cfg) is None

    def test_global_blocks(self):
        cfg = {"security": {"command_allowlist": ["echo"]}}
        assert check_allowlist("rm", cfg) == "rm"

    def test_pane_blocks_when_global_permits(self):
        cfg = {
            "security": {"command_allowlist": "all"},
            "panes": {"p1": {"command_allowlist": ["echo"]}},
        }
        assert check_allowlist("rm", cfg, pane_id="p1") == "rm"
        assert check_allowlist("rm", cfg, pane_id="other") is None

    def test_pane_all_and_global_all(self):
        cfg = {
            "security": {"command_allowlist": "all"},
            "panes": {"p1": {"command_allowlist": "all"}},
        }
        assert check_allowlist("rm -rf /", cfg, pane_id="p1") is None

    def test_documented_regex_form(self):
        cfg = {"security": {"command_allowlist": ["/^systemctl status nginx/"]}}
        assert check_allowlist("systemctl status nginx", cfg) is None
        assert check_allowlist("systemctl restart nginx", cfg) == "systemctl"


class TestConfirmMatch:
    def test_matches_pattern(self):
        cfg = {"security": {"confirm_patterns": [r"^rm\s+-rf\b"]}}
        assert _confirm_match("rm -rf /", cfg) == r"^rm\s+-rf\b"

    def test_no_patterns(self):
        assert _confirm_match("anything", {}) is None
        assert _confirm_match("anything", {"security": {}}) is None

    def test_first_match_wins(self):
        cfg = {"security": {"confirm_patterns": [r"^rm\b", r"^rm -rf"]}}
        assert _confirm_match("rm -rf /", cfg) == r"^rm\b"

    def test_invalid_regex_skipped(self):
        cfg = {"security": {"confirm_patterns": ["/[bad", r"^echo"]}}
        assert _confirm_match("echo hi", cfg) == r"^echo"


class TestCheckConfirm:
    """Cover the six branches of check_confirm: yes-flag short-circuit, no
    match (no-op), mode=allow (warn but pass), mode=deny (refuse), interactive
    without TTY (refuse), interactive with TTY answering yes (pass), and TTY
    answering no (refuse)."""

    _CFG = {"security": {"confirm_patterns": [r"^rm -rf\b"], "confirm_mode": "interactive"}}

    def test_yes_flag_shortcircuits(self, capsys):
        check_confirm("rm -rf /", self._CFG, yes=True)
        assert capsys.readouterr().out == ""

    def test_no_match_is_noop(self, capsys):
        check_confirm("ls", self._CFG, yes=False)
        assert capsys.readouterr().out == ""

    def test_mode_allow_warns_but_passes(self, capsys):
        cfg = {"security": {"confirm_patterns": [r"^rm\b"], "confirm_mode": "allow"}}
        check_confirm("rm -rf /", cfg, yes=False)
        out = capsys.readouterr().out
        assert "[warning:" in out and "allowed by confirm_mode=allow" in out

    def test_mode_deny_refuses(self, capsys):
        cfg = {"security": {"confirm_patterns": [r"^rm\b"], "confirm_mode": "deny"}}
        with pytest.raises(SystemExit) as exc:
            check_confirm("rm -rf /", cfg, yes=False)
        assert exc.value.code == 1
        assert "confirm_mode=deny" in capsys.readouterr().out

    def test_interactive_without_tty_refuses(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stderr.isatty", lambda: False)
        with pytest.raises(SystemExit):
            check_confirm("rm -rf /", self._CFG, yes=False)
        assert "confirmation required" in capsys.readouterr().out

    def test_interactive_tty_user_says_yes(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")
        check_confirm("rm -rf /", self._CFG, yes=False)
        # Confirm prompt is emitted via click.echo(err=True). No SystemExit.
        out = capsys.readouterr()
        assert "type 'yes' to proceed" in out.err

    def test_interactive_tty_user_says_no(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
        with pytest.raises(SystemExit):
            check_confirm("rm -rf /", self._CFG, yes=False)
        assert "declined" in capsys.readouterr().out

    def test_interactive_tty_eof_treated_as_no(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        def _raise_eof(_prompt=""):
            raise EOFError
        monkeypatch.setattr("builtins.input", _raise_eof)
        with pytest.raises(SystemExit):
            check_confirm("rm -rf /", self._CFG, yes=False)
        assert "declined" in capsys.readouterr().out
