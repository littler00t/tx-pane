"""Normalizer registry — discovery, precedence, dispatch.

Precedence per design plan §5.6 + §5.5 rule 5 (resolved as
*engine-then-source*):

    user-plugin > user-toml > builtin-plugin > builtin-toml

This is the lookup order ``find_normalizer`` consults for a given
command string. First match wins. Pipeline commands (containing
``| ; & > <``) never match — the user has chosen a representation.

Per-call ``--no-normalize`` and per-pane ``disabled_normalizers`` are
honoured by the public ``normalize`` function.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

from .api import NormalizeCtx, NormalizeResult
from .tier import Tier
from .toml_engine import (
    TomlFilter,
    apply_filter,
    filter_matches_command,
    load_filter_file,
)
from .plugin_engine import (
    LoadedPlugin,
    discover_plugins,
    invoke_plugin,
    plugin_matches_command,
)


_PACKAGE_ROOT = Path(__file__).resolve().parent
BUILTIN_FILTERS_DIR = _PACKAGE_ROOT / "builtin_filters"
BUILTIN_PLUGINS_DIR = _PACKAGE_ROOT / "builtin_plugins"


def _user_dir(sub: str) -> Path:
    home = Path(os.environ.get("TX_HOME") or str(Path.home() / ".tx"))
    return home / sub


@dataclass
class Registry:
    user_plugins: list[LoadedPlugin] = field(default_factory=list)
    user_filters: list[TomlFilter] = field(default_factory=list)
    builtin_plugins: list[LoadedPlugin] = field(default_factory=list)
    builtin_filters: list[TomlFilter] = field(default_factory=list)

    def all_names(self) -> list[str]:
        return [
            *(p.name for p in self.user_plugins),
            *(f.name for f in self.user_filters),
            *(p.name for p in self.builtin_plugins),
            *(f.name for f in self.builtin_filters),
        ]


_cached: Registry | None = None


def load_registry(refresh: bool = False) -> Registry:
    """Discover all normalizers. Cached after the first call.

    Set ``refresh=True`` to force a re-scan (used in tests that mutate
    the filesystem and want to pick up changes).
    """
    global _cached
    if _cached is not None and not refresh:
        return _cached

    reg = Registry()

    # Order matters: user shadows builtin. We don't actually need to
    # check shadow names here — find_normalizer walks lists in the
    # precedence order so a user normalizer with the same name as a
    # builtin will win automatically.
    user_p_dir = _user_dir("plugins")
    user_f_dir = _user_dir("filters")

    reg.user_plugins = discover_plugins(user_p_dir)
    reg.builtin_plugins = discover_plugins(BUILTIN_PLUGINS_DIR)

    reg.user_filters = _load_filters_from_dir(user_f_dir)
    reg.builtin_filters = _load_filters_from_dir(BUILTIN_FILTERS_DIR)

    _cached = reg
    return reg


def _load_filters_from_dir(directory: Path) -> list[TomlFilter]:
    out: list[TomlFilter] = []
    if not directory.exists() or not directory.is_dir():
        return out
    for p in sorted(directory.glob("*.toml")):
        if p.name.startswith("_"):
            continue
        try:
            out.extend(load_filter_file(p))
        except Exception as e:  # pragma: no cover (defensive)
            import sys
            print(f"[tx_compact] skipping filter {p}: {type(e).__name__}: {e}",
                  file=sys.stderr)
    return out


# A union type for "anything that can normalize" — both engines share
# the (text, ctx) → NormalizeResult shape via their adapters.
Normalizer = Union[LoadedPlugin, TomlFilter]


def find_normalizer(reg: Registry, cmd: str) -> Normalizer | None:
    """Return the first normalizer that matches `cmd`, or None.

    Honours precedence: user-plugin > user-toml > builtin-plugin >
    builtin-toml. Pipeline commands never match.
    """
    for p in reg.user_plugins:
        if plugin_matches_command(p, cmd):
            return p
    for f in reg.user_filters:
        if filter_matches_command(f, cmd):
            return f
    for p in reg.builtin_plugins:
        if plugin_matches_command(p, cmd):
            return p
    for f in reg.builtin_filters:
        if filter_matches_command(f, cmd):
            return f
    return None


def normalizer_name(n: Normalizer) -> str:
    return n.name


def invoke(n: Normalizer, text: str, ctx: NormalizeCtx) -> NormalizeResult:
    """Dispatch to the appropriate engine."""
    if isinstance(n, LoadedPlugin):
        return invoke_plugin(n, text, ctx)
    return apply_filter(n, text, ctx)


def is_normalizer_disabled(
    n: Normalizer,
    disabled_names: list[str] | None,
) -> bool:
    """Honour per-call / per-pane disabled-name lists.

    ``["*"]`` disables all normalizers (used by --no-normalize).
    """
    if not disabled_names:
        return False
    if "*" in disabled_names:
        return True
    return n.name in disabled_names
