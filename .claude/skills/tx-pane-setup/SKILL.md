---
name: tx-pane-setup
description: >-
  Install and verify tx-pane on this machine for first-time use. Use when the
  user is getting started with tx-pane, asks to "install" / "set up" / "get
  tx-pane working", hits "command not found: tx-pane", or wants to confirm their
  environment (uv, tmux, Python 3.11+) is ready. Symlinks the command, creates
  the default config, and runs a real marker-protocol smoke test.
argument-hint: "(no args)"
disable-model-invocation: true
allowed-tools:
  - Bash(./tx-pane *)
  - Bash(tx-pane *)
  - Bash(command -v *)
  - Bash(which *)
  - Bash(python3 --version)
  - Bash(tmux -V)
  - Bash(uv --version)
  - Bash(uname *)
  - Bash(chmod +x *)
  - Bash(ln -s *)
  - Bash(mkdir -p *)
---

# tx-pane first-time setup

Goal: get `tx-pane` installed, on `PATH`, and **verified with a real
marker-protocol smoke test**. Be idempotent — detect what's already present
before changing anything. Run from the repo root (the directory containing the
`tx-pane` script).

## 1. Preconditions — check, don't assume

Run these and report a checklist (✓/✗):

- `python3 --version` → need **≥ 3.11**
- `command -v uv` → `uv` resolves the inline PEP-723 deps. Required.
- `command -v tmux` && `tmux -V` → need **tmux ≥ 3.0**
- `uname -s` → branch macOS vs Linux for any install commands

If `uv` or `tmux` is missing, **show the user the exact install command and let
them confirm** (do not silently install system packages):

- `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh` (see astral.sh/uv)
- `tmux`: macOS → `brew install tmux`; Debian/Ubuntu → `sudo apt-get install -y tmux`

## 2. Make the command available

Skip this whole step if `command -v tx-pane` already resolves to this repo's
script. Otherwise pick one:

- **Symlink (zero-install, recommended):**
  ```sh
  chmod +x ./tx-pane
  mkdir -p ~/.local/bin
  ln -s "$PWD/tx-pane" ~/.local/bin/tx-pane
  ```
  Then confirm `~/.local/bin` is on `PATH` (`command -v tx-pane`). If not, tell
  the user to add `export PATH="$HOME/.local/bin:$PATH"` to their shell rc.
- **pip:** `pip install -e .` (provides the `tx-pane` console script).

## 3. Verify — smoke test (this is the important part)

```sh
tx-pane --help | head -5
pane=$(tx-pane new setup-check)
tx-pane run "$pane" "echo tx-pane works"        # expect [exit:0] + the echo
tx-pane run "$pane" "printf 'a\na\na\nb\n'"      # expect a [tx-pane:compact ...] footer
tx-pane ls                                        # setup-check listed as idle
tx-pane kill "$pane"
```

- `[exit:0]` on the first run = the marker hook installed correctly.
- `[exit:?] (hook missing)` = the controlling shell isn't bash/zsh, or
  `PROMPT_COMMAND`/`precmd` was overridden — note it and point at
  `tx-pane hook-install`.

## 4. Hand off to next steps

- Config: `~/.tx-pane/config.toml` (created on first run). Logs:
  `~/.tx-pane/logs/`. Override the home dir with `TX_PANE_HOME`.
- Decision table: `CLAUDE.md`. Agent workflows: `docs/tx-doc-agent-playbook.md`.
- Try `/tx-pane-demo` for a live co-working session, or
  `/tx-pane-run <task>` to execute a task end-to-end.

Finish with a short summary: versions found, what you installed/symlinked, and
the smoke-test result.
