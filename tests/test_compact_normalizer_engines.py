"""Tests for tx_compact normalizer engines:

- TOML engine: pipeline correctness + inline test discovery
- Plugin engine: import + 2-strike auto-disable + invoke surface
- Registry: discovery, precedence, dispatch

All pure-function — no tmux, no subprocess.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tx_compact import (  # noqa: E402
    NormalizeCtx,
    NormalizeResult,
    Tier,
    compact,
    CompactCtx,
)
from tx_compact import toml_engine, plugin_engine, registry  # noqa: E402


# ---------------------------------------------------------------------
# TOML engine
# ---------------------------------------------------------------------

class TestTomlPipeline:
    def _flt(self, body: str) -> toml_engine.TomlFilter:
        """Load a single filter from an inline TOML string."""
        return toml_engine.load_filter_file(self._write_tmp(body))[0]

    def _write_tmp(self, body: str, tmp_path: Path | None = None) -> Path:
        # Use pytest's tmp_path via class-level fixture trick.
        import tempfile
        p = Path(tempfile.mkstemp(suffix=".toml")[1])
        p.write_text(body)
        return p

    def test_loader_rejects_schema_mismatch(self):
        p = self._write_tmp("schema_version = 999\n[filters.x]\nmatch_command = \"^x\"\n")
        with pytest.raises(ValueError, match="schema_version"):
            toml_engine.load_filter_file(p)

    def test_strip_lines_removes_matching(self):
        flt = self._flt(textwrap.dedent('''
            schema_version = 1
            [filters.x]
            match_command = "^x"
            strip_lines_matching = ["^drop"]
        '''))
        r = toml_engine.apply_filter(flt, "drop me\nkeep me\ndrop again\n")
        assert "drop" not in r.text
        assert "keep me" in r.text

    def test_keep_lines_overrides_strip(self):
        flt = self._flt(textwrap.dedent('''
            schema_version = 1
            [filters.x]
            match_command = "^x"
            keep_lines_matching = ["^KEEP"]
            strip_lines_matching = ["^KEEP"]
        '''))
        r = toml_engine.apply_filter(flt, "KEEP a\nDROP b\nKEEP c\n")
        # keep wins → only KEEP rows
        assert "KEEP a" in r.text and "KEEP c" in r.text
        assert "DROP" not in r.text

    def test_truncate_lines_at(self):
        flt = self._flt(textwrap.dedent('''
            schema_version = 1
            [filters.x]
            match_command = "^x"
            truncate_lines_at = 10
        '''))
        r = toml_engine.apply_filter(flt, "abcdefghij_TAIL\n")
        # 10 chars + ellipsis
        assert "_TAIL" not in r.text
        assert "…" in r.text

    def test_head_tail_with_omit(self):
        flt = self._flt(textwrap.dedent('''
            schema_version = 1
            [filters.x]
            match_command = "^x"
            head_lines = 2
            tail_lines = 2
        '''))
        # No trailing \n so split() doesn't add an empty last element
        # that would steal tail-slot space.
        text = "\n".join(f"line{i}" for i in range(10))
        r = toml_engine.apply_filter(flt, text)
        assert r.tier == Tier.DEGRADED
        assert "line0" in r.text and "line1" in r.text
        assert "line8" in r.text and "line9" in r.text
        assert "line5" not in r.text
        assert "omitted" in r.text

    def test_max_lines_absolute(self):
        flt = self._flt(textwrap.dedent('''
            schema_version = 1
            [filters.x]
            match_command = "^x"
            max_lines = 5
        '''))
        text = "\n".join(f"line{i}" for i in range(20)) + "\n"
        r = toml_engine.apply_filter(flt, text)
        # First 4 lines + 1 omit marker = 5 total
        lines = r.text.split("\n")
        assert len(lines) == 5
        assert "omitted" in lines[-1]

    def test_match_output_short_circuit(self):
        flt = self._flt(textwrap.dedent('''
            schema_version = 1
            [filters.x]
            match_command = "^x"
            match_output = [ { pattern = "ALL OK", message = "ok" } ]
        '''))
        r = toml_engine.apply_filter(flt, "garbage\nALL OK\nmore garbage\n")
        assert r.text == "ok"
        assert r.tier == Tier.FULL

    def test_match_output_unless_blocks_collapse(self):
        flt = self._flt(textwrap.dedent('''
            schema_version = 1
            [filters.x]
            match_command = "^x"
            match_output = [ { pattern = "ACTIVE", message = "ok", unless = "ERROR" } ]
        '''))
        r = toml_engine.apply_filter(flt, "ACTIVE running\nbut ERROR present\n")
        # unless matched → no collapse, fall through to default pipeline
        assert "ERROR" in r.text

    def test_pipeline_command_blocks_match(self):
        flt = self._flt(textwrap.dedent('''
            schema_version = 1
            [filters.x]
            match_command = "^x"
        '''))
        assert toml_engine.filter_matches_command(flt, "x foo") is True
        assert toml_engine.filter_matches_command(flt, "x foo | grep bar") is False
        assert toml_engine.filter_matches_command(flt, "x; y") is False

    @pytest.mark.parametrize("cmd,expected_pipeline", [
        # Stderr-only redirects do NOT count as pipelines — these are
        # the canonical agent invocations that should still get normalized.
        ("iptables -L 2>&1", False),
        ("find /etc 2>/dev/null", False),
        ("smartctl -A /dev/sda 2>&1", False),
        ("docker ps 2>&1", False),
        # Real pipelines block the normalizer.
        ("ss -tulnp | grep 22", True),
        ("ps auxf | head -10", True),
        # Conjunctions block.
        ("df -h && du -sh", True),
        ("apt update; apt list", True),
        # Stdout redirect to file blocks (output goes to disk, not tx).
        ("ls > out.txt", True),
        # Input redirect blocks.
        ("cat < input.txt", True),
        # Heredoc blocks.
        ("cat <<EOF\nx\nEOF", True),
    ])
    def test_pipeline_detection_redirects(self, cmd, expected_pipeline):
        assert toml_engine.is_pipeline_command(cmd) is expected_pipeline

    def test_replace_rule(self):
        flt = self._flt(textwrap.dedent('''
            schema_version = 1
            [filters.x]
            match_command = "^x"
            replace = [ { pattern = "secret", with = "[REDACTED]" } ]
        '''))
        r = toml_engine.apply_filter(flt, "the secret is out\n")
        assert "[REDACTED]" in r.text
        assert "secret" not in r.text

    def test_on_empty_fallback(self):
        flt = self._flt(textwrap.dedent('''
            schema_version = 1
            [filters.x]
            match_command = "^x"
            strip_lines_matching = [".*"]
            on_empty = "(no output)"
        '''))
        r = toml_engine.apply_filter(flt, "everything\ngets\nstripped\n")
        assert r.text == "(no output)"

    def test_failure_demotes_to_passthrough(self, monkeypatch):
        flt = self._flt(textwrap.dedent('''
            schema_version = 1
            [filters.x]
            match_command = "^x"
        '''))
        # Inject a fault by replacing replace[0].pattern with a broken object.
        def boom(*a, **kw):
            raise RuntimeError("boom")
        flt.replace = [type("E", (), {"pattern": type("P", (), {"sub": boom})()})()]
        flt.replace[0].replacement = ""
        r = toml_engine.apply_filter(flt, "anything\n")
        assert r.tier == Tier.PASSTHROUGH


# ---------------------------------------------------------------------
# Plugin engine
# ---------------------------------------------------------------------

class TestPluginEngine:
    def _make_plugin(self, tmp_path: Path, body: str, name: str = "tp") -> Path:
        p = tmp_path / f"{name}.py"
        p.write_text(textwrap.dedent(body))
        return p

    def test_discover_plugin(self, tmp_path):
        self._make_plugin(tmp_path, '''
            SCHEMA_VERSION = 1
            NAME = "tp"
            MATCH_COMMAND = r"^tp\\b"
            from tx_compact.api import NormalizeResult
            def normalize(text, ctx):
                return NormalizeResult.full(text.upper())
        ''')
        plugins = plugin_engine.discover_plugins(tmp_path)
        assert len(plugins) == 1
        assert plugins[0].name == "tp"

    def test_skip_invalid_plugin(self, tmp_path, capsys):
        # Missing MATCH_COMMAND
        self._make_plugin(tmp_path, '''
            SCHEMA_VERSION = 1
            NAME = "broken"
            def normalize(text, ctx):
                return None
        ''')
        plugins = plugin_engine.discover_plugins(tmp_path)
        assert plugins == []

    def test_invoke_success_resets_failures(self, tmp_path):
        self._make_plugin(tmp_path, '''
            SCHEMA_VERSION = 1
            NAME = "tp"
            MATCH_COMMAND = r"^tp"
            from tx_compact.api import NormalizeResult
            def normalize(text, ctx):
                return NormalizeResult.full("ok")
        ''')
        plugin = plugin_engine.discover_plugins(tmp_path)[0]
        plugin.consecutive_failures = 1
        r = plugin_engine.invoke_plugin(plugin, "x", NormalizeCtx(cmd="tp"))
        assert r.text == "ok"
        assert plugin.consecutive_failures == 0

    def test_invoke_failure_increments_and_auto_disables(self, tmp_path):
        self._make_plugin(tmp_path, '''
            SCHEMA_VERSION = 1
            NAME = "tp"
            MATCH_COMMAND = r"^tp"
            def normalize(text, ctx):
                raise RuntimeError("boom")
        ''')
        plugin = plugin_engine.discover_plugins(tmp_path)[0]
        captured: list[str] = []
        # First failure: passthrough + count = 1
        r1 = plugin_engine.invoke_plugin(plugin, "x", NormalizeCtx(cmd="tp"),
                                          failure_log=captured.append)
        assert r1.tier == Tier.PASSTHROUGH
        assert plugin.consecutive_failures == 1
        assert plugin.disabled is False
        # Second failure: auto-disable
        r2 = plugin_engine.invoke_plugin(plugin, "x", NormalizeCtx(cmd="tp"),
                                          failure_log=captured.append)
        assert r2.tier == Tier.PASSTHROUGH
        assert plugin.disabled is True
        # Subsequent invocations short-circuit to passthrough w/o calling the plugin
        r3 = plugin_engine.invoke_plugin(plugin, "x", NormalizeCtx(cmd="tp"),
                                          failure_log=captured.append)
        assert r3.tier == Tier.PASSTHROUGH


# ---------------------------------------------------------------------
# Inline tests embedded in shipped TOML filters
# ---------------------------------------------------------------------

class TestBuiltinInlineTests:
    """Every builtin .toml file ships ``[[tests.<name>]]`` blocks.

    This test discovers them and asserts each input→expected pair
    works against the filter that loaded it. New normalizers get
    test coverage automatically just by adding the inline blocks.
    """

    @pytest.fixture(scope="class")
    def builtins(self):
        reg = registry.load_registry(refresh=True)
        return reg.builtin_filters

    def test_at_least_some_builtins_loaded(self, builtins):
        # Sanity: we shipped non-zero builtin TOMLs.
        assert len(builtins) >= 5

    def test_inline_test_cases(self, builtins):
        """Each TOML's [[tests.<name>]] runs through apply_filter."""
        any_ran = False
        failures: list[str] = []
        for flt in builtins:
            for case in flt.inline_tests:
                any_ran = True
                name = case.get("name", "?")
                inp = case.get("input", "")
                expected = case.get("expected")
                expected_contains = case.get("expected_contains")
                r = toml_engine.apply_filter(flt, inp)
                if expected is not None:
                    if r.text != expected:
                        failures.append(
                            f"{flt.name}::{name}: expected {expected!r}, got {r.text!r}"
                        )
                if expected_contains:
                    missing = [s for s in expected_contains if s not in r.text]
                    if missing:
                        failures.append(
                            f"{flt.name}::{name}: missing {missing!r} in {r.text!r}"
                        )
        assert any_ran, "no inline tests discovered — registry empty?"
        assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------
