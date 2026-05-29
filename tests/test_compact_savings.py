"""Token-savings regression test for builtin normalizers.

Each builtin TOML filter declares an optional ``min_savings_pct`` that
defines the expected minimum byte reduction against the filter's
inline test fixtures. If a future change degrades the savings below
the threshold for any filter, this test fails with a clear diff.

For filters without inline tests this test is a no-op (the filter is
covered indirectly via test_normalizer_real.py on Docker).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tx_compact import registry, toml_engine  # noqa: E402


def _saved_pct(inp: str, out: str) -> float:
    if not inp:
        return 0.0
    return 100.0 * max(0, len(inp) - len(out)) / len(inp)


def test_builtin_filters_meet_savings_threshold():
    """For every filter with min_savings_pct > 0 and at least one
    inline test case, assert savings ≥ threshold on the *largest*
    fixture (so a tiny "happy path" sample doesn't gerrymander the
    metric)."""
    reg = registry.load_registry(refresh=True)
    failures: list[str] = []
    checked = 0
    for flt in reg.builtin_filters:
        if flt.min_savings_pct <= 0:
            continue
        if not flt.inline_tests:
            continue
        # Find the largest input among test cases.
        biggest = max(
            (c for c in flt.inline_tests if c.get("input")),
            key=lambda c: len(c["input"]),
            default=None,
        )
        if biggest is None:
            continue
        inp = biggest["input"]
        result = toml_engine.apply_filter(flt, inp)
        saved = _saved_pct(inp, result.text)
        checked += 1
        if saved < flt.min_savings_pct:
            failures.append(
                f"{flt.name}: saved {saved:.1f}% (threshold {flt.min_savings_pct:.1f}%)"
                f"\n  input:  {inp[:80]!r}..."
                f"\n  output: {result.text[:80]!r}..."
            )
    assert checked > 0, "no builtin filters had a savings threshold to check"
    assert not failures, "\n".join(failures)


def test_builtin_filters_never_grow_input():
    """Stronger sanity: NO filter should produce output larger than its
    input on any fixture. This catches over-eager replace rules that
    add more than they remove."""
    reg = registry.load_registry(refresh=True)
    failures: list[str] = []
    for flt in reg.builtin_filters:
        for case in flt.inline_tests:
            inp = case.get("input", "")
            if not inp:
                continue
            result = toml_engine.apply_filter(flt, inp)
            if len(result.text) > len(inp) + 20:  # +20 slop for tier markers
                failures.append(
                    f"{flt.name}::{case.get('name','?')}: grew "
                    f"{len(inp)}B → {len(result.text)}B"
                )
    assert not failures, "\n".join(failures)


def test_normalizer_idempotency():
    """Running a normalizer on its own output should produce the same
    output. This is the rtk-lesson invariant — flaky tier transitions
    breed bug reports."""
    reg = registry.load_registry(refresh=True)
    for flt in reg.builtin_filters:
        for case in flt.inline_tests:
            inp = case.get("input", "")
            if not inp:
                continue
            once = toml_engine.apply_filter(flt, inp).text
            twice = toml_engine.apply_filter(flt, once).text
            assert once == twice, (
                f"{flt.name}::{case.get('name','?')} not idempotent"
            )
