# Changelog

All notable changes to `tx` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The wire format (marker protocol v2, `offsets.json` schema, JSON output shape)
is committed — breaking changes require a major version bump and a migration
path.

## [1.5.0] — 2026-05-14

### Added
- L5 cross-call deduplication layer (opt-in).
- Comprehensive documentation split into topic-focused files under `docs/`.

## [1.4.0]

### Added
- L4 token-budget head/tail elision.
- `h-XXX` handle protocol for recovering elided output
  (`tx output --handle h-XXX --range/--grep/--full`).
- 17 builtin per-tool normalizers.

## [1.2.0]

### Added
- L3 repeated-line collapse (run-length encoding).
- Three-tier output quality model and compaction telemetry.
- `tx compact-stats`.

## [1.1.0]

### Added
- Compaction core: L1 banner hygiene + L2 whitespace normalization.

## [1.0.0]

### Added
- `tx write` atomic file deploy (stage, sha256-verify, chmod/chown, atomic mv, reload).
- Log rotation.
- `fish` shell marker hook.

### Changed
- Evaluated and explicitly declined a `txd` daemon — the architecture stays
  stateless (single script + `fcntl.flock` on `offsets.json`).

## [0.4.0]

### Added
- `tx grep` / `tx dump` / `tx stream`.
- `--json` output on every command.
- `tx sudo`, paste support, and the `[security]` safety rails
  (allowlists, redaction, confirm patterns).

## [0.3.0]

### Added
- `tx info`, `tx handoff` / `tx resume`, `tx send-secret`, output bookmarks,
  `--on-timeout` policies, and `tx restart`.

## [0.2.0]

### Added
- Marker protocol v2, the pane state machine, refuse-on-busy semantics,
  and run-ids.
