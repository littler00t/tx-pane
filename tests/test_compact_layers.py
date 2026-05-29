"""Pure-function unit tests for tx_compact L1 hygiene and L2 whitespace.

Phase-1 scope. Each test exercises a layer directly (no tmux, no
subprocess, no I/O). Target: full suite under 1 second.

L3 RLE / L4 budget / L5 dedup tests slot into this file as those layers
land in later phases.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
import sys

import pytest

# tx_compact is a sibling package next to the tx script at the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tx_compact import (  # noqa: E402
    compact,
    CompactCtx,
    CompactResult,
    Tier,
    BUILTIN_BANNERS,
    is_compaction_disabled,
)
from tx_compact.layers import (  # noqa: E402
    apply_l1_hygiene,
    apply_l2_whitespace,
    apply_l3_rle,
)
from tx_compact.budget import apply_l4_budget, L4Decision  # noqa: E402
from tx_compact.tokens import estimate as estimate_tokens  # noqa: E402
from tx_compact import HANDLE_PLACEHOLDER  # noqa: E402
from tx_compact import handle as handle_store  # noqa: E402


# ---------------------------------------------------------------------
# Top-level compact() contract
# ---------------------------------------------------------------------

class TestCompactEntrypoint:
    def test_raw_mode_is_identity(self):
        ctx = CompactCtx(mode="raw", cmd="echo hi")
        text = "hello\nworld\n\n\n"
        result = compact(text, ctx)
        assert isinstance(result, CompactResult)
        assert result.text == text
        assert result.tier == Tier.FULL
        assert result.applied_layers == []
        assert result.in_bytes == result.out_bytes

    def test_tx_no_compact_env_short_circuits(self, monkeypatch):
        monkeypatch.setenv("TX_NO_COMPACT", "1")
        assert is_compaction_disabled() is True
        ctx = CompactCtx(mode="terse", cmd="apt list")
        # Banner that L1 would normally strip:
        text = "Reading package lists... Done\nfoo\n"
        result = compact(text, ctx)
        assert result.text == text
        assert result.applied_layers == []
        assert result.tier == Tier.FULL

    def test_tx_no_compact_unset(self, monkeypatch):
        monkeypatch.delenv("TX_NO_COMPACT", raising=False)
        assert is_compaction_disabled() is False

    def test_terse_fires_l1_and_l2(self):
        ctx = CompactCtx(mode="terse", cmd="apt list")
        text = "Reading package lists... Done\nfoo\n\n\nbar\n"
        result = compact(text, ctx)
        assert "Reading package lists" not in result.text
        # L2 tightens blank runs to 1
        assert "\n\n\n" not in result.text
        assert "L1" in result.applied_layers
        assert "L2" in result.applied_layers
        assert result.out_bytes < result.in_bytes

    def test_terse_with_no_strip_banners_skips_l1(self):
        ctx = CompactCtx(mode="terse", cmd="apt list", strip_banners=False)
        text = "Reading package lists... Done\nfoo\n"
        result = compact(text, ctx)
        assert "Reading package lists" in result.text
        assert "L1" not in result.applied_layers
        assert "L2" in result.applied_layers


# ---------------------------------------------------------------------
# L1 banner registry — positive + negative per builtin
# ---------------------------------------------------------------------

class TestL1Banners:
    """Each shipped banner needs a positive AND negative test case.

    Add new entries to BUILTIN_BANNERS only with both kinds of coverage
    in this class — otherwise a subtly-too-greedy regex silently strips
    real content.
    """

    @pytest.fixture
    def ctx(self):
        return CompactCtx(mode="terse", cmd="any")

    # smartctl banner
    def test_strips_smartctl_version_line(self, ctx):
        text = "smartctl 7.3 2022-02-28 r5338 [x86_64-linux]\nfoo\n"
        out, fired = apply_l1_hygiene(text, ctx)
        assert "smartctl 7.3" not in out
        assert "foo" in out
        assert "smartctl-version" in fired

    def test_does_not_strip_similar_looking_smartctl_line(self, ctx):
        text = "this is smartctl-related but not the banner\n"
        out, _ = apply_l1_hygiene(text, ctx)
        assert "smartctl-related" in out

    def test_strips_smartmontools_copyright(self, ctx):
        text = "Copyright (C) 2002-22, Bruce Allen, smartmontools.org\nbody\n"
        out, fired = apply_l1_hygiene(text, ctx)
        assert "Copyright" not in out
        assert "smartmontools-copy" in fired

    def test_strips_smartctl_section(self, ctx):
        text = "=== START OF INFORMATION SECTION ===\nname=value\n"
        out, fired = apply_l1_hygiene(text, ctx)
        assert "START OF INFORMATION SECTION" not in out
        assert "smartctl-section" in fired

    # apt banners
    @pytest.mark.parametrize("line,banner_name", [
        ("Reading package lists... Done", "apt-reading-pkgs"),
        ("Building dependency tree... Done", "apt-building-tree"),
        ("Reading state information... Done", "apt-reading-state"),
        ("WARNING: apt does not have a stable CLI interface. Use with caution in scripts.",
         "apt-warn-cli"),
        ("Listing... Done", "apt-listing"),
    ])
    def test_strips_apt_banners(self, ctx, line, banner_name):
        text = f"{line}\nfoo\n"
        out, fired = apply_l1_hygiene(text, ctx)
        assert line not in out
        assert banner_name in fired

    def test_does_not_strip_apt_in_content(self, ctx):
        text = "we installed apt today\n"
        out, _ = apply_l1_hygiene(text, ctx)
        assert "we installed apt today" in out

    # journal / last / lastlog
    def test_strips_journal_no_entries(self, ctx):
        text = "-- No entries --\n"
        out, fired = apply_l1_hygiene(text, ctx)
        assert out == "" or out.strip() == ""
        assert "journal-no-entries" in fired

    def test_strips_wtmp_begins(self, ctx):
        text = "user1 pts/0 ...\nwtmp begins Sun Jan  1 00:00:00 2023\n"
        out, fired = apply_l1_hygiene(text, ctx)
        assert "wtmp begins" not in out
        assert "user1" in out
        assert "wtmp-begins" in fired

    def test_does_not_strip_wtmp_mention_inline(self, ctx):
        text = "the wtmp file is corrupted\n"
        out, _ = apply_l1_hygiene(text, ctx)
        assert "wtmp file is corrupted" in out

    def test_strips_lastlog_never(self, ctx):
        text = "alice  pts/0 ...\nroot     **Never logged in**\nbob   pts/1 ...\n"
        out, fired = apply_l1_hygiene(text, ctx)
        assert "Never logged in" not in out
        assert "alice" in out and "bob" in out
        assert "lastlog-never" in fired

    # systemctl legend
    def test_strips_systemctl_legend(self, ctx):
        text = (
            "foo.service loaded active running Some thing\n"
            "LOAD   = Reflects whether the unit definition was properly loaded.\n"
            "ACTIVE = The high-level unit activation state.\n"
            "SUB    = The low-level unit activation state.\n"
            "320 loaded units listed. Pass --all to see loaded but inactive.\n"
        )
        out, fired = apply_l1_hygiene(text, ctx)
        assert "foo.service" in out
        assert "Reflects whether" not in out
        assert "The high-level" not in out
        assert "The low-level" not in out
        assert "loaded units listed" not in out
        # all four legend banners should have fired
        for name in (
            "systemctl-legend-load",
            "systemctl-legend-active",
            "systemctl-legend-sub",
            "systemctl-listed",
        ):
            assert name in fired


# ---------------------------------------------------------------------
# L1 exit-code line strip
# ---------------------------------------------------------------------

class TestL1ExitCode:
    def test_strips_zero_exit(self):
        ctx = CompactCtx(mode="terse")
        text = "hello\n[exit:0]\n"
        out, fired = apply_l1_hygiene(text, ctx)
        assert "[exit:0]" not in out
        assert "hello" in out
        assert "exit-code-line" in fired

    def test_strips_nonzero_exit(self):
        ctx = CompactCtx(mode="terse")
        text = "boom\n[exit:127]\n"
        out, _ = apply_l1_hygiene(text, ctx)
        assert "[exit:127]" not in out

    def test_strips_negative_exit(self):
        ctx = CompactCtx(mode="terse")
        text = "x\n[exit:-1]\n"
        out, _ = apply_l1_hygiene(text, ctx)
        assert "[exit:-1]" not in out

    def test_does_not_strip_inline_exit_mention(self):
        ctx = CompactCtx(mode="terse")
        text = "the process exit code was [exit:0] when run\n"
        out, _ = apply_l1_hygiene(text, ctx)
        # Whole-line anchor means this doesn't match.
        assert "the process exit code was" in out


# ---------------------------------------------------------------------
# L1 boundary prompt elision
# ---------------------------------------------------------------------

class TestL1BoundaryPrompts:
    def test_strips_leading_prompt(self):
        ctx = CompactCtx(mode="terse", prompt_patterns=[re.compile(r"\$\s*$")])
        text = "$\nactual output\n"
        out, fired = apply_l1_hygiene(text, ctx)
        assert "$" not in out.split("\n")[0]
        assert "actual output" in out
        assert "boundary-prompt" in fired

    def test_strips_trailing_prompt(self):
        ctx = CompactCtx(mode="terse", prompt_patterns=[re.compile(r"\$\s*$")])
        text = "actual output\n$\n"
        out, _ = apply_l1_hygiene(text, ctx)
        assert "actual output" in out
        # The trailing prompt line should be gone
        lines = [l for l in out.split("\n") if l.strip()]
        assert not (lines and lines[-1].strip() == "$")

    def test_does_not_strip_interior_prompt(self):
        """Interior `>>>` line inside a Python REPL transcript is content."""
        ctx = CompactCtx(mode="terse", prompt_patterns=[re.compile(r">>>\s*$")])
        text = ">>>\n2 + 2\n4\n>>>\nfoo()\n"
        out, _ = apply_l1_hygiene(text, ctx)
        # The middle >>> line must remain
        lines = out.split("\n")
        body_prompts = [l for l in lines[1:-1] if l.strip() == ">>>"]
        assert len(body_prompts) >= 1


# ---------------------------------------------------------------------
# L1 command-echo elision
# ---------------------------------------------------------------------

class TestL1CommandEcho:
    def test_strips_matching_first_line_echo(self):
        ctx = CompactCtx(mode="terse", cleaned_cmd_echo="ls -la /tmp")
        text = "ls -la /tmp\ndrwx... foo\n"
        out, fired = apply_l1_hygiene(text, ctx)
        assert not out.startswith("ls -la /tmp")
        assert "drwx" in out
        assert "cmd-echo" in fired

    def test_does_not_strip_non_matching_first_line(self):
        ctx = CompactCtx(mode="terse", cleaned_cmd_echo="ls -la /tmp")
        text = "different first line\nls -la /tmp\n"
        out, _ = apply_l1_hygiene(text, ctx)
        # Echo doesn't appear at the first non-blank line, so it stays
        assert "ls -la /tmp" in out


# ---------------------------------------------------------------------
# L1 must-keep override
# ---------------------------------------------------------------------

class TestL1MustKeep:
    def test_must_keep_blocks_banner_strip(self):
        ctx = CompactCtx(
            mode="terse",
            must_keep=[re.compile(r"Reading package")],
        )
        text = "Reading package lists... Done\nfoo\n"
        out, fired = apply_l1_hygiene(text, ctx)
        assert "Reading package lists" in out
        # Banner did not fire because must-keep won
        assert "apt-reading-pkgs" not in fired

    def test_must_keep_blocks_exit_code_strip(self):
        ctx = CompactCtx(
            mode="terse",
            must_keep=[re.compile(r"^\[exit:")],
        )
        text = "[exit:0]\n"
        out, _ = apply_l1_hygiene(text, ctx)
        assert "[exit:0]" in out


# ---------------------------------------------------------------------
# L2 whitespace
# ---------------------------------------------------------------------

class TestL2Whitespace:
    def test_collapses_blank_run_to_one(self):
        ctx = CompactCtx(mode="terse", cmd="echo")
        text = "a\n\n\n\nb\n"
        out = apply_l2_whitespace(text, ctx)
        # Exactly one blank between a and b
        assert out.split("\n") == ["a", "", "b"]

    def test_blank_run_of_one_unchanged(self):
        ctx = CompactCtx(mode="terse", cmd="echo")
        text = "a\n\nb\n"
        out = apply_l2_whitespace(text, ctx)
        assert out.split("\n") == ["a", "", "b"]

    def test_strips_trailing_whitespace(self):
        ctx = CompactCtx(mode="terse", cmd="echo")
        text = "hello    \nworld\t\n"
        out = apply_l2_whitespace(text, ctx)
        assert out == "hello\nworld"

    def test_strips_leading_and_trailing_blanks(self):
        ctx = CompactCtx(mode="terse", cmd="echo")
        text = "\n\nhello\n\n"
        out = apply_l2_whitespace(text, ctx)
        assert out == "hello"

    @pytest.mark.parametrize("cmd", [
        "python -c 'print(1)'",
        "python3 script.py",
        "ipython",
        "yq '.foo' file.yml",
        "diff a b",
        "git diff HEAD~1",
    ])
    def test_preserve_whitespace_allowlist_skips_l2(self, cmd):
        """Python REPL, YAML, diff: indentation is semantic, don't touch."""
        ctx = CompactCtx(mode="terse", cmd=cmd)
        text = "a\n\n\n\nb\n   indented\n"
        out = apply_l2_whitespace(text, ctx)
        # L2 was skipped: blank runs preserved, trailing whitespace untouched
        assert out == text

    def test_cat_yaml_file_preserves(self):
        ctx = CompactCtx(mode="terse", cmd="cat /etc/foo.yaml")
        text = "key: value\n  nested: 1\n\n\nlist:\n"
        out = apply_l2_whitespace(text, ctx)
        assert out == text

    def test_cat_plain_file_normalizes(self):
        ctx = CompactCtx(mode="terse", cmd="cat /etc/hosts")
        text = "127.0.0.1 localhost\n\n\n\n::1 ip6-localhost\n"
        out = apply_l2_whitespace(text, ctx)
        assert out.split("\n") == ["127.0.0.1 localhost", "", "::1 ip6-localhost"]

    def test_must_keep_preserves_trailing_ws_too(self):
        ctx = CompactCtx(
            mode="terse",
            cmd="echo",
            must_keep=[re.compile(r"preserve me")],
        )
        text = "preserve me   \nnormal   \n"
        out = apply_l2_whitespace(text, ctx)
        # The must-keep line keeps its trailing whitespace; the other line doesn't
        assert "preserve me   " in out
        assert "normal\n" in out + "\n"
        # And blank-counter resets across must-keep
        assert "normal   " not in out


