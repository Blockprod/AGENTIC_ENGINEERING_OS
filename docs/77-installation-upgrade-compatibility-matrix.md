# Installation and Upgrade Compatibility Matrix

## Candidate and installed environment

P7.8 used one wheel built from Git baseline
`fed05e9ed3c32e8ec81b32836e9eaf4ed9e828bf`:

- filename: `agentic_engineering_os-0.1.0-py3-none-any.whl`;
- version: `0.1.0`;
- size: `428638` bytes;
- SHA-256: `2940d2265f66189ffb277aa0d183e9d02263504dd72c57fb712802a07345dc48`;
- archive entries: `145`;
- required packaged content: schemas, roles and product documents present.

The wheel was built once from a Git archive outside the checkout and reused
unchanged. One fresh Python 3.11 environment outside the checkout installed
that wheel and its declared dependency. Import resolved from its
`site-packages`; packaged schemas, roles and documents were readable without
`PYTHONPATH` or access to the source checkout. Installed `pip check` passed.

## BEFORE matrix

The matrix is derived only from P5.9 and P7.7 repository truth: package
`0.1.0`; configuration, state, mission, worktree, operational-event and
maintenance formats `1.0`; AGENTS `2`; Git-ignore `1`; negative outcomes
`2.0`; execution ledger `1.1`. The only registered edges are AGENTS `1 → 2`
and negative outcomes `1.0 → 2.0`. Execution `1.0 → 1.1` is unsupported.

## Installed compatibility matrix

| Scenario | Product | Artifact versions / pre-install state | Expected classification | Explicit migration | Observed result |
|---|---:|---|---|---|---|
| A — fresh uninitialized | `0.1.0` | no Agentic OS artifacts | `SUPPORTED`, then `NEEDS_CONFIGURATION` | adoption plan only | `inspect=0/SUPPORTED`; `status=2/NEEDS_CONFIGURATION`; `plan=0/READY_TO_APPLY`; bytes unchanged — PASS |
| B — current adopted | `0.1.0` | config/state `1.0`, AGENTS `2`, Git-ignore `1` | `ADOPTED` / `ALREADY_CURRENT` | none | status and upgrade dry-run/apply current; bytes unchanged — PASS |
| C — lazy artifacts absent | `0.1.0` | mission/worktree/execution absent | `NOT_PRESENT_LAZY`, globally current | none | repository remains adopted; no lazy file created — PASS |
| D — AGENTS historical | `0.1.0` | AGENTS `1` | `MIGRATION_REQUIRED` | registered `1 → 2`, Human required | dry-run first; missing Human `BLOCKED`; fake Codex `REFUSED`; Human apply `MIGRATED`; backup exact — PASS |
| E — negative outcomes historical | `0.1.0` | negative outcomes `1.0` | `MIGRATION_REQUIRED` | registered `1.0 → 2.0` | explicit apply produced `2.0`, exact backup and preserved outcomes semantics — PASS |
| F — execution historical | `0.1.0` | executions `1.0`, current target `1.1` | `UNSUPPORTED` | none | upgrade exit `2/BLOCKED`, source unchanged — PASS |
| G — maintenance current | `0.1.0` | maintenance `1.0` | `CURRENT` | none | upgrade `ALREADY_CURRENT` — PASS |
| H — maintenance future | `0.1.0` | maintenance `99.0` | `FUTURE_VERSION` / unsupported migration | none | upgrade exit `2/BLOCKED`, source unchanged — PASS |
| I — corrupt persisted artifact | `0.1.0` | invalid ProjectState JSON | `CORRUPT` | none | status exit `2/PARTIAL_OR_INCONSISTENT` — PASS |
| J — mixed current + migrations | `0.1.0` | current core plus AGENTS `1` and negative outcomes `1.0` | `MIGRATION_REQUIRED`, never current | both registered edges only | two-step dry-run; explicit apply migrated both; second apply `ALREADY_CURRENT` — PASS |
| K — future configuration | `0.1.0` | config `99.0` | `FUTURE_VERSION` | none | status exit `2/UPGRADE_REQUIRED` — PASS |
| L — foreign runtime artifact | `0.1.0` | maintenance `1.0` copied from another project/repository | incompatible foreign binding | none | upgrade exit `2/BLOCKED`, `FOREIGN_RUNTIME_ARTIFACT`, bytes unchanged — PASS |

There is no generic “compatible” result. Each row states the exact product and
artifact condition, classification, migration availability and observation.

## Installation is not migration

Repository file SHA-256 snapshots taken before and after wheel installation
and the `inspect`, `status`, `plan`, and upgrade dry-run operations were
unchanged. Import and startup created no project artifact. Only an explicit
installed CLI `upgrade --apply`, after a fresh P5.9 plan and exact Human
confirmation where required, wrote a migrated target and adjacent backup.

The optional `agentic-os.exe` shim was not required. Every installed CLI proof
used the canonical `<environment-python> -m agentic_engineering_os` form and
parsed deterministic JSON with observed exit codes.

## Cross-artifact and cross-project results

A present lazy artifact is relevant: one migration-required artifact prevents
global `CURRENT`, and one unsupported artifact blocks. Legitimately absent
lazy artifacts are neutral and are not created by inspection.

P5.9/P7.7 adversarial coverage additionally refused CompatibilityAssessment,
UpgradePlan and Human confirmation reuse across projects; copied runtime
state; swapped backup/source fingerprints; and stale plans after source or
HEAD mutation. Refusal occurs before unauthorized replacement.

## Product-version probes

Evaluator-level inputs, not fabricated wheels, produced:

| Installed product input | Product classification | Artifact migration implication |
|---|---|---|
| `0.1.0` | `CURRENT` | none |
| `0.1.1` | `FUTURE_VERSION` to this installed contract | none |
| `0.2.0` | `FUTURE_VERSION` to this installed contract | none |
| `1.0.0` | `FUTURE_VERSION` to this installed contract | none |
| malformed `next` | rejected input | none |

A newer product identity never changes artifact versions or creates a
migration edge.

## Failure injection and scope

The bounded P5.9 impacted tests passed backup collision, write failure, source
mutation, corrupt source, symlink source/target protection, wrong repository,
missing/fake Human, and partial multi-artifact migration. Completed prior
steps remain observable during partial failure; no silent rollback or
authority escalation is claimed.

P7.8 introduced no product correction, migration edge, release candidate,
P7.9/P7.10 behavior, Codex probe, project-command execution or full-suite run.
