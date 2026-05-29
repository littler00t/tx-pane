"""Package of click command handlers.

Each submodule defines a group of `cmd_*` functions decorated with
`@cli.command()` from `tx_core.cli`. Importing this package imports
every submodule for its decorator side effect — that is how `cli()`
learns about each subcommand.
"""

from __future__ import annotations

from tx_core.commands import admin  # noqa: F401
from tx_core.commands import input as _input  # noqa: F401
from tx_core.commands import panes  # noqa: F401
from tx_core.commands import read  # noqa: F401
from tx_core.commands import run  # noqa: F401