# ---------------------------------------------------------------------
# Cross-cutting properties
# ---------------------------------------------------------------------

class TestCompactProperties:
    """Idempotency, byte-count, and the cross-cutting invariants from §10.3."""

    @pytest.mark.parametrize("text", [
        "",
        "single line\n",
        "Reading package lists... Done\nfoo\nbar\n",
        "a\n\n\n\n\nb\n",
        "smartctl 7.3 2022-02-28 r5338\n=== START OF INFORMATION SECTION ===\nfoo\n",
    ])
    def test_compact_is_idempotent_on_repeated_terse_calls(self, text):
        ctx = CompactCtx(mode="terse", cmd="any")
        first = compact(text, ctx).text
        second = compact(first, ctx).text
        assert first == second

    def test_compact_never_grows_input(self):
        """Terse output is never larger than the input."""
        ctx = CompactCtx(mode="terse", cmd="any")
        cases = [
            "",
            "x\n",
            "Reading package lists... Done\n" * 5,
            "line\n\n\n\n" * 10,
        ]
        for text in cases:
            result = compact(text, ctx)
            assert result.out_bytes <= result.in_bytes, f"grew on {text!r}"

    def test_raw_mode_byte_identical_to_input(self):
        """Raw mode is *exactly* identity — important for the
        TX_NO_COMPACT byte-baseline regression test."""
        ctx = CompactCtx(mode="raw", cmd="any")
        text = "anything\n\nat\nall\n   trailing  \n"
        assert compact(text, ctx).text == text

    def test_builtin_banner_list_has_no_duplicates(self):
        names = [n for n, _ in BUILTIN_BANNERS]
        assert len(names) == len(set(names)), f"duplicate banner name(s): {names}"

    def test_every_banner_has_unique_pattern_str(self):
        patterns = [p.pattern for _, p in BUILTIN_BANNERS]
        assert len(patterns) == len(set(patterns)), "duplicate banner regex"


