# Phase 5 Certification

- Phase: `Phase 5 — Repository Deployment / Installation Kit`
- Status: `CERTIFIED`
- Certified audited baseline: `f4ac97da6e7af4ec1d3bb1eac56a62120a459d3a`
- Certification date: `2026-08-31`
- Certifying environment: `.venv / Python 3.11.9`
- Final audit: `P5.12-R3 PASS`
- Phase result: `P5.12 PASS`
- Final suite: `1453 passed, 2 skipped, 0 failed`
- Final targeted re-certification: `42/42 PASS`
- Installed-product evidence: wheel outside the source checkout and
  multi-repository campaign `14/14 PASS`
- Missions: `P5.1` through `P5.12` completed
- Blocking findings: `0`
- Recommendation: `PHASE 5 CERTIFICATION RECOMMENDED`

## Certified capabilities

- Deployment Architecture and `ProjectConfiguration`.
- Deterministic `RepositoryReconnaissance` and Initialization Planner dry-run.
- Safe Repository Initializer and bounded `AGENTS.md` integration.
- Minimal Runtime State Bootstrap and Existing Repository Adoption.
- Explicit Upgrade/Migration.
- Installable wheel with package-native resources and CLI entrypoints.
- Canonical portable invocation:
  `<environment-python> -m agentic_engineering_os`.
- Optional convenience launcher: `agentic-os`.
- Multi-repository validation and cross-repository authority isolation.
- Human Authority preservation and fail-closed stale/replay protections.

## Authority guarantees

- Authority created for repository A cannot be reused for repository B.
- Inference never becomes project authority.
- Human confirmation remains exact and bound to its operation and repository.
- No silent overwrite and no fabricated mission or state are permitted.
- A package upgrade is not a project migration.
- Unsupported migrations remain refused.
- Runtime and product resources do not depend on the source checkout.
- The CLI remains a thin facade over the existing application services.

## Known non-blocking limitations

1. Windows Enterprise Code Integrity may block newly generated unsigned
   console-script shims such as `agentic-os.exe`. The portable module invocation
   is the canonical supported path; the convenience shim remains
   policy-dependent.
2. Migration `executions 1.0 → 1.1` is intentionally unsupported.
3. No cryptographic protection is provided against a process hostile enough to
   control both code and repository beyond the documented threat model.
4. Forty-six unreachable Git blobs and six unreachable Git trees are outside
   the certified audited baseline and remain non-authoritative.

## Baseline semantics

- Certified audited baseline:
  `f4ac97da6e7af4ec1d3bb1eac56a62120a459d3a`.
- Phase 5 closure commit: the commit containing this certification record. It
  is distinct from and does not replace the certified audited baseline.