# Registry precedence
# ---------------------------------------------------------------------

class TestRegistry:
    def test_pipeline_commands_never_match(self):
        reg = registry.load_registry(refresh=True)
        assert registry.find_normalizer(reg, "ss -tulnp | grep 22") is None
        assert registry.find_normalizer(reg, "ps; echo done") is None
        assert registry.find_normalizer(reg, "df > /tmp/x") is None

    def test_finds_builtin_filter(self):
        reg = registry.load_registry(refresh=True)
        n = registry.find_normalizer(reg, "ss -tulnp")
        assert n is not None
        assert registry.normalizer_name(n) == "ss"

    def test_finds_builtin_plugin(self):
        reg = registry.load_registry(refresh=True)
        n = registry.find_normalizer(reg, "zpool status tank")
        assert n is not None
        assert registry.normalizer_name(n) == "zpool-status"

    def test_disabled_names(self):
        reg = registry.load_registry(refresh=True)
        n = registry.find_normalizer(reg, "ss -tulnp")
        assert registry.is_normalizer_disabled(n, ["ss"]) is True
        assert registry.is_normalizer_disabled(n, ["*"]) is True
        assert registry.is_normalizer_disabled(n, []) is False
        assert registry.is_normalizer_disabled(n, ["other"]) is False


# ---------------------------------------------------------------------
# compact() integrates normalizer dispatch
# ---------------------------------------------------------------------

