# semgrep — tx-specific lint rules

A small, hand-written semgrep ruleset that enforces conventions and
security invariants specific to this codebase. The rules target
patterns that are **wrong for `tx-pane` even when they'd be fine elsewhere**;
generic Python lints are out of scope (use `ruff` / `mypy` / `bandit`
for those).

```
semgrep/
├── README.md            this file
├── tx-security.yaml     5 ERROR rules — shell injection / secret-leak surface
├── tx-conventions.yaml  6 rules     — module-boundary + helper-call invariants
├── tx-quality.yaml      2 WARNING rules — code-quality micro-fixes
└── tests/
    ├── tx-security.py     positive + negative fixtures
    ├── tx-conventions.py
    └── tx-quality.py
```

## Run it

```bash
# install once
uv tool install semgrep

# scan the source tree (current behaviour: zero findings on main)
./scripts/semgrep

# verify the rule fixtures
./scripts/semgrep --test
```

The wrapper exits non-zero on any finding so it slots into CI as-is.
Today it's opt-in (developer-run); it'll become a required check when
the ruleset stabilises.

## What's enforced

**Security (ERROR):**
- `tx-no-shell-true` — no `shell=True` / `os.system`. Argv lists only.
- `tx-shell-injection-in-send-keys` — CLI args may not be f-string-spliced
  into a `send_keys(...)` payload. Use `wrap_command(cmd, run_id)` first.
- `tx-paste-buffer-via-tempfile` — every `paste-buffer` must be paired
  with a tempfile-staged `load-buffer`. Argv encoding doesn't survive
  binary content.
- `tx-sudo-needs-bracketed-paste` — `sudo -S` passwords must traverse
  the `stop_pipe_pane → send_keys → start_pipe_pane` send-secret path.
- `tx-no-stale-write-on-failed-mv` — atomic file deploys must
  `sha256-verify` the staged file before `mv`-ing it into place.

**Conventions (WARNING / ERROR):**
- `tx-error-via-err-helper` — `err("…")` instead of
  `click.echo("[error: …]") + sys.exit(N)`.
- `tx-warn-via-warn-helper` — `warn("…")` instead of
  `click.echo("[warning: …]")`.
- `tx-offsets-mutation-locked` — `save_offsets(...)` must run inside
  `with offsets_lock(): ...` (modulo a few documented exclusions).
- `tx-tmux-via-wrappers` — `get_server()`, not `libtmux.Server()`.
- `tx-marker-via-protocol` — never inline `f"r-{secrets.token_hex(...)}"`
  or the `\x01TX_END …` byte sequence; use the helpers in
  `tx_core.marker`.
- `tx-no-hardcoded-paths` — reach for `TX_DIR` / `LOGS_DIR` /
  `OFFSETS_PATH` constants instead of `~/.tx-pane/...` literals.

**Quality (WARNING):**
- `tx-redundant-or-none` — `state.get("x") or None` is a no-op.
- `tx-color-on-echo` — multi-line `click.echo("\n".join(...))` in run
  emitters needs `color=keep_ansi_resolved or None`.

Two rules from the original spec (`tx-no-bare-except`,
`tx-re-compile-in-loop`, `tx-pane-resolution-via-helper`) were dropped
during the initial dry run — see the comments in the yaml files for
the rationale. Each had heavy false-positive rates against this
codebase's deliberate idioms (best-effort tmux cleanup, one-shot config
loaders, post-allocate pane verification).

## Adding a rule

1. Edit the appropriate `tx-*.yaml` file. Keep rule IDs `tx-`-prefixed
   so they stand out in mixed output with other tools.
2. Add at least one `# ruleid: <id>` (positive) and one `# ok: <id>`
   (negative) fixture in the matching `tests/tx-*.py` file.
3. Verify: `./scripts/semgrep --test`.
4. Whole-tree dry run: `./scripts/semgrep`. The rule should fire **zero
   times** on the current `main`. If it fires, either the rule is too
   broad or you've found a real bug — fix one, not both.
5. Run `./run-tests -q` afterward if any code-level cleanup landed.

## Why per-file `.semgrepignore`?

`.semgrepignore` at repo root carries `!semgrep/tests/` so semgrep's
built-in default exclusion of `tests/` doesn't drop our fixtures.
Production tests under `tests/` follow `.gitignore`'s `tests/` rule
naturally; only the `semgrep/tests/` subtree needs the exception.
