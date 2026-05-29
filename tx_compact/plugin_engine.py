"""Python plugin engine — load .py plugins from a directory.

Plugin shape (one file per normalizer per design §5.5 rule 1):

    # ~/.tx-pane/plugins/zpool_status.py
    SCHEMA_VERSION = 1
    NAME           = "zpool-status"
    MATCH_COMMAND  = r"^zpool\\s+status\\b"

    def normalize(text: str, ctx) -> NormalizeResult:
        ...

Trust + safety:
- Loaded in-process via importlib (per design Q2).
- A plugin that raises is auto-disabled after the second consecutive
  failure for the lifetime of the process. A stderr warning names the
  file and the exception type.
- No network / no subprocess from plugins is policy, not enforced —
  plugin authors must self-discipline. The engine here only enforces
  the call-path invariants.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Pattern

from .api import NormalizeCtx, NormalizeResult
from .tier import Tier


SCHEMA_VERSION = 1


@dataclass
class LoadedPlugin:
    name: str
    match_command: Pattern[str]
    normalize: Callable
    source_path: Path
    schema_version: int = 1
    # Operational state — kept on the plugin record so it survives across
    # multiple calls but resets with each process restart.
    consecutive_failures: int = 0
    disabled: bool = False


def discover_plugins(directory: Path) -> list[LoadedPlugin]:
    """Scan `directory` for .py files, load each, return LoadedPlugins.

    Files that fail to load (syntax error, missing attrs, bad regex)
    are skipped with a stderr warning rather than aborting the scan.
    """
    out: list[LoadedPlugin] = []
    if not directory.exists() or not directory.is_dir():
        return out
    for p in sorted(directory.glob("*.py")):
        if p.name.startswith("_"):
            continue
        try:
            plugin = _load_one(p)
            if plugin is not None:
                out.append(plugin)
        except Exception as e:
            print(f"[tx_compact] skipping plugin {p}: {type(e).__name__}: {e}",
                  file=sys.stderr)
    return out


def _load_one(path: Path) -> LoadedPlugin | None:
    """Load one plugin file. Returns None if the file is not a valid plugin."""
    mod_name = f"_tx_compact_plugin_{path.stem}"
    loader = importlib.machinery.SourceFileLoader(mod_name, str(path))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    if spec is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    loader.exec_module(mod)

    schema_v = getattr(mod, "SCHEMA_VERSION", 1)
    if schema_v != SCHEMA_VERSION:
        raise ValueError(f"unsupported SCHEMA_VERSION={schema_v}")

    name = getattr(mod, "NAME", path.stem)
    match_cmd_str = getattr(mod, "MATCH_COMMAND", None)
    if not match_cmd_str:
        raise ValueError("missing MATCH_COMMAND")
    match_cmd = re.compile(match_cmd_str)

    fn = getattr(mod, "normalize", None)
    if not callable(fn):
        raise ValueError("missing normalize(text, ctx)")

    return LoadedPlugin(
        name=name,
        match_command=match_cmd,
        normalize=fn,
        source_path=path,
        schema_version=schema_v,
    )


def plugin_matches_command(plugin: LoadedPlugin, cmd: str) -> bool:
    """Same pipeline-rejecting rule as the TOML engine (design §9.4)."""
    if plugin.disabled or not cmd:
        return False
    from .toml_engine import is_pipeline_command  # avoid import cycle at module load
    if is_pipeline_command(cmd):
        return False
    return bool(plugin.match_command.search(cmd))


def invoke_plugin(
    plugin: LoadedPlugin,
    text: str,
    ctx: NormalizeCtx,
    *,
    failure_log: Callable[[str], None] | None = None,
) -> NormalizeResult:
    """Call a plugin's normalize(); enforce the 2-strike auto-disable.

    On any exception:
        - return PASSTHROUGH with the original text
        - increment plugin.consecutive_failures
        - if it hits 2, set plugin.disabled = True (skipped on next match)
    On success:
        - reset consecutive_failures to 0
    """
    if plugin.disabled:
        return NormalizeResult.passthrough(text, reason=f"plugin {plugin.name} disabled")
    try:
        result = plugin.normalize(text, ctx)
        if not isinstance(result, NormalizeResult):
            raise TypeError(
                f"plugin returned {type(result).__name__}, expected NormalizeResult"
            )
        plugin.consecutive_failures = 0
        return result
    except Exception as e:
        plugin.consecutive_failures += 1
        msg = f"plugin {plugin.name} raised {type(e).__name__}: {e}"
        if failure_log:
            failure_log(msg)
        else:
            print(f"[tx_compact] {msg}", file=sys.stderr)
        if plugin.consecutive_failures >= 2:
            plugin.disabled = True
            disable_msg = (
                f"plugin {plugin.name} at {plugin.source_path} disabled after "
                f"{plugin.consecutive_failures} consecutive failures"
            )
            if failure_log:
                failure_log(disable_msg)
            else:
                print(f"[tx_compact] {disable_msg}", file=sys.stderr)
        return NormalizeResult.passthrough(text, reason=msg)
