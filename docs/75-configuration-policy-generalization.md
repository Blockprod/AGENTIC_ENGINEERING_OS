# Configuration and Policy Generalization

## Scope and authority

P7.6 closes the configuration taxonomy and adds a pure deterministic resolver.
It creates no DSL, editor, remote configuration, dynamic policy service,
persistence format, migration, or P7.7 versioning behavior. Resolution does not
mutate a store and never grants Evidence, Human Approval, Gate, transition,
merge, maintenance operation, or Certification authority.

## BEFORE configuration matrix

| Existing surface / field | Source | Authority class | Persistence | Scope | Mutability | Safety ceiling | Current validation before P7.6 |
|---|---|---|---|---|---|---|---|
| `config_version` | P5 `config.json` | Project configuration / structural | Git-tracked project file | repository | explicit file change only | known schema version only | strict schema; no migration |
| `project_id` | P5 `config.json` | Project configuration | Git-tracked | project | explicit | cannot substitute runtime binding | NFC identity validation |
| `repository_root_policy` | P5 `config.json` | Project configuration | Git-tracked | repository | explicit | root remains discovered fact | one closed enum |
| `toolchains` | P5 declarations | Project configuration | Git-tracked | project | explicit | bare identities; no executable trust | identity/version/path checks |
| `verification_commands` | P5/P7.4 | Project configuration | Git-tracked | project/component | explicit | argv only, no shell/path escape | schema plus command semantics |
| allowed/protected/forbidden paths | P5 `path_policy` | Project configuration | Git-tracked | repository | explicit | certified reserved paths and User Story scope still prevail | normalized conflict checks |
| `context_sources` | P5 | Project configuration | Git-tracked | repository | explicit | Markdown, repository-local | path/secret checks |
| Codex sandbox/approval/clean Git/concurrency | P5 `codex_constraints` | Project configuration within hard safety | Git-tracked | project | explicit | P4 sandbox/never/clean-Git/concurrency ceilings | closed enums and schema |
| mission-state Git disposition | P5 | Project configuration | Git-tracked | project | explicit | existing tracked/ignored semantics only | closed enum |
| `ExecutionScope.allowed_paths/forbidden_paths` | P4 request/User Story | System execution binding | execution context | mission/generation/subject | immutable per request | intersects project and invariant restrictions | ContextBuilder validation |
| archetype command bindings | P7.4 profile | Project configuration projection | none beyond P5 source | project/component | recomputed | configured argv only | archetype evaluator |
| GovernancePolicy identity/version/class/domain/scope/condition/action | P6.6 | Hard safety, operational, or preference | in-memory only | exact governance scope | immutable evaluation input | decision order `BLOCK > ... > ALLOW` | closed model/evaluator |
| ResourceBudget limit/unit/class/scope | P6.7 | Hard safety, operational, preference, or machine restriction | in-memory only | project/HEAD/mission/generation | immutable evaluation input | minimum applicable limit | closed model/evaluator |
| maintenance state/revision/operator/recovery | P6.10 | System invariant and Human authority | repository runtime state | project/repository | controlled transition only | freeze/recovery/operator continuity | strict store and governance service |
| verification tier/completeness | P6 Governance | Hard or operational constraint | in-memory evaluation | governed operation | immutable | may become stricter only | closed policy domain |
| Human requirements | P0–P6 contracts | System invariant | authoritative state/Evidence | exact subject/baseline | controlled services only | never configurable away | identity, evidence and certification checks |
| platform/Git/Python/TEMP facts | P7.3 | Machine fact | not project authority | current machine/repository | freshly observed | can satisfy or restrict prerequisites | bounded discovery |
| toolchain path/version/digest/availability | P7.4 | Machine fact | not project authority | current machine/repository | freshly observed | cannot authorize commands | bound probe/evaluator |
| Codex path/version/digest/capabilities | P7.5 | Machine fact | process-local assessment | executable/project runtime | freshly assessed | can restrict runtime only | identity-bound assessment |

Before P7.6 these surfaces were individually strict, but no single artifact
bound their compatible safety values and provenance to one project,
repository, configuration fingerprint, mission/generation and relevant machine
facts.

## Closed taxonomy and precedence

The exact taxonomy is:

1. `SYSTEM_INVARIANT`;
2. `PROJECT_CONFIGURATION`;
3. `HARD_SAFETY_POLICY`;
4. `OPERATIONAL_POLICY`;
5. `OPERATOR_PREFERENCE`;
6. `MACHINE_FACT`.

Authority precedence is deliberately not alphabetical:

```text
SYSTEM_INVARIANT
  > HARD_SAFETY_POLICY
  > PROJECT_CONFIGURATION
  > OPERATIONAL_POLICY
  > OPERATOR_PREFERENCE
```

