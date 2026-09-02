# Backward Compatibility and Versioning

## Version inventory reconstructed at P7.7

The inventory below is limited to formats present in code, schemas, tests, or
the P5.9 migration registry. “None observed” means the repository contains no
evidence of an older persisted format; it is not a compatibility promise.

| Artifact | Current version | Historical versions actually observed | Persistence | Authority class | Registered migration edges | Unknown/future behavior |
|---|---:|---|---|---|---|---|
| Python package | `0.1.0` | none observed | installed wheel metadata | product identity, not repository authority | not applicable | non-current installation is not inferred compatible |
| ProjectConfiguration | `1.0` | none observed | `.agentic-engineering-os/config.json`, Git-tracked | project configuration | none | unsupported/future fail closed |
| ProjectState | `1.0` | none observed | `.agentic-engineering-os/state.json`, Git-tracked | authoritative project state | none | unsupported/future/corrupt fail closed |
| MissionState | `1.0` | `0.9` appears only as an unsupported adversarial fixture | `.agentic-engineering-os/mission.json`, tracked or ignored by project policy | operational state, non-authoritative for project control | none | unsupported/future/corrupt fail closed |
| AGENTS managed section | `2` | `1` canonical historical content | managed section of Git-tracked `AGENTS.md` | system operating contract | `1 → 2` | unknown/future/tampered fail closed |
| Git-ignore managed section | `1` | none observed | managed section of Git-tracked `.gitignore` | system installation policy | none | unknown/future/tampered fail closed |
| WorktreeRegistry | `1.0` | none observed | ignored volatile `worktrees.json` | operational resource state | none | unsupported/future/corrupt fail closed |
| negative-outcomes ledger | `2.0` | `1.0` canonical historical document | ignored volatile `negative-outcomes.json` | operational replay protection | `1.0 → 2.0` | unsupported/future/corrupt fail closed |
| execution ledger | `1.1` | `1.0` | ignored volatile `executions.json` | operational execution recovery | deliberately no `1.0 → 1.1` edge | unsupported/future/corrupt fail closed |
| OperationalEvent | `1.0` | none observed | event payload inside ignored JSONL segments | non-authoritative observation | none | unsupported/future/corrupt record fails closed |
| EventStore record/segment | `1.0` | none observed | `operational-events/segment-*.jsonl`; record version and segment identity marker | non-authoritative observation store | none | unsupported/future/corrupt segment fails closed |
| maintenance state | `1.0` | none observed | ignored volatile `maintenance.json` | system safety/continuity state | none | absent is lazy; incompatible/corrupt fails closed |

User Stories, Evidence, Gates, Certifications, and Audit Events are nested in
ProjectState and therefore share its persisted document version. Incident
records reuse OperationalEvent persistence. Metrics, Health, Governance and
ResourceBudget structures have model/schema constants `1.0` but no standalone
persistent store was found; they are not invented as repository formats.

## Independent version axes

```text
PACKAGE VERSION
!= CONFIG VERSION
!= SCHEMA VERSION
!= PERSISTED FORMAT VERSION
```

A package upgrade is not a project migration. Importing or installing a newer
wheel must not rewrite repository state. Each persisted format owns its exact
version, validation contract, relevance, and explicit migration edges.

## Closed compatibility taxonomy

- `CURRENT`: exact current version and successful model/schema validation.
- `BACKWARD_COMPATIBLE`: an explicitly catalogued version readable without a
  migration. The P7.7 catalogue currently contains no such historical entry.
- `MIGRATION_REQUIRED`: the exact source-to-target edge exists in P5.9.
- `UNSUPPORTED`: an older version has no registered edge.
- `FUTURE_VERSION`: a numerically newer format than the installed product
  understands.
- `CORRUPT`: required artifact missing, malformed, or model/schema-misaligned.
- `UNKNOWN`: version or required validation cannot be classified.
- `NOT_PRESENT_LAZY`: a legitimately lazy artifact is absent.

