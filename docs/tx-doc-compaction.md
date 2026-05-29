# tx — output compaction

Read this when you need to:
- Control how `tx` compacts agent-facing output (`--raw`/`--terse`/`--token-budget`).
- Retrieve content that L4 elided (`--handle h-XXX` + `--range/--grep/--full`).
- Inspect or tune the per-tool normalizer registry.
- Read telemetry (`tx compact-stats`).

The compaction pipeline is a pure function (`tx_compact/`); see the module
docstrings and `tests/test_compact_layers.py` for the protocol-level contract
when authoring new normalizers.

## Mental model

```
log bytes → ANSI strip → marker strip → redact
              ↓
       [ normalizer (per-tool) ]      ← P4+; happy-path collapse
              ↓
       L1 hygiene (banner registry)   ← strip apt/journal/smartctl banners
              ↓
       L2 whitespace (≤1 blank run)
              ↓
       L3 RLE (collapse identical / near-identical line runs)
              ↓
       L4 budget (head + tail around an elision marker, emits handle)
              ↓
       L5 cross-call dedup (disabled by default; opt-in)
              ↓
       click.echo → agent
```

Each layer can be disabled per-call. The on-disk log is **never**
rewritten — what the human sees in the pane is unchanged. Compaction
is a read-time render transform only.

## Modes

`[compact] default_mode` in `~/.tx/config.toml`:
- `terse` — L1+L2+L3+L4 + normalizer dispatch. (**Ships as the default in v1.5.0.**)
- `raw` — identity. No layers fire. Use `--raw` for a per-call escape hatch.
- `summary` — synonym for `terse` plus prefer normalizer match_output happy-path.

Per-call override: `--raw` / `--terse`. Per-pane override:
`offsets.json::<pane>.compact.mode`. Resolution order: per-call > per-pane > global.

## Per-call flags (every output command)

| Flag | Effect |
|---|---|
| `--raw` | bypass *all* layers. Escape hatch. |
| `--terse` | force compaction even if config or pane state says raw. |
| `--token-budget N` | override L4 cap. Default 4000. |
| `--no-strip-banners` | skip L1 banner registry. |
| `--no-collapse-repeats` | skip L3 RLE. |
| `--no-normalize` | skip the per-tool normalizer; L1-L5 still run. |

## When compaction strips info you need

The compaction stage is correct >99% of the time, but normalizers are
opinionated. They drop columns and rows the typical agent doesn't need
(`pkts`/`bytes` in `iptables`, `LOAD` in `systemctl list-units`, ANSI
banners in `apt`, etc.). If the *specific* column or line you need was
elided:

1. **Recover via the handle** (L4 truncation only) — the elided middle
   is on disk. Pull the slice you need:
   ```
   tx output <pane> --handle h-XXX --range 100-150     # 0-based lines
   tx output <pane> --handle h-XXX --grep "ERROR"      # ±3 context
   tx output <pane> --handle h-XXX --full              # everything, raw
   ```

2. **Re-run with the per-tool normalizer disabled** (L1-L5 still fire):
   ```
   tx run --no-normalize <pane> "iptables -L -n -v"
   ```

3. **Re-run with all compaction off** — last resort, costs tokens:
   ```
   tx run --raw <pane> "smartctl -A /dev/sda"
   ```

4. **Disable per-pane** if a normalizer is consistently wrong for a
   specific tool on a specific pane:
   ```
   tx config <pane> compact.disabled_normalizers=["smartctl-attrs"]
   ```

