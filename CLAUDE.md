## Terminal: `tx-pane`

Use `tx-pane` for all terminal interaction. Never invoke `tmux` directly. If
`tx-pane` can't do what you need, stop and report — do not work around.

A pane is the unit of work. `tx-pane new` returns an id (`p1`, `web-server`,
…); capture it once and re-use. State persists across `tx-pane` invocations
in `~/.tx-pane/`; there is no daemon.

### Decision table

| Need | Command |
|---|---|
| Create pane | `tx-pane new [name] [--cwd <dir>]` → capture id |
| Run + wait for output | `tx-pane run <pane> <cmd>` |
| Run async, wait later | `id=$(tx-pane exec <pane> <cmd>)` then `tx-pane wait-run <pane> $id` |
| Resolve "pane busy" error | `--queue` \| `--kill-and-run` \| `--stdin` (one of, no `--force`) |
| Early-exit on a pattern | `tx-pane run --wait-for <re>` (exit 0) / `--fail-for <re>` (exit 1) |
| Cancel on timeout | `tx-pane run --on-timeout cancel\|kill` |
| Send key / raw text | `tx-pane key <pane> C-c Enter` / `tx-pane send <pane> <text>` |
| Read new output | `tx-pane tail <pane>` (advances offset); `--all` drains |
| Read fixed slice | `tx-pane dump <pane> --tail N` / `--head N` (no offset change) |
| Grep output | `tx-pane grep <pane> '<re>' -C 2` |
| Wait for a regex | `tx-pane wait <pane> <re>` |
| Bounded capture (`tail -f`-like) | `tx-pane stream <pane> <cmd> --duration 5s\|--lines N\|--until <re>` |
| Output of a finished run | `tx-pane output <pane> <run-id>` / `--last` / `--since-run <id>` |
| Bookmark + read from | `tx-pane mark <pane> <name>` ; `tx-pane tail --from <name>` |
| Status | `tx-pane status <pane>` (one line) / `tx-pane info <pane>` / `tx-pane ls` / `tx-pane runs <pane>` |
| Interrupt one run | `tx-pane kill-run <pane> <run-id>` |
| Revive a dead pane | `tx-pane restart <pane>` |
| Structured exit code | `tx-pane run --json` → `{exit, duration_ms, stdout, truncated, …}` |
| Preserve ANSI | `--keep-ansi` on any read command |

`tx-pane run` exits 0 even when the wrapped command failed — read `[exit:N]`
from the body or `--json.exit`. Compaction is on by default in v1.5.0;
add `--raw` if you need bytes verbatim.

### Compaction may strip information

Output is post-processed by per-tool normalizers + generic layers
(banners, blank-runs, repeated-line collapse, token-budget truncation).
A footer line `[tx-pane:compact tier=... layers=... saved=...%]` tells you
what fired. **If you suspect a normalizer dropped a column or line you
need**, re-run the *single* command with one of:

| Need | Flag |
|---|---|
| Full original bytes, no compaction at all | `tx-pane run --raw <pane> <cmd>` |
| Compaction except the per-tool normalizer (keeps L1-L5) | `tx-pane run --no-normalize <pane> <cmd>` |
| Recover the elided middle of a truncated response | `tx-pane output <pane> --handle h-XXX --full` (or `--range`/`--grep`) |

Use these sparingly. The default mode is correct >99% of the time and
the agent's effective context is much larger with it on. `--raw` should
be the exception, not the workaround.

### Pull a sub-doc when you need to …

| Topic | Doc |
|---|---|
| any flag/option you don't recognise, full command reference | `docs/tx-doc-reference.md` |
| control compaction (`--terse`/`--raw`/`--token-budget`), retrieve elided content (`tx-pane output --handle h-XXX --range/--grep/--full`), per-tool normalizers, `tx-pane compact-stats`, `TX_PANE_NO_COMPACT` | `docs/tx-doc-compaction.md` |
| nested shells (`tx-pane hook-install`), secrets (`tx-pane send-secret`), handoff (`tx-pane handoff` / `tx-pane resume`), sudo (`tx-pane sudo`), paste, file deploy (`tx-pane write`), safety rails (allowlist / redact / confirm), `--on-timeout` details | `docs/tx-doc-advanced.md` |
| concrete before/after for each builtin normalizer | `docs/compaction_samples.md` |
| real-world scenarios / troubleshooting / comparison | `docs/tx-doc-use-cases.md`, `docs/tx-doc-troubleshooting.md`, `docs/tx-doc-comparison.md` |

Default workflow if unsure: `tx-pane new` → `tx-pane run` → read the body + `[exit:N]`.
Reach for the sub-docs only when the table above doesn't answer your case.
