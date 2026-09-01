# Generalization & Final Product Contract

## Status and evidence boundary

P7.1 defines the target contract; it certifies no new runtime capability. Its
baseline is the Phase 6 closure commit
`bde549d6397fa49ed65d72ef309cf7d1322f3380`. P0 through P6 are certified and
closed. The certified Phase 6 audited baseline remains
`23eef2e0a1a9d69a1fc7ffbe7232361a525824df`.

Observed product evidence includes `.venv`, Python 3.11.9, Windows AMD64, Git
2.55.0.windows.2, an installed-wheel campaign outside the source checkout, a
real single-role Codex canary, and a final Phase 6 suite of 1773 passed and 2
skipped in approximately 2 hours 14 minutes. These observations define the
current evidence boundary, not universal portability.

## Certified capability map

| Capability | Owning phase | Certified guarantee | Known limitation | Generalization requirement |
| --- | --- | --- | --- | --- |
| Foundation contracts | P0 | Repository truth, fail-closed policy, authority and lifecycle contracts | Primarily project-level documentary contracts | Bind them to an installable product and supported-environment contract |
| Deterministic Control Plane | P1 | Strict models, transitions, Evidence, Gates, Certification and persistent state | No cryptographic defense against a process controlling code and repository | Preserve authority while varying environment, repository and configuration |
| Sequential agentic workflow | P2 | Mandatory roles, remediation, Human Authority and restart-safe mission state | Multi-store writes are not a distributed transaction | Validate complete external-repository missions and recovery |
| Parallel execution plane | P3 | DAG, readiness, waves, real-Git worktrees, integration and transactional merge | No general distributed transaction manager | Prove supported Git/platform combinations and bounded scale |
| Codex runtime integration | P4 | Deterministic context/prompt, subprocess adapter, intake, replay protection and real serial canary | Real Codex parallel capacity is `UNKNOWN`; observed executable was Windows-specific | Define capability negotiation and required versus optional Codex capabilities |
| Repository deployment | P5 | Wheel installation outside checkout, portable module CLI, safe adoption and explicit upgrade | Toolchain recognition is not workflow execution; compatibility matrix is incomplete | Validate declared archetypes, install modes and version transitions in clean rooms |
| Production governance | P6 | Events, metrics, health, policies, budgets, incidents, diagnostics and persistent maintenance | Cooperative single-writer store, no autonomous recovery, high suite runtime | Prove accumulation/rotation/soak and retain authority separation at product scale |

## Falsifiable definition of a generic product

`AGENTIC_ENGINEERING_OS` is a generic production product only when all of the
following are true for an explicitly supported environment and repository:

1. A published artifact installs in a fresh environment and operates without
   importing files or resources from the source checkout.
2. Product behavior does not require this repository, a personal absolute
   path, the launcher's current directory, internal fixtures or undeclared
   machine state.
3. Repository identity is supplied or reconstructed from the target Git
   repository; authority created for one repository cannot be reused for
   another.
4. Supported platforms, filesystem semantics, Git versions, repository
   archetypes, toolchains, Codex capabilities and installation modes are an
   explicit closed matrix.
5. Project layout and commands come from observed manifests plus explicit
   configuration. Inference never becomes authority and no hidden project
   template is required.
6. The same canonical inputs and repository state produce the same plan and
   authority decision, excluding explicitly observational fields such as
   timestamps and process identifiers.
7. Missing prerequisites, unsupported versions, ambiguous paths, unavailable
   capabilities and stale or corrupt state are reported and refused
   fail-closed.
8. A clean-room campaign can reproduce installation, adoption, execution,
   observation, restart/recovery and final result from declared inputs.

A dependency is permitted only when its name, supported range, discovery rule,
configuration source and failure behavior are part of the product contract.

## Final product guarantee

For an immutable installed release artifact, on a platform and Git version in
the supported matrix, against a repository satisfying the supported-repository
contract and an explicit valid project configuration, the product shall:

- inspect and classify repository facts without mutation;
- plan and apply adoption or a registered migration only through its bounded,
  explicitly authorized path;
- execute a supported sequential mission, and parallel work only when the
  required runtime capability is positively established;
- preserve repository, mission, generation, commit and worktree bindings;
- accept only canonical validated role results and preserve deterministic
  Control Plane and Human Authority boundaries;
- persist sufficient state to reconstruct interrupted work without blind retry;
- expose truthful events, metrics, health, governance, incidents, diagnostics
  and maintenance admission without treating observations as authority;
- fail closed on unknown, ambiguous, stale, corrupt, incompatible or
  insufficiently attributable input; and
- produce a reproducible final result and evidence set for the declared
  operation.

This guarantee is tested by the compatibility matrix, clean-room campaign,
bounded soak and final adversarial certification. A single successful demo is
not sufficient.

The product does not guarantee arbitrary languages or layouts, every Git or
Codex version, Linux/macOS support, real Codex parallel capacity, autonomous
remediation, a web dashboard, distributed transactions, perfect rollback,
absence of all hostile-process attacks, or business success of generated code.

