"""Fixture file for tx-quality.yaml.

Each rule has positive and negative annotations; the negative examples
mirror the canonical idiomatic form named in the rule's message.
"""

import click


# =====================================================================
# tx-redundant-or-none
# =====================================================================


def bad_get_or_none(state: dict) -> None:
    # ruleid: tx-redundant-or-none
    x = state.get("x") or None
    _ = x


def bad_get_default_or_none(state: dict) -> None:
    # ruleid: tx-redundant-or-none
    x = state.get("x", 0) or None
    _ = x


def good_plain_get(state: dict) -> None:
    # ok: tx-redundant-or-none
    x = state.get("x")
    _ = x


def good_or_default(state: dict) -> None:
    # ok: tx-redundant-or-none
    x = state.get("x") or 0
    _ = x


# =====================================================================
# tx-color-on-echo
# =====================================================================


def bad_echo_join_no_color(parts: list) -> None:
    # ruleid: tx-color-on-echo
    click.echo("\n".join(parts))


def good_echo_join_with_color(parts: list, keep_ansi_resolved: bool) -> None:
    # ok: tx-color-on-echo
    click.echo("\n".join(parts), color=keep_ansi_resolved or None)


def good_single_line_echo(s: str) -> None:
    # ok: tx-color-on-echo
    click.echo(s)
