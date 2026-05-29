"""tx_core — modular core of the `tx-pane` tmux pane controller.

This package is being incrementally extracted from the monolithic `tx-pane`
script. Each submodule owns a focused concern; see `nested-forging-puffin.md`
for the full split plan. The public entry point will land at `tx_core.cli:main`
once the CLI module is extracted.
"""