# ---------------------------------------------------------------------
# L3 — Repeated-line collapse (RLE)
# ---------------------------------------------------------------------

class TestL3RLE:
    """Identical and near-identical line-run collapse."""

    @pytest.fixture
    def ctx(self):
        return CompactCtx(mode="terse", cmd="dmesg", repeat_threshold=3)

    def test_collapses_identical_run(self, ctx):
        """Use a long-enough repeated line that the marker overhead is
        worth eating. Very short repeated lines (e.g. 'x\\n') won't
        collapse because the marker would be larger than what's elided."""
        line = "repeated content of moderate length that is realistic"
        text = f"header\n{line}\n{line}\n{line}\n{line}\nfooter\n"
        out, n = apply_l3_rle(text, ctx)
        lines = out.split("\n")
        assert "header" in lines
        assert "footer" in lines
        assert lines.count(line) == 1
        assert any("× 3 identical lines elided" in l for l in lines)
        assert n == 3

    def test_does_not_collapse_short_repeated_when_marker_costs_more(self, ctx):
        """Short repeated lines stay as-is when the elision marker
        would be larger than the lines it elides."""
        text = "x\nx\nx\nx\nx\n"
        out, n = apply_l3_rle(text, ctx)
        assert out == text
        assert n == 0

    def test_does_not_collapse_below_threshold(self, ctx):
        text = "a\nx\nx\nb\n"
        out, n = apply_l3_rle(text, ctx)
        assert out == text
        assert n == 0

    def test_collapses_two_below_threshold_three(self):
        ctx = CompactCtx(mode="terse", cmd="any", repeat_threshold=2)
        line = "long enough that the elision marker fits inside one copy"
        text = f"a\n{line}\n{line}\nb\n"
        out, n = apply_l3_rle(text, ctx)
        # threshold=2 → 2 identical → keep 1, summary
        assert "× 1 identical lines elided" in out
        assert n == 1

    def test_collapses_near_identical_by_prefix(self, ctx):
        """Same prefix + same length bucket → near-identical."""
        text = "\n".join([
            "[2026-05-14 03:14:01] INFO processed item 1234",
            "[2026-05-14 03:14:02] INFO processed item 1235",
            "[2026-05-14 03:14:03] INFO processed item 1236",
            "[2026-05-14 03:14:04] INFO processed item 1237",
        ]) + "\n"
        out, n = apply_l3_rle(text, ctx)
        assert "[2026-05-14 03:14:01]" in out
        assert "× 3 similar lines elided" in out
        assert n == 3

    def test_does_not_collapse_distinct_lines(self, ctx):
        text = "alice\nbob\ncharlie\ndavid\n"
        out, n = apply_l3_rle(text, ctx)
        assert n == 0
        assert out == text

    def test_must_keep_breaks_run(self):
        ctx = CompactCtx(
            mode="terse", cmd="any", repeat_threshold=3,
            must_keep=[re.compile(r"ERROR")],
        )
        noise = "noise content of realistic length here please"
        text = f"{noise}\n{noise}\nERROR: boom\n{noise}\n{noise}\n{noise}\n"
        out, _ = apply_l3_rle(text, ctx)
        # ERROR survives verbatim
        assert "ERROR: boom" in out

    def test_must_keep_inside_run_blocks_collapse(self):
        """A must-keep line in the middle of an identical run must NOT
        be summarised away."""
        ctx = CompactCtx(
            mode="terse", cmd="any", repeat_threshold=2,
            must_keep=[re.compile(r"NOISE-2")],
        )
        noise = "NOISE content of realistic length here please"
        keep = "NOISE-2 content of realistic length here pls"
        text = f"{noise}\n{noise}\n{noise}\n{keep}\n{noise}\n{noise}\n{noise}\n"
        out, _ = apply_l3_rle(text, ctx)
        assert keep in out

    def test_disabled_when_collapse_repeats_false(self):
        ctx = CompactCtx(mode="terse", cmd="any", collapse_repeats=False)
        line = "long enough line for collapse to potentially fire"
        text = f"{line}\n" * 5
        out, n = apply_l3_rle(text, ctx)
        assert n == 0
        assert out == text

    def test_disabled_when_threshold_is_one(self):
        ctx = CompactCtx(mode="terse", cmd="any", repeat_threshold=1)
        line = "long enough line for collapse to potentially fire"
        text = f"{line}\n" * 5
        out, n = apply_l3_rle(text, ctx)
        assert n == 0

    def test_preserve_whitespace_cmd_skips_rle(self):
        ctx = CompactCtx(mode="terse", cmd="python -c 'x=1'", repeat_threshold=3)
        line = "long enough line for collapse to potentially fire"
        text = f"{line}\n" * 5
        out, n = apply_l3_rle(text, ctx)
        assert n == 0
        assert out == text

    def test_idempotent_on_second_call(self, ctx):
        line = "long enough line for collapse to fire repeatedly here"
        text = f"{line}\n" * 10
        first, _ = apply_l3_rle(text, ctx)
        second, _ = apply_l3_rle(first, ctx)
        assert first == second

    def test_huge_run(self, ctx):
        # Long-enough line so collapse fires
        line = "spam content of realistic length to make collapse worthwhile"
        text = f"{line}\n" * 1000
        out, n = apply_l3_rle(text, ctx)
        # One sample + one marker should be small relative to 1000 lines
        assert len(out) < 200
        assert n == 999

    def test_l3_in_compact_pipeline(self):
        ctx = CompactCtx(mode="terse", cmd="dmesg", repeat_threshold=3)
        line = "long enough line for collapse to fire in pipeline test"
        text = f"{line}\n" * 5
        result = compact(text, ctx)
        assert "L3" in result.applied_layers
        assert result.out_bytes < result.in_bytes
        assert any("L3 collapsed" in n for n in result.notes)


