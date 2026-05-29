## Terminal: `tx`

Use `tx` for all terminal interaction. Never invoke `tmux` directly. If
`tx` can't do what you need, stop and report — do not work around.

A pane is the unit of work. `tx new` returns an id (`p1`, `web-server`,
…); capture it once and re-use. State persists across `tx` invocations
in `~/.tx/`; there is no daemon.

### Decision table

| Need | Command |
|---|---|
| Create pane | `tx new [name] [--cwd <dir>]` → capture id |
| Run + wait for output | `tx run <pane> <cmd>` |
| Run async, wait later | `id=$(tx exec <pane> <cmd>)` then `tx wait-run <pane> $id` |
| Resolve "pane busy" error | `--queue` \| `--kill-and-run` \| `--stdin` (one of, no `--force`) |
| Early-exit on a pattern | `tx run --wait-for <re>` (exit 0) / `--fail-for <re>` (exit 1) |
| Cancel on timeout | `tx run --on-timeout cancel\|kill` |
| Send key / raw text | `tx key <pane> C-c Enter` / `tx send <pane> <text>` |
| Read new output | `tx tail <pane>` (advances offset); `--all` drains |
| Read fixed slice | `tx dump <pane> --tail N` / `--head N` (no offset change) |
| Grep output | `tx grep <pane> '<re>' -C 2` |
| Wait for a regex | `tx wait <pane> <re>` |
| Bounded capture (`tail -f`-like) | `tx stream <pane> <cmd> --duration 5s\|--lines N\|--until <re>` |
| Output of a finished run | `tx output <pane> <run-id>` / `--last` / `--since-run <id>` |
| Bookmark + read from | `tx mark <pane> <name>` ; `tx tail --from <name>` |
| Status | `tx status <pane>` (one line) / `tx info <pane>` / `tx ls` / `tx runs <pane>` |
| Interrupt one run | `tx kill-run <pane> <run-id>` |
| Revive a dead pane | `tx restart <pane>` |
| Structured exit code | `tx run --json` → `{exit, duration_ms, stdout, truncated, …}` |
| Preserve ANSI | `--keep-ansi` on any read command |

`tx run` exits 0 even when the wrapped command failed — read `[exit:N]`
from the body or `--json.exit`. Compaction is on by default in v1.5.0;
add `--raw` if you need bytes verbatim.

### Compaction may strip information

Output is post-processed by per-tool normalizers + generic layers
(banners, blank-runs, repeated-line collapse, token-budget truncation).
A footer line `[tx:compact tier=... layers=... saved=...%]` tells you
what fired. **If you suspect a normalizer dropped a column or line you
need**, re-run the *single* command with one of:

| Need | Flag |
|---|---|
| Full original bytes, no compaction at all | `tx run --raw <pane> <cmd>` |
| Compaction except the per-tool normalizer (keeps L1-L5) | `tx run --no-normalize <pane> <cmd>` |
| Recover the elided middle of a truncated response | `tx output <pane> --handle h-XXX --full` (or `--range`/`--grep`) |

Use these sparingly. The default mode is correct >99% of the time and
the agent's effective context is much larger with it on. `--raw` should
be the exception, not the workaround.

### Pull a sub-doc when you need to …

| Topic | Doc |
|---|---|
| any flag/option you don't recognise, full command reference | `docs/tx-doc-reference.md` |
| control compaction (`--terse`/`--raw`/`--token-budget`), retrieve elided content (`tx output --handle h-XXX --range/--grep/--full`), per-tool normalizers, `tx compact-stats`, `TX_NO_COMPACT` | `docs/tx-doc-compaction.md` |
| nested shells (`tx hook-install`), secrets (`tx send-secret`), handoff (`tx handoff` / `tx resume`), sudo (`tx sudo`), paste, file deploy (`tx write`), safety rails (allowlist / redact / confirm), `--on-timeout` details | `docs/tx-doc-advanced.md` |
| concrete before/after for each builtin normalizer | `docs/compaction_samples.md` |
| real-world scenarios / troubleshooting / comparison | `docs/tx-doc-use-cases.md`, `docs/tx-doc-troubleshooting.md`, `docs/tx-doc-comparison.md` |

Default workflow if unsure: `tx new` → `tx run` → read the body + `[exit:N]`.
Reach for the sub-docs only when the table above doesn't answer your case.
