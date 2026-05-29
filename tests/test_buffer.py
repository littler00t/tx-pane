"""Unit tests for pure buffer/cleaning helpers."""

from __future__ import annotations


def test_ansi_stripping_basic(tx_module):
    s = "\x1b[31mred\x1b[0m text"
    cleaned = tx_module.ANSI_RE.sub("", s)
    assert cleaned == "red text"


def test_ansi_stripping_complex(tx_module):
    # CSI, OSC, simple ESC, color codes with parameters
    s = "\x1b]0;title\x07prefix\x1b[1;31mhello\x1b[0m\x1b[Kafter"
    cleaned = tx_module.ANSI_RE.sub("", s)
    assert cleaned == "prefixhelloafter"


def test_strip_lines_blank_collapse(tx_module):
    text = "a\n\n\n\n\nb"
    out = tx_module._strip_lines(text, strip_blanks=True)
    # 5 consecutive blanks should collapse to 2.
    assert out == ["a", "", "", "b"]


def test_strip_lines_leading_trailing_trim(tx_module):
    text = "\n\n  \nhello\nworld\n  \n\n"
    out = tx_module._strip_lines(text, strip_blanks=True)
    assert out == ["hello", "world"]


def test_strip_lines_no_strip(tx_module):
    text = "\n\n\nfoo  \n\n\nbar  \n\n\n"
    out = tx_module._strip_lines(text, strip_blanks=False)
    # When strip_blanks=False, lines are returned as-is (split on \n), no rstrip.
    assert out == ["", "", "", "foo  ", "", "", "bar  ", "", "", ""]


def test_strip_lines_trailing_whitespace_rstripped(tx_module):
    text = "foo   \nbar\t\n"
    out = tx_module._strip_lines(text, strip_blanks=True)
    assert out == ["foo", "bar"]


def test_process_raw_log_under_limit(tx_module):
    raw = b"line1\nline2\nline3\n"
    kept, truncated, remaining, consumed = tx_module.process_raw_log(raw, 10, True)
    assert kept == ["line1", "line2", "line3"]
    assert truncated is False
    assert remaining == 0
    assert consumed == len(raw)


def test_process_raw_log_truncation(tx_module):
    raw = b"\n".join(f"line{i}".encode() for i in range(20)) + b"\n"
    kept, truncated, remaining, consumed = tx_module.process_raw_log(raw, 5, True)
    assert kept == [f"line{i}" for i in range(5)]
    assert truncated is True
    assert remaining == 15


def test_process_raw_log_exact_max_no_truncation(tx_module):
    raw = b"\n".join(f"line{i}".encode() for i in range(5)) + b"\n"
    kept, truncated, remaining, consumed = tx_module.process_raw_log(raw, 5, True)
    assert kept == [f"line{i}" for i in range(5)]
    assert truncated is False
    assert remaining == 0


def test_process_raw_log_strips_ansi(tx_module):
    raw = b"\x1b[31mred\x1b[0m\nplain\n"
    kept, _, _, _ = tx_module.process_raw_log(raw, 10, True)
    assert kept == ["red", "plain"]


def test_process_raw_log_no_trailing_newline(tx_module):
    raw = b"a\nb\nc"
    kept, _, _, _ = tx_module.process_raw_log(raw, 10, True)
    assert kept == ["a", "b", "c"]


def test_process_raw_log_carriage_returns(tx_module):
    raw = b"hello\r\nworld\r\n"
    kept, _, _, _ = tx_module.process_raw_log(raw, 10, True)
    assert kept == ["hello", "world"]


def test_process_raw_log_blank_collapse(tx_module):
    raw = b"a\n\n\n\n\nb\n"
    kept, _, _, _ = tx_module.process_raw_log(raw, 10, True)
    # leading blanks dropped at trim; internal runs collapsed to 2.
    # Order: a, blank, blank, b (after collapse-of-3+ to 2)
    assert kept == ["a", "", "", "b"]