class TestCompactPipelineIntegration:
    def test_zpool_healthy_collapses_to_one_line(self):
        text = textwrap.dedent('''
              pool: tank
             state: ONLINE
              scan: scrub repaired 0B in 00:01:23 with 0 errors on Sat Apr 1 09:00:00 2026
            config:

                NAME        STATE     READ WRITE CKSUM
                tank        ONLINE       0     0     0

            errors: No known data errors
        ''').strip()
        ctx = CompactCtx(mode="terse", cmd="zpool status tank")
        result = compact(text, ctx)
        assert "zpool-status" in result.applied_layers
        assert result.text.startswith("zpool tank: ONLINE")
        assert "scrub clean" in result.text

    def test_zpool_degraded_falls_through(self):
        text = textwrap.dedent('''
              pool: tank
             state: DEGRADED
              scan: scrub in progress
            config:

                NAME        STATE     READ WRITE CKSUM
                tank        DEGRADED     0     0     0
                  mirror-0  DEGRADED     0     0     0

            errors: No known data errors
        ''').strip()
        ctx = CompactCtx(mode="terse", cmd="zpool status tank")
        result = compact(text, ctx)
        assert "DEGRADED" in result.text
        assert result.tier == Tier.DEGRADED

    def test_ss_filter_runs(self):
        text = (
            "Netid State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
            "tcp   LISTEN 0      128    0.0.0.0:22       0.0.0.0:*\n"
        )
        ctx = CompactCtx(mode="terse", cmd="ss -tulnp")
        result = compact(text, ctx)
        assert "ss" in result.applied_layers
        # Header dropped, listen row preserved (in some form)
        assert "Netid State" not in result.text
        assert "0.0.0.0:22" in result.text

    def test_disabled_normalizer_falls_back_to_layers(self):
        ctx = CompactCtx(mode="terse", cmd="ss -tulnp",
                         disabled_normalizers=["ss"])
        text = (
            "Netid State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
            "tcp   LISTEN 0      128    0.0.0.0:22       0.0.0.0:*\n"
        )
        result = compact(text, ctx)
        assert "ss" not in result.applied_layers

    def test_no_normalize_wildcard(self):
        ctx = CompactCtx(mode="terse", cmd="zpool status",
                         disabled_normalizers=["*"])
        text = "  pool: tank\n state: ONLINE\nerrors: No known data errors\n"
        result = compact(text, ctx)
        assert "zpool-status" not in result.applied_layers