# ---------------------------------------------------------------------
# Tier footer behaviour
# ---------------------------------------------------------------------

class TestFooter:
    def test_silent_footer_in_full_with_no_layers(self):
        """Raw mode → no layers → footer is None."""
        ctx = CompactCtx(mode="raw", cmd="any")
        result = compact("hello\n", ctx)
        assert result.footer is None

    def test_footer_present_when_savings(self):
        """Footer is emitted when L1+L2 save enough bytes that the
        footer itself is dwarfed by the savings (otherwise emitting the
        footer would net-grow the output)."""
        ctx = CompactCtx(mode="terse", cmd="any", strip_banners=True)
        # Big banner block → large savings.
        text = (
            "Reading package lists... Done\n"
            "Building dependency tree... Done\n"
            "Reading state information... Done\n"
            "WARNING: apt does not have a stable CLI interface. Use with caution in scripts.\n"
            "WARNING: apt does not have a stable CLI interface. Use with caution in scripts.\n"
            "actual content here\n"
        )
        result = compact(text, ctx)
        assert result.footer is not None, f"no footer for in={result.in_bytes}B out={result.out_bytes}B"
        assert "tier=full" in result.footer
        assert "in=" in result.footer and "out=" in result.footer

    def test_footer_verbose_emits_even_with_no_savings(self):
        ctx = CompactCtx(mode="terse", cmd="any", verbose=True)
        # input with no banners and no blank-runs
        result = compact("hello\n", ctx)
        assert result.footer is not None
        assert "tier=full" in result.footer