## Generalization matrix

| Dimension | Current status | Repository-based justification | P7 disposition |
| --- | --- | --- | --- |
| A. OS/platform | `PARTIALLY CERTIFIED` | All certifying environments and the real canary are Windows; Python portability alone is not evidence for Linux/macOS | Windows is the V1 target; audit abstractions and do not claim other OSes |
| B. Filesystem/path semantics | `PARTIALLY CERTIFIED` | Canonical paths, containment, symlink refusal, atomic replace and exclusive locks are tested on Windows | Validate drive, case, junction, long-path and lock semantics in the target matrix |
| C. Git behavior/version | `PARTIALLY CERTIFIED` | Real Git repositories/worktrees are heavily exercised; Git 2.55.0.windows.2 is currently observed, but no supported range exists | Declare and test a Git version/behavior matrix |
| D. Repository archetype | `PARTIALLY CERTIFIED` | Python, Node, mixed and Rust repositories were inspected/adopted | Prove full supported lifecycle per declared archetype |
| E. Language/toolchain | `PARTIALLY CERTIFIED` | Markers and candidate commands are recognized; discovered commands were intentionally not executed in P5 | Separate discovery from supported command execution and validate each claim |
| F. Codex runtime/capabilities | `PARTIALLY CERTIFIED` | Subprocess adapter and real serial canary passed; real parallelism remains `UNKNOWN` | Negotiate capabilities and refuse unsupported paths |
| G. Installation mode | `PARTIALLY CERTIFIED` | Wheel outside checkout and module invocation passed on Windows; console shim is policy-dependent | Certify declared fresh-install and upgrade modes |
| H. Configuration/policies | `PARTIALLY CERTIFIED` | Strict versioned project configuration exists, but environment/profile combinations are not a product matrix | Define portable defaults, explicit overrides and precedence |
| I. Upgrade/backward compatibility | `PARTIALLY CERTIFIED` | Only registered migrations are allowed and some edges are deliberately unsupported | Publish format compatibility and migration policy per release |
| J. Operator UX | `PARTIALLY CERTIFIED` | Structured CLI diagnostics exist; operator acceptance and complete task flow are not certified | Run bounded operator acceptance without adding a dashboard requirement |
| K. Performance/scalability | `UNKNOWN` | Correctness passed, but the final suite took about 2 h 14 min and no workload envelope is defined | Profile first, then set and meet measured budgets |
| L. Clean-room reproducibility | `PARTIALLY CERTIFIED` | P5 installed a wheel outside checkout across external temporary repositories, but not the full P2–P6 lifecycle | Run the complete installed-product clean-room protocol |

`UNKNOWN` is not automatically a release blocker. It blocks only a capability
claimed by the supported V1 matrix or the final product guarantee.

## V1 platform target

V1 is officially **Windows-first**. The supported platform matrix must name
the Windows editions/architectures, Python and Git ranges actually certified.
The currently observed evidence is Windows AMD64, Python 3.11.9 and Git
2.55.0.windows.2; it does not yet establish the final supported ranges.

Linux and macOS remain `UNKNOWN` and outside the certified V1 guarantee unless
P7.3 and later clean-room evidence explicitly add them. P7.3 must still remove
implicit personal paths and cwd assumptions, validate executable resolution,
subprocess termination, case/drive semantics, symlinks/junctions, atomic
replacement and filesystem locking on Windows. PowerShell may be a declared
operator/test dependency only where explicitly named; product execution must
not acquire an undeclared shell dependency.

## Repository and toolchain target

The V1 supported repository is a real Git repository at an arbitrary canonical
absolute path, within the declared Git/platform matrix, with explicit
configuration and a state compatible with the requested operation. Cleanliness,
branch/HEAD and worktree requirements are operation-specific contracts, not a
hidden global assumption. No fixed source directory layout is required.

| Archetype/toolchain | Current product claim |
| --- | --- |
| Python | `SUPPORTED` for reconnaissance and adoption; candidate build/test commands are `DISCOVERABLE`, while generic execution still requires explicit configuration and P7 evidence |
| Node | `SUPPORTED` for reconnaissance and adoption; npm/pnpm/yarn commands are `DISCOVERABLE`, not yet certified as executed workflows |
| Python + Node | `SUPPORTED` for reconnaissance and adoption; cross-toolchain workflow execution is `UNKNOWN` |
| Rust | `SUPPORTED` for reconnaissance and adoption; Cargo workflow execution is `UNKNOWN` |
| Other languages/layouts | `OUT OF SCOPE` for V1 unless P7.4 adds them to the closed matrix; otherwise they remain observable without a support claim |

P7.4 evidence must use external repositories and real files/Git. Each supported
execution claim requires the real declared toolchain command, exit/result
capture, failure behavior and restart implications; manifest recognition alone
cannot satisfy it.

## Codex portability decision

