# Contributing to `tx`

Thanks for your interest in improving `tx`. This guide covers the dev loop.

## Requirements

- Python ≥ 3.11
- [`uv`](https://github.com/astral-sh/uv) (resolves the inline PEP-723 deps)
- `tmux` ≥ 3.0
- Docker (optional — only for the Linux real-tool normalizer tier)

## Running the tests

```sh
./run-tests                       # full host suite (~600 cases, ~2.5 min)
./run-tests -q                    # quiet
./run-tests tests/test_state.py   # a single file
```

The Linux-only normalizer tests (`tests/test_normalizer_real.py`) are skipped on
a host run and execute inside the container:

```sh
./run-tests-docker                # builds the image if needed, runs everything
./run-tests-docker --rebuild      # force a clean docker build
```

Coverage (subprocess-aware, since most CLI tests run `tx` in a subprocess):

```sh
./run-coverage
```

## Linting

Project-specific [semgrep](https://semgrep.dev/) rules live in `semgrep/`
(security, conventions, quality). Run them with:

```sh
./scripts/semgrep
```

## Adding a normalizer

Drop a single file in `~/.tx/filters/<name>.toml` (line-based filter) or
`~/.tx/plugins/<name>.py` (structural parser). See
[`docs/tx-doc-compaction.md`](docs/tx-doc-compaction.md) → "Authoring a new
normalizer" for the schema and an example. Builtins live in
`tx_compact/builtin_filters/` and `tx_compact/builtin_plugins/`. Inline tests
are discovered automatically by the test runner.

## Commit style

Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
(`fix:`, `feat:`, `docs:`, `test:`, …). Keep the wire format stable: the marker
protocol, `offsets.json` schema, and JSON output shape are committed — breaking
changes require a major version bump and a migration path.

## Before opening a PR

1. `./run-tests` is green.
2. New behavior has a test and a docs update (the `tx --help` text and
   `docs/tx-doc-reference.md` are kept in sync — see `tests/test_docs_help_contract.py`).
3. No personal paths, secrets, or machine-specific config in the diff.