# ---------------------------------------------------------------------
# L4 — token-budget truncation
# ---------------------------------------------------------------------

class TestL4Budget:
    def test_no_budget_is_identity(self):
        ctx = CompactCtx(mode="terse", cmd="any", token_budget=None)
        text = "line\n" * 100
        d = apply_l4_budget(text, ctx)
        assert d.elided is False
        assert d.text == text

    def test_under_budget_is_identity(self):
        ctx = CompactCtx(mode="terse", cmd="any", token_budget=10_000)
        text = "short output\n"
        d = apply_l4_budget(text, ctx)
        assert d.elided is False
        assert d.text == text

    @staticmethod
    def _unique_lines(n: int) -> str:
        """200 lines with content that defeats L3 near-identical
        fingerprinting (varied first words so prefix differs)."""
        # Cycle word lists so the prefix varies meaningfully.
        first = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
                 "golf", "hotel", "india", "juliet"]
        second = ["apple", "banana", "cherry", "date", "fig", "grape",
                  "honeydew", "kiwi", "lemon", "mango"]
        third = ["red", "orange", "yellow", "green", "blue", "indigo", "violet"]
        return "\n".join(
            f"{first[i % len(first)]} {second[(i*3) % len(second)]} "
            f"{third[(i*7) % len(third)]} item id-{i:04d} of content"
            for i in range(n)
        ) + "\n"

    def test_truncates_when_overflow(self):
        ctx = CompactCtx(mode="terse", cmd="any", token_budget=80,
                         pane="p1", run_id="r-abc")
        text = self._unique_lines(200)
        d = apply_l4_budget(text, ctx, handle_placeholder=HANDLE_PLACEHOLDER)
        assert d.elided is True
        assert d.raw_lines == 201
        assert d.head_lines >= 1
        assert d.tail_lines >= 1
        assert HANDLE_PLACEHOLDER in d.text
        assert "tx output p1 r-abc" in d.text
        # First and last lines kept
        assert "id-0000" in d.text
        assert "id-0199" in d.text
        # Middle is gone
        assert "id-0100" not in d.text

    def test_marker_includes_elided_range(self):
        ctx = CompactCtx(mode="terse", cmd="any", token_budget=80,
                         pane="p1", run_id="r-abc")
        text = self._unique_lines(200)
        d = apply_l4_budget(text, ctx)
        assert "elided_lines=" in d.text
        assert "--range" in d.text
        # Default placeholder is in the marker
        assert "HANDLE-PLACEHOLDER" in d.text

    def test_head_fraction_one_keeps_only_head(self):
        ctx = CompactCtx(mode="terse", cmd="any", token_budget=80,
                         pane="p1", run_id="r-abc")
        text = self._unique_lines(200)
        d = apply_l4_budget(text, ctx, head_fraction=1.0)
        # Last content line should NOT be in output
        assert "id-0199" not in d.text
        # First should be
        assert "id-0000" in d.text

    def test_in_compact_pipeline(self):
        ctx = CompactCtx(mode="terse", cmd="any", token_budget=80,
                         pane="p1", run_id="r-abc")
        text = self._unique_lines(200)
        result = compact(text, ctx)
        assert "L4" in result.applied_layers
        l4 = getattr(result, "l4", None)
        assert l4 is not None
        assert l4.elided is True
        assert HANDLE_PLACEHOLDER in result.text


