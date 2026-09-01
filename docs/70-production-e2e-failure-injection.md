# Production E2E & Failure Injection

## Campaign boundary

P6.11 validates the existing production-observability chain with deterministic
temporary repositories, real local stores and Git, existing fake Codex processes,
and controlled file failures. It adds no scheduler, daemon, autonomous repair,
business authority or failure-injection framework.

The exercised pipeline is:

```text
OperationalEvent → OperationalEventStore → Metrics → Health → Governance
→ Resource budgets → Incidents → Maintenance admission → Operator diagnostics
```

Every result remains observational or admission-only. `HEALTHY` is not
`CERTIFIED`; Governance `ALLOW` and `WITHIN_BUDGET` are not Control Plane
authorization. No P0–P5 state is reconstructed from metrics or mutated by this
campaign.

## Failure-injection matrix

| ID | Injection | Expected | Observed | Result |
|---|---|---|---|---|
| A | Healthy nominal repository and stores | COMPLETE, HEALTHY, ALLOW, within budget, no incident, NORMAL admission | Exact chain observed; no authority methods exposed | PASS |
| B | Corrupt, truncated, locked or unavailable EventStore | UNAVAILABLE, non-HEALTHY, governance constraint, incident, refusal | Fail-closed chain observed without repair | PASS |
| C | Retention exhausted | INCOMPLETE and observable saturation | Health/governance/admission degraded consistently | PASS |
| D | Authoritative project/mission source unavailable or corrupt | Metrics cannot substitute authority | COMPLETE metrics coexisted with non-HEALTHY, block and incident; corrupt stores retained explicit errors | PASS |
| E | Codex timeout/interruption | Durable failure, no blind retry | Event, metric, Health, incident and P4 restart disposition remained negative | PASS |
| F | Tool failure with exit 0 and malformed result | Never accepted as success | P4 stored `FAILED`; intake refused malformed payload | PASS |
| G | Execution recovery required | Operator recovery, no retry | P4 returned `RECOVERY_REQUIRED`; maintenance routed to the existing boundary | PASS |
| H | Missing, drifted, dirty, mismatched, orphan or stale-generation worktree | Block new work; preserve facts/files | Exact anomalies and refusal observed; no adoption/cleanup/reset/rebase occurred | PASS |
| I | Codex/worktree concurrency exceeded | Not admitted | `current + requested > limit` produced `LIMIT_EXCEEDED` and refusal | PASS |
| J | Storage unknown/exceeded | Never within budget | `UNKNOWN`/`LIMIT_EXCEEDED`, incident and admission refusal | PASS |
| K | Remediation generation maximum | No extra generation | `LIMIT_EXCEEDED`; no generation mutation | PASS |
| L | Critical persistence failure | Human-controlled freeze | Critical incident informed an explicit Human transition to `FROZEN` | PASS |
| M | Restart while frozen | Preserve `FROZEN` | New service/store instance restored exact state; diagnostics remained available | PASS |
| N | Fake Codex, unrelated Human, stale/replayed or foreign transition | Refuse | Canonical Human checks, operator continuity and exact revision/scope checks refused attacks | PASS |
| O | Controlled recovery and operator unfreeze | Existing recovery, factual resolution, fresh reevaluation, Human transition | P4 route, normalized incident resolution and explicit return to `NORMAL` observed | PASS |
| P | Diagnostics during degraded state | Truthful output, attention exit, read-only, no secret | CLI reported INCOMPLETE/non-HEALTHY/incidents, exit 2, authority notice, unchanged files | PASS |

## Persistence and restart proof

The campaign reconstructs the EventStore, incident journal, maintenance store and
P4 execution recovery service. Incident identity and revision deduplication survive
journal reconstruction. `FROZEN` survives process-object destruction. A stale
healthy snapshot is rejected and cannot authorize a later transition.

No injected failure is silently deleted or repaired. Stale writer locks remain
fail-closed, dirty files remain present, P4 never blindly retries, and maintenance
never unfreezes itself.

## Product correction

The adversarial Human test demonstrated that any otherwise attributable Human
could previously replace the durable maintenance operator. P6.11 minimally closes
that integration gap: every transition must match the canonically normalized
identity of the current persisted operator. This operator continuity remains
separate from business `HumanApproval`; no allowlist or identity engine was added.