The V1 transport remains the shell-free subprocess boundary around `codex
exec`, with executable path/version/hash observed per execution and a minimal
explicit environment. The real single-role canary is required for release.

Real Codex parallelism remaining `UNKNOWN` is acceptable only because it is not
a mandatory V1 guarantee. Deterministic parallel planning and coordination may
remain supported, but real parallel dispatch must be capability-gated:
positive evidence permits it; `UNKNOWN` or unavailable capacity refuses or
falls back only through an explicitly requested sequential plan. No result may
silently advertise real parallel execution. P7.5 either certifies a bounded
real-parallel profile or records it as optional and unsupported by the V1
release matrix.

## Performance and test debt

The observed Phase 6 baseline is 1773 passed and 2 skipped in approximately 2
hours 14 minutes. P7.2 is mandatory Test Performance Engineering work.

P7.2 must first record reproducible wall time, per-test durations, fixture and
subprocess/Git setup costs, variance and machine context. It then sets a
numeric full-suite budget and regression threshold from that profile before
optimization. Acceptance requires a strong measured reduction against the
recorded baseline, stable repeated runs and unchanged required coverage. It
may not delete adversarial coverage, hide slow tests, replace real-Git evidence
with unjustified mocks or introduce races/flakiness. No arbitrary duration is
certified by P7.1.

## Version and compatibility contract

- The current package version `0.1.0` is pre-v1 and is not the final v1 claim.
- A v1 release declares its package version, Python/Git/Codex ranges, project
  configuration version, schema set and every persisted runtime format.
- Package versions and persisted schema versions are independent. A package
  upgrade never implies or performs a project migration.
- `Package upgrade != project migration` remains an invariant.
- Loaders accept current formats, apply only explicitly registered historical
  migration edges through the authorized migration service, and reject unknown
  future, corrupt or unsupported formats fail-closed.
- Backward compatibility means the published matrix either loads a format or
  offers a tested explicit migration. No unlisted edge is implied.
- Forward compatibility is not presumed: a newer persisted format is refused
  by an older product unless explicitly documented otherwise.
- Deliberately unsupported migrations, including `executions.json` 1.0 to 1.1,
  remain named in the matrix with operator recovery guidance.
- A release candidate is an immutable wheel plus source commit, dependency
  lock/constraints, resource set and cryptographic digests. Any byte or
  compatibility-contract change creates a new candidate.

## Clean-room final validation

The final campaign starts from a fresh environment and an immutable candidate
artifact, with no source checkout on import/resource paths. It uses fresh
external repositories at unrelated absolute paths and performs:

```text
install → inspect → plan → adopt → execute mission
→ observe/govern → interrupt/restart/recover → final result
```

Real Git and the real filesystem are mandatory for repository identity,
worktrees, merge, persistence, locks, corruption and recovery. The real
installed wheel and package resources are mandatory throughout. At least one
supported serial mission uses real Codex. Real parallel Codex is mandatory only
if the release matrix claims it.

Deterministic fakes remain acceptable for exhaustive transport errors,
timeouts, malformed outputs, injected crashes, resource exhaustion and rare
failure interleavings, provided the real boundary has a canary and fake results
never replace a claimed real capability. The campaign records artifact digest,
environment, commands, repository commits and final evidence without secrets.

## Release soak requirement

A bounded reproducible soak is required before final certification. Against one
immutable release candidate and a fixed scenario manifest/seed, it includes at
minimum:

- 10 sequential mission cycles across supported repository archetypes;
- 5 restart cycles at distinct persisted workflow/execution boundaries;
- 5 deterministic `SAFE` parallel-group cycles, plus 3 real Codex parallel
  cycles only if that capability is claimed;
- 5 injected failure/recovery cycles with no autonomous repair;
- event-store rotation through at least 3 segments and verification of metrics,
  incidents and diagnostics after restart;
- 2 clean install/uninstall environments and cleanup verification.

Every iteration reconstructs Git and persistent state, checks resource cleanup
and detects accumulation, replay, stale authority or flakiness. Expected
fail-closed outcomes are successes only when explicitly declared by the
scenario. Any unexplained divergence or intermittent failure is blocking. No
daemon is required.

## Final project Definition of Done

After P7.CLOSE, `AGENTIC_ENGINEERING_OS v1 CERTIFIED` may be declared only if:

- the generic target and supported/unsupported matrices are explicit;
- platform, repository, toolchain, installation and Codex capability claims
  have matching reproducible evidence;
- deterministic Control Plane/Human Authority, restart/recovery and
  governance/observability guarantees remain intact;
- package, schema, runtime-format and migration compatibility are published;
- performance is profiled, budgeted and within the accepted measured budget;
- the installed immutable release candidate passes clean-room validation and
  the bounded soak;
- final adversarial product certification passes with zero blocking findings;
- the certified artifact, source commit and digests are immutable and exactly
  identified; and
- all required evidence is attributable, secret-free and repository-backed.

Failure, `UNKNOWN` inside a claimed capability, missing evidence or a changed
release candidate blocks certification.
