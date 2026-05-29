"""Click root group and main entry point.

Owns the `TxGroup` (which routes `tx --help` to the curated HELP_TEXT)
and the `cli()` root group every command decorates with `@cli.command()`.
Importing `tx_core.commands` is required for the side effect of
registering every command — without it, `cli()` would dispatch nothing.
"""

from __future__ import annotations

import click

from tx_core.help_text import HELP_TEXT


class TxGroup(click.Group):
    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write(HELP_TEXT)
        formatter.write("\n")


@click.group(cls=TxGroup)
def cli() -> None:
    pass


# Importing this package registers every `cmd_*` via its `@cli.command()`
# decorator. Must happen after `cli` is defined.
import tx_core.commands  # noqa: E402, F401


def main() -> None:
    cli()