5. **File a bug** (or open an issue on the normalizer's file) when
   step 4 keeps happening — the normalizer needs a `keep_lines_matching`
   or `match_output unless` addition. The footer's `tier=passthrough`
   or `tier=degraded` is the canonical signal that the agent should
   double-check rather than trust the compacted form.

**Do not** default to `--raw` to "be safe". The agent's effective
context is much larger with compaction on, and the handle protocol
makes elision reversible. Use `--raw` only for the *specific* call
where you've identified missing content.

## Env-var kill switches

- `TX_NO_COMPACT=1` — short-circuits at the entry point; byte-identical to `--raw`.
- `TX_NO_TELEMETRY=1` — disables compact.jsonl writes (compaction still runs).
- `TX_COMPACT_DEBUG=1` — every compacted response prepends a diagnostic block.
- `TX_DEBUG=1` — `CompactCtx.verbose = True` → emit footer even on no-savings cases.

## Handle protocol (L4)

When L4 elides content, the body contains:

```
<head lines>
[tx:elided run=r-XXX raw_lines=1200 elided_lines=850 ~3400tok handle=h-1a2b]
[retrieve: tx output <pane> r-XXX --handle h-1a2b --range 30-880   (or --grep PAT / --full)]
<tail lines>
```

Retrieve the elided content with one of:

```
tx output <pane> --handle h-1a2b --full                  # full original, no compaction
tx output <pane> --handle h-1a2b --range 100-150         # 0-based line slice
tx output <pane> --handle h-1a2b --grep "ERROR"          # matches + ±3 context lines
                  --grep-context N                       # adjust context
```

Handles persist in `offsets.json` for the life of the run record
(`max_run_history`, default 100). When the run rotates out, its handle
is GC'd; `tx output --handle` errors with "handle expired; use `--full`".

Buffer-handles (prefix `b-`) are issued by `tx tail` / `tx dump` /
`tx wait` / `tx grep` / `tx stream` — same lifecycle, separate prefix.

## Footer

Every compacted response that *meaningfully* compressed appends one line:

```
[tx:compact tier=full layers=L1,L2,L3 in=14000B out=320B saved=98%]
```

Tier ∈ {full, degraded, passthrough}. The footer is suppressed when:
- raw mode / no layers fired,
- the footer's own byte cost would exceed savings (tiny outputs).

Tier 2 (`degraded`) / Tier 3 (`passthrough`) footers are always emitted —
they convey diagnostic info the agent needs.

## Normalizers (per-tool)

Shipped (18): `ss`, `ps`, `df`, `du`, `last`, `find`, `apt-list`,
`smartctl-health`, `smartctl-attrs`, `iptables`, `virsh-list`,
`systemctl-status`, `systemctl-list-units` (TOML),
`zpool-status`, `lsblk`, `docker-ps`, `dmesg`, `journalctl` (Python).

**Lookup precedence** (highest to lowest):
1. `~/.tx/plugins/*.py`  (user Python plugins)
2. `~/.tx/filters/*.toml` (user TOML filters)
3. `tx_compact/builtin_plugins/*.py`
4. `tx_compact/builtin_filters/*.toml`

A user normalizer shadows a builtin by **name**, not just by command-match.

**Pipeline rejection**: a command containing `| ; & > <` never matches —
the user has already chosen a representation. `zpool status | grep ONLINE`
runs unmodified.

**Disable**: `tx run --no-normalize` (per-call) or
`offsets.json::<pane>.compact.disabled_normalizers = ["smartctl-attrs"]`.

**Plugin trust**: in-process, two-strike auto-disable. A plugin that
raises twice consecutively is disabled for the rest of the `tx` process
lifetime with a stderr warning naming the file.

Sample outputs for every normalizer: `compaction_samples.md`
(headline: 49.9% saved across the canonical set).

## L5 cross-call dedup (default disabled)

When `[compact.dedup] enabled = true`:

1. After L1-L4, SHA-256-truncated (12 hex) the emitted text.
2. Look up in the pane's bounded cache (`cache_size_per_pane`, default 32).
3. On hit within `ttl_seconds` (default 60) **in the same pane**,
   replace the body with `[tx:same-as r-XXX emitted Ns ago handle=h-YYY]`.

The handle is re-used so the agent can still recover the full content.
Never cross-pane. Opt in only after `tx compact-stats --dedup-would-hit`
(P5 telemetry) shows the hit rate is worth the staleness risk.

## Telemetry — `tx compact-stats`

Per-call records at `~/.tx/compact.jsonl` (rotates at 10MB). Privacy:
only `shlex.split(cmd)[0]` is recorded (with special handling for
`sudo X` and `env K=V X` prefixes); arguments, paths, values never hit
disk. Disable with `TX_NO_TELEMETRY=1`.

```
tx compact-stats                    # 7-day summary
tx compact-stats --weak             # cmd_heads with <30% savings
tx compact-stats --passthrough      # top tier-3 cmd_heads (missing normalizer)
tx compact-stats --since 2026-05-14T00:00:00Z
tx compact-stats --json
tx compact-stats --forget           # wipe the jsonl + backup
```

Use `--weak` / `--passthrough` to find candidates for a new
normalizer. Use `--json` for programmatic analysis.

## Authoring a new normalizer

TOML (line-based filter): drop a file in `~/.tx/filters/<name>.toml`:

```toml
schema_version = 1

[filters.my-tool]
description    = "..."
match_command  = "^my-tool\\b"
replace        = [ { pattern = "...", with = "..." } ]
match_output   = [ { pattern = "OK", message = "ok", unless = "ERROR" } ]
strip_lines_matching = ["^banner"]
keep_lines_matching  = []
truncate_lines_at    = 120
head_lines = 5
tail_lines = 10
max_lines  = 60
on_empty   = "(no output)"
min_savings_pct = 30

[[tests.my-tool]]
name     = "happy path"
input    = '''...'''
expected = "ok"
```

Python plugin (structural parsing): drop a file in `~/.tx/plugins/<name>.py`:

```python
SCHEMA_VERSION = 1
NAME = "my-tool"
MATCH_COMMAND = r"^my-tool\b"

from tx_compact.api import NormalizeResult

def normalize(text, ctx):
    # ctx.cmd, ctx.pane, ctx.run_id available
    if "OK" in text:
        return NormalizeResult.full("ok")
    return NormalizeResult.passthrough(text)
```

Restart `tx` (or run `from tx_compact.registry import load_registry;
load_registry(refresh=True)`) to pick up changes. Inline TOML
`[[tests.<name>]]` blocks are auto-discovered by `tests/test_compact_normalizer_engines.py`.