# ---------------------------------------------------------------------
# Handle store
# ---------------------------------------------------------------------

class TestHandleStore:
    def test_allocate_and_find(self):
        state: dict = {}
        hid = handle_store.store_handle(
            state, kind="run", run_id="r-1",
            log_path="/p1.log", start_offset=0, end_offset=1000,
            applied_layers=["L1", "L2", "L4"], raw_lines=500,
        )
        assert hid.startswith("h-")
        rec = handle_store.find_handle(state, hid)
        assert rec is not None
        assert rec["run_id"] == "r-1"
        assert rec["start_offset"] == 0
        assert rec["end_offset"] == 1000

    def test_unknown_handle_returns_none(self):
        state: dict = {"compact": {"handles": {}}}
        assert handle_store.find_handle(state, "h-nope") is None

    def test_max_handles_evicts_oldest(self):
        state: dict = {}
        ids = []
        for _ in range(7):
            ids.append(handle_store.store_handle(
                state, kind="run", run_id="r-x",
                log_path="/p.log", start_offset=0, end_offset=1,
                applied_layers=[], max_handles=5,
            ))
        handles = state["compact"]["handles"]
        assert len(handles) == 5
        # Earlier ids should have been evicted
        assert ids[0] not in handles
        assert ids[-1] in handles

    def test_gc_drops_rotated_runs(self):
        state: dict = {}
        h_live = handle_store.store_handle(
            state, kind="run", run_id="r-live",
            log_path="/p.log", start_offset=0, end_offset=1,
            applied_layers=[],
        )
        h_dead = handle_store.store_handle(
            state, kind="run", run_id="r-dead",
            log_path="/p.log", start_offset=0, end_offset=1,
            applied_layers=[],
        )
        n = handle_store.gc_handles_for_rotated_runs(state, {"r-live"})
        assert n == 1
        assert h_live in state["compact"]["handles"]
        assert h_dead not in state["compact"]["handles"]

    def test_gc_keeps_buffer_handles(self):
        state: dict = {}
        h_buf = handle_store.store_handle(
            state, kind="buffer", run_id=None,
            log_path="/p.log", start_offset=0, end_offset=1,
            applied_layers=[],
        )
        # Even with empty live_run_ids, buffer handles are preserved
        n = handle_store.gc_handles_for_rotated_runs(state, set())
        assert n == 0
        assert h_buf in state["compact"]["handles"]