def test_process_raw_log_no_strip_preserves_blanks(tx_module):
    raw = b"a\n\n\n\n\nb\n"
    kept, _, _, _ = tx_module.process_raw_log(raw, 10, False)
    assert kept == ["a", "", "", "", "", "b"]


def test_split_raw_by_newlines_basic(tx_module):
    raw = b"a\nb\nc\n"
    parts = tx_module._split_raw_by_newlines(raw)
    assert [p[0] for p in parts] == [b"a", b"b", b"c"]
    assert [p[1] for p in parts] == [2, 4, 6]


def test_split_raw_by_newlines_no_trailing(tx_module):
    raw = b"a\nb\nc"
    parts = tx_module._split_raw_by_newlines(raw)
    assert [p[0] for p in parts] == [b"a", b"b", b"c"]
    assert parts[-1][1] == len(raw)


def test_split_raw_by_newlines_empty(tx_module):
    parts = tx_module._split_raw_by_newlines(b"")
    assert parts == []


def test_last_non_empty_line(tx_module):
    assert tx_module._last_non_empty_line("a\nb\n\n  \n") == "b"
    assert tx_module._last_non_empty_line("$ ") == "$"
    assert tx_module._last_non_empty_line("") == ""
    assert tx_module._last_non_empty_line("\n\n") == ""


def test_last_non_empty_line_strips_ansi(tx_module):
    line = "\x1b[32mhello\x1b[0m\n"
    assert tx_module._last_non_empty_line(line) == "hello"


def test_check_allowlist_all_allows_everything(tx_module):
    cfg = {"security": {"command_allowlist": "all"}}
    assert tx_module.check_allowlist("rm -rf /", cfg) is None


def test_check_allowlist_none_blocks_everything(tx_module):
    cfg = {"security": {"command_allowlist": "none"}}
    assert tx_module.check_allowlist("echo hi", cfg) == "echo"


def test_check_allowlist_permits_when_first_token_allowed(tx_module):
    cfg = {"security": {"command_allowlist": ["echo", "ls"]}}
    assert tx_module.check_allowlist("echo hi there", cfg) is None
    assert tx_module.check_allowlist("ls -la", cfg) is None


def test_check_allowlist_blocks_unknown(tx_module):
    cfg = {"security": {"command_allowlist": ["echo"]}}
    assert tx_module.check_allowlist("rm -rf /", cfg) == "rm"


def test_check_allowlist_handles_leading_whitespace(tx_module):
    cfg = {"security": {"command_allowlist": ["echo"]}}
    assert tx_module.check_allowlist("   echo hi", cfg) is None


def test_check_allowlist_regex_pattern(tx_module):
    cfg = {"security": {"command_allowlist": ["/^sudo -n /"]}}
    assert tx_module.check_allowlist("sudo -n smartctl -a /dev/sda", cfg) is None
    assert tx_module.check_allowlist("sudo rm -rf /", cfg) == "sudo"


def test_check_allowlist_empty_list_allows_all(tx_module):
    # Backwards-compat: legacy "empty list" form (after deprecation warning).
    cfg = {"security": {"allowed_commands": []}}
    # Suppress deprecation noise across test runs.
    tx_module._DEPRECATION_WARNED.add("allowed_commands")
    assert tx_module.check_allowlist("rm -rf /", cfg) is None


def test_check_allowlist_empty_command_allowlist_fails_closed(tx_module):
    cfg = {"security": {"command_allowlist": []}}
    try:
        tx_module.check_allowlist("rm -rf /", cfg)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("empty command_allowlist should refuse")


def test_check_allowlist_legacy_key_with_entries(tx_module):
    tx_module._DEPRECATION_WARNED.add("allowed_commands")
    cfg = {"security": {"allowed_commands": ["echo"]}}
    assert tx_module.check_allowlist("echo hi", cfg) is None
    assert tx_module.check_allowlist("rm hi", cfg) == "rm"

