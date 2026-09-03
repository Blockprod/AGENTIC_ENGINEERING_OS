# Changelog

This project follows Semantic Versioning. Release entries identify the exact
Git commit and immutable artifact digests in the certification dossier.

## [Unreleased]

- Added explicit `orchestration.json` 1.0/1.1 to 1.2 migrations.
- Added restart-safe multi-story planning and canonical DAG validation.
- Added the vertical `MissionRunner` and public `mission run/resume/status` CLI.
- Added bounded Codex and verification subprocess output collection.
- Added repository-wide mission writer locking and canonical Human Evidence intake.
- Added Apache-2.0 licensing and Windows release automation foundations.
- Reduced the complete deterministic Windows suite median to 527.12 seconds
  through isolated module-level workers, with a 607-second regression gate.
- Added strict release-dossier, signed-tag, candidate-wheel digest and packaged
  resource-inventory verification.
