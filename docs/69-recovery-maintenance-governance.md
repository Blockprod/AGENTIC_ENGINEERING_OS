# Recovery & Maintenance Governance

## Boundary

`MaintenanceGovernanceService` controls whether a future operation may begin and
records operator-controlled maintenance transitions. It is an admission boundary,
not a second business state machine. Its decisions never certify, pass a gate,
approve business work, merge, change a workflow generation, or mutate
`ProjectState`/`MissionState`.

The service exposes three operations:

- `initialize(request)` explicitly establishes `NORMAL` under attributable Human
  authority; absence is never interpreted as `NORMAL`;
- `evaluate(context, operation)` returns `ADMITTED`, `HUMAN_REQUIRED`, or
  `REFUSED` from the persisted state and current bound observations;
- `request_transition(context, request)` performs an exact authorized state
  transition and may return a declarative recovery dispatch request.

`enforce(admission, operation)` is the minimal integration guard. It accepts only
an `ADMITTED` decision bound to the exact current persisted fingerprint. Existing
authoritative workflow services retain all of their own validations.

## Closed state model

- `NORMAL`: new operations may be admitted when Health, Governance, budgets and
  incidents permit them.
- `DRAINING`: no new mission, role, parallel group, worktree, merge, or deployment
  operation. Only a safe already-in-flight completion is admitted; recovery needs
  an operator.
- `MAINTENANCE`: new work is refused. Recovery/remediation requires an operator;
  read-only diagnostics remain available.
- `RECOVERY_REQUIRED`: ordinary work is refused. Only explicitly routed recovery
  may proceed, with Human intervention.
- `FROZEN`: all new work and mutations are refused. Read-only diagnostics remain
  admitted; explicit recovery/remediation is `HUMAN_REQUIRED`. There is no generic
  maintenance exception.

The closed transition graph is:

- `NORMAL → DRAINING | MAINTENANCE | RECOVERY_REQUIRED | FROZEN`;
- `DRAINING → NORMAL | MAINTENANCE | RECOVERY_REQUIRED | FROZEN`;
- `MAINTENANCE → NORMAL | RECOVERY_REQUIRED | FROZEN`;
- `RECOVERY_REQUIRED → NORMAL | MAINTENANCE | FROZEN`;
- `FROZEN → MAINTENANCE | RECOVERY_REQUIRED`.

Returning to `NORMAL` requires current exact-scope sources, healthy Health,
non-blocking Governance, safe budgets and no unresolved critical incident.
`RECOVERY_REQUIRED → NORMAL` additionally requires a current successful
observation for the exact selected route. A frozen system must therefore pass
through an explicit recovery or maintenance path; it cannot be directly unfrozen.

## Admission inputs

The closed operation catalog covers mission, role and parallel starts, worktree
creation, merge, in-flight completion, remediation, recovery, adoption/migration,
and read-only diagnostics. The service combines the durable maintenance state with
current Health, the matching `GovernedOperation` decision, matching resource
budgets, and exact-scope incidents. Foreign project, repository, HEAD, mission or
generation bindings, stale/future evaluations, `BLOCK`/`UNKNOWN`, exhausted
budgets and unresolved critical incidents fail closed.

Diagnostics are the deliberate exception to source-health admission: they remain
read-only and available when the persisted maintenance record itself can be read
and exactly bound. Corrupt or missing expected maintenance state is never replaced
with a default.

## Human authority and recovery routing

Initialization and every state transition require an attributable non-Codex Human
identity. Each transition must also match the canonically normalized identity of
the durable current operator; an unrelated Human cannot take over implicitly.
This operator authority is separate from business `HumanApproval`; the service
cannot create or apply such an approval. Requests carry the exact current revision
and fingerprint, so stale, duplicate and replayed writes are refused.

Entering `RECOVERY_REQUIRED` produces only a declarative request for an existing
boundary:

- P2 sequential remediation;
- `ParallelMissionWorkflow.resume_recovery` for P3;
- `RestartSafeCodexExecutionService.inspect_restart` for P4;
- existing adoption/upgrade recovery boundaries for P5.

The service performs no recovery or repair. Existing mechanisms run separately,
produce new observations, and are followed by reevaluation. A reserved Codex
identity cannot be the source that declares recovery successful.

## Persistence and incident integration

`.agentic-engineering-os/maintenance.json` is a versioned, strict JSON,
project/root-bound runtime record. Writes use an atomic replacement and a private
exact-mutation capability; there is no public arbitrary `save`. Revision,
timestamp, actor, HEAD, mission/generation, transition reason, predecessor and
content fingerprints form a restart-safe chain. An exclusive fail-closed write lock
serializes competing writers; a stale lock requires operator inspection rather than
being removed automatically. The state, lock and temporary write files are
Git-ignored and covered by repository/runtime policy checks.

Incidents inform admission and transition evaluation but do not mutate maintenance
state. A persistent critical incident can justify a Human transition to `FROZEN`;
recovery-stuck conditions can justify `RECOVERY_REQUIRED`; uncertain observability
prevents returning to normal. Each actual transition still requires an explicit,
fresh Human request through this service.