`MACHINE_FACT` is not an authority layer. A fresh fact may satisfy a
prerequisite or lower a usable limit; it can never raise a limit or define
business semantics. Lower authority cannot weaken a higher constraint.

## Non-configurable system invariants

The following remain outside every configurable surface: attributable Human
Authority; Evidence and Certification integrity; DAG dependency semantics;
stale/replay rejection; project/repository/HEAD/mission/generation binding;
fail-closed handling of failure, contradiction and `UNKNOWN`; no silent
migration; argv execution without arbitrary shell; P6 authority separation;
maintenance freeze, controlled transition and operator continuity.

The resolver additionally fixes clean Git, observability, strict Health,
Reviewer, Human, Evidence and Certification requirements to true and arbitrary
executable trust to false. A directive attempting `skip_reviewer`, fake Human,
auto-certification, ignore-unknown-Health, disabled Evidence/observability or
arbitrary executable trust is retained as rejected provenance and has no
effect. Even the `HardSafetyPolicy` constructor cannot disable these values.

## Project configuration boundary

The existing P5 `ProjectConfiguration` remains the sole repository-persisted
configuration. It owns project identity, portable toolchain declarations,
structured verification commands, path policy, Markdown context sources,
Codex constraints within product ceilings and the existing mission-state Git
disposition. The complete immutable object is carried in the effective result;
it is not copied into another schema.

It cannot control Human identity, certification or Evidence semantics, schema
trust, precedence, arbitrary executable path trust, machine observations,
maintenance authority, fail-closed behavior, or migrations. Approval remains
`never`, clean Git remains mandatory, and sandbox values stay in the P4 closed
set.

## Safety, operations, and preferences

`HardSafetyPolicy` supplies explicit maximum concurrency, timeout and
remediation generation, maximum sandbox and minimum verification tier. The P4
product maximum of eight concurrent workers is enforced at construction.
Certified booleans cannot be set to unsafe values.

P6.7 resource budgets for Codex concurrency, execution time and remediation
generation are consumed directly with their existing policy class and exact
scope. Operational policies and preferences use closed typed directives only.
They may lower numeric limits, select read-only instead of workspace-write,
raise the verification tier or require an operator earlier. They cannot
certify, bypass Human, suppress proof or weaken Health/maintenance admission.

Preferences are the lowest authority. Unsafe requests are rejected with
`SYSTEM_INVARIANT`, `WEAKENS_HIGHER_AUTHORITY` or
`EXCEEDS_SAFETY_CEILING`; they are never applied silently.

## Machine-fact boundary

`MachineFactBinding` carries only a project/repository binding, UTC observation
time, platform fingerprint, optional authentic P7.5 Codex assessment and an
optional concurrency restriction proved by that assessment. Facts older than
five minutes, future, foreign or forged fail closed. A machine fact cannot be
constructed as a configuration layer and is never persisted as project
authority.

## Resolver API and deterministic conflict rules

```text
ConfigurationResolver.resolve(ConfigurationResolutionContext)
    -> EffectiveConfiguration
```

The output contains the unchanged P5 configuration, effective closed values,
per-value authority/source, applied hard ceiling, rejected overrides,
diagnostics, all scope/fact bindings and a deterministic SHA-256 fingerprint.
It carries a process-local resolver attestation to reject plain forged output;
this is not a defense against a hostile process controlling the code.

There is no dictionary merge and no last-write-wins. Numeric limits use the
minimum applicable value. Sandbox uses `read-only < workspace-write`.
Verification uses `STRICT > STANDARD > BASIC`. Mandatory true cannot become
false; prohibited false cannot become true. Duplicate same-level keys are an
invalid conflict rather than an ordering choice. Input order is canonical.

`verify_current(effective, context)` re-resolves from current inputs. A changed
configuration fingerprint, project/repository, HEAD, mission/generation,
platform/Codex binding, machine observation or policy result invalidates the
previous effective fingerprint.

## Input safety and drift

Directive keys, value types, verification tiers, sandbox and authority classes
are closed. Sources are bounded identities with SHA-256 provenance. Secret-like
identities, credentials, tokens, private-key concepts, shell fragments,
absolute executable paths and unbounded free-form metadata have no accepted
field. P5 continues to reject duplicate JSON keys in the only persisted config.

Authority-bearing configuration is never silently hot-reloaded. A change
during a mission requires reconstruction and a new effective fingerprint; a
generation change invalidates the prior result. Before Codex execution,
parallel dispatch or an operation following maintenance, the consumer must
call `verify_current` and then repeat the existing P4/P6 admission. The resolver
does not mutate, replan, migrate or transition state itself.

## Performance

Resolution is pure in-memory canonicalization and hashing. It performs no Git,
filesystem, toolchain or Codex subprocess probe and does not alter P7.2 test
selection. Machine facts are passed in from their owning discovery boundary.
The full pytest suite is intentionally outside P7.6 verification.
