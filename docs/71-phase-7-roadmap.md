# Phase 7 Roadmap

## Basis

Phase 7 generalizes the certified P0–P6 product without replacing their
authority model. The remaining gaps are test runtime, Windows/platform
contracts, full repository/toolchain execution evidence, Codex capability
portability, configuration profiles, release compatibility, operator
acceptance, complete clean-room validation and repeated-run confidence.

## Validation of the proposed roadmap

| Proposed step | Decision | Repository-based reason |
| --- | --- | --- |
| P7.1 Generalization & Final Product Contract | `KEEP` | A falsifiable product boundary and final guarantee are prerequisites |
| P7.2 Test Performance Engineering | `KEEP` | The 1775-test Phase 6 run took about 2 h 14 min; later matrices would multiply that cost |
| P7.3 Platform / Environment Abstraction | `KEEP` | Only Windows is certified and filesystem/process assumptions need an explicit target contract |
| P7.4 Repository Archetype Generalization | `KEEP` | P5 proved reconnaissance/adoption, not complete toolchain workflows |
| P7.5 Runtime / Codex Capability Portability | `KEEP` | Real serial Codex passed while real parallel capacity remains `UNKNOWN` |
| P7.6 Configuration & Policy Generalization | `KEEP` | Current strict configuration lacks a certified environment/profile matrix |
| P7.7 Installation / Upgrade Compatibility Matrix | `REORDER` to P7.8 | The matrix depends on the version/backward-compatibility contract |
| P7.8 Backward Compatibility & Versioning | `REORDER` to P7.7 | Version semantics must precede installation and migration combinations |
| P7.9 Product UX / Operator Acceptance | `KEEP` | CLI diagnostics exist, but complete operator workflows are not accepted evidence yet |
| P7.10 Clean-Room Product Validation | `KEEP` | P5 clean installation did not cover the full P2–P6 lifecycle |
| P7.11 Release Candidate & Production Soak | `KEEP` | Restart, accumulation, rotation, cleanup and flakiness need repeated-run evidence |
| P7.12 Final Adversarial Product Certification | `KEEP` | Final claims require independent bypass and evidence audit |
| P7.CLOSE | `KEEP` | Closure must bind the immutable audited candidate to the v1 certification record |

No step is removed or merged because each owns a distinct acceptance boundary.

## Canonical roadmap

| Step | Objective | Depends on | Required exit evidence |
| --- | --- | --- | --- |
| P7.1 | Define generic product, V1 target, final guarantee and roadmap | P0–P6 closed | This contract and dependency-ordered roadmap |
| P7.2 | Profile and reduce test runtime without weakening coverage | P7.1 | Reproducible profile, data-derived numeric budget, repeated stable suite results and coverage inventory |
| P7.3 | Establish Windows-first platform/environment abstraction | P7.1–P7.2 | Supported Windows/Python/Git matrix; path, executable, subprocess and lock probes; no personal/cwd dependency |
| P7.4 | Generalize supported repository archetypes and toolchain execution claims | P7.3 | External real-Git archetype matrix distinguishing inspect/adopt/execute support |
| P7.5 | Make Codex runtime capability requirements portable and explicit | P7.3–P7.4 | Real serial canary, capability negotiation, fail-closed unavailable paths and honest parallel status |
| P7.6 | Generalize configuration and policy profiles | P7.3–P7.5 | Versioned portable configuration, precedence rules, safe defaults and profile matrix |
| P7.7 | Define backward compatibility and release versioning | P7.6 | Package/schema/runtime compatibility policy, immutable RC semantics and closed migration registry |
| P7.8 | Certify installation and upgrade compatibility matrix | P7.3–P7.7 | Fresh install, supported upgrade/migration and explicit refusal cases from installed artifacts |
| P7.9 | Validate product UX and operator acceptance | P7.6–P7.8 | Bounded task-based CLI acceptance with truthful actionable errors and no dashboard dependency |
| P7.10 | Execute full clean-room product validation | P7.2–P7.9 | Installed artifact completes external-repository adoption, mission, governance and restart/recovery |
| P7.11 | Freeze a release candidate and run bounded production soak | P7.10 | Immutable digests, fixed soak manifest, repeated cycles, cleanup/accumulation report and zero unexplained flakes |
| P7.12 | Perform final adversarial product certification | P7.11 | Complete compatibility/authority bypass audit, final suite and zero blocking findings |
| P7.CLOSE | Record final product certification | P7.12 PASS and recommendation | Distinct audited baseline and closure commit; `AGENTIC_ENGINEERING_OS v1 CERTIFIED` only if the Definition of Done holds |

## Ordering rules

- P7.2 precedes expanded matrices so performance debt is not multiplied.
- Platform facts precede repository, runtime and configuration claims.
- Version semantics precede installation/upgrade certification.
- UX validates the stable supported surface, not an intermediate one.
- Clean-room validation precedes RC freeze; soak uses one immutable candidate;
  adversarial certification audits exactly that candidate.
- A later step may start only after its dependencies have verifiable evidence.
  `FAIL`, required `UNKNOWN`, candidate drift or missing evidence blocks progress.

P7.1 creates no P7.2+ implementation and does not certify any roadmap item.