There is no optimistic fallback. Global ordering is `CORRUPT`,
`FUTURE_VERSION`, `UNSUPPORTED`, `UNKNOWN`, `MIGRATION_REQUIRED`,
`BACKWARD_COMPATIBLE`, then `CURRENT`. `NOT_PRESENT_LAZY` is neutral only for a
catalogued lazy artifact.

## Evaluator contract

```text
CompatibilityEvaluator.evaluate(CompatibilityEvaluationContext)
    -> CompatibilityAssessment
```

The context binds installed product version and release digest, project ID,
resolved repository, HEAD, configuration fingerprint, and canonically ordered
artifact observations. Every present artifact binds its canonical path,
version, content SHA-256, structural validation and model/schema alignment.

The result contains per-artifact source/target versions, classification,
diagnostic and fingerprint; global compatibility; exact required migrations;
blockers; diagnostics; and a deterministic assessment fingerprint. A
process-local attestation distinguishes evaluator output from a plain forged
object. `verify_current` re-evaluates all inputs; any changed artifact digest,
project, repository, HEAD, config fingerprint, product identity, path or
version invalidates the old assessment.

The evaluator is read-only. It has no migration, rewrite, save, transition,
certification, unfreeze, operator, Evidence, Gate or Human Approval method.
Its assessment never authorizes adoption or migration.

## Migration authority and known edges

`RepositoryMigrationRegistry` from P5.9 remains the sole source of supported
migrations. P7.7 consults its exact `definition(artifact, source, target)` and
does not accept caller-provided edges. Only these edges exist:

- AGENTS managed section `1 → 2`, with Human confirmation under P5.9;
- negative-outcomes ledger `1.0 → 2.0`.

Execution ledger `1.0 → 1.1` is intentionally unsupported. Maintenance state
`1.0` is current and has no historical edge. There is no nearest-version
guessing, generic JSON upgrade, inferred schema migration, or automatic chain.

## Cross-artifact and schema/model compatibility

Project configuration, ProjectState, AGENTS, and Git-ignore managed sections
are required in the compatibility view. Mission, worktree, negative-outcome,
execution, maintenance and operational-event stores are lazy: absence is not
corruption, but a present instance is fully relevant. One relevant
incompatible artifact prevents global `CURRENT`, even if every other artifact
is current.

Current packaged schemas for configuration, ProjectState, MissionState,
WorktreeRegistry and OperationalEvent use their current model version, require
their contract fields, and retain `additionalProperties: false`. P7.7 does not
loosen schemas. A future version or an unproved model/schema match cannot be
classified `CURRENT`.

## Product and release policy

The package uses Semantic Versioning. During the unfinished P7 roadmap it
remains `0.1.0`; P7.7 does not claim `1.0.0`. A `1.0.0-rc.N` identity may be
created only by an explicit release mission after the remaining P7.8–P7.12
work and requires immutable built-artifact SHA-256 identity. Stable `1.0.0`
requires explicit final product certification and release instruction.

- Patch releases may fix behavior without weakening contracts. They do not
  imply any repository format change or migration.
- Minor releases may add backward-compatible product APIs. Strict persisted
  schemas cannot gain fields under an unchanged format merely because the
  package minor changed.
- Major releases may intentionally break product APIs, but persisted formats
  remain independently versioned and still require explicit registered edges.

Every published wheel/release candidate must be identified by exact SemVer and
immutable digest in compatibility evidence. Package SemVer never substitutes
for config, schema, or persisted-format versions.

## Authority and performance boundaries

Compatibility evaluation cannot migrate, certify, unfreeze, select an
operator, mutate ProjectState/MissionState, or fabricate Evidence, Gates or
Human Approval. Only the explicit P5.9 service can apply a registered edge.

Evaluation is bounded in-memory classification and hashing. It performs no
subprocess, Codex probe, Git invocation, filesystem write, or P7.8
installation/upgrade-matrix behavior.
