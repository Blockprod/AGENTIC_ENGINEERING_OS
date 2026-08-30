# Phase 4 Certification

- Phase: `Phase 4 — VS Code / Codex Runtime Integration`
- Status: `CERTIFIED`
- Certified audited baseline: `17535ec4847eea2300693b7ed23e3037e073db19`
- Certification date: `2026-08-30`
- Certifying environment: `.venv / Python 3.11.9`
- Codex CLI: `0.151.0-alpha.7.1`
- Final audit: `P4.11 PASS`
- Final suite: `1221 passed, 2 skipped`
- Missions: `P4.1` through `P4.11` completed
- Blocking findings: `0`
- Recommendation: `PHASE 4 CERTIFICATION RECOMMENDED`

## Certified capabilities

- Codex execution contract.
- Deterministic Context Builder and Prompt Compiler.
- Real `codex exec` capability discovery and runtime adapter.
- Structured canonical RoleResult intake.
- Restart-safe execution state with stale and replay protection.
- Single-role Codex execution.
- Parallel Implementer execution restricted to P3 `SAFE` groups.
- End-to-end runtime integration with the existing P2/P3 workflows.
- Preservation of Human and Control Plane authority.

Codex executes; deterministic OS / Control Plane decides.

## Authority and recovery guarantees

- Prompt and cognitive context remain non-authoritative data.
- Transport success never constitutes role, Gate, merge or certification
  success.
- Only canonical validated RoleResults can enter the existing workflows.
- No blind retry is allowed when side effects are uncertain.
- Intake-only replay and validated-result reuse occur only when proven safe.
- Stale generation, commit, worktree, execution and cross-member artifacts are
  refused fail-closed.
- `MERGED` is not `CERTIFIED`; final certification remains a Control Plane
  decision.

## Known non-blocking limitations

1. The real single-role Codex canary passed; real Codex parallel capability
   remains `UNKNOWN`. No guaranteed real parallel provider capacity is claimed.
2. V1 provides no cryptographic authenticity against a hostile process
   controlling both code and repository or state files.
3. Twenty-eight dangling Git blobs and one dangling Git tree are outside the
   certified audited baseline and do not form part of the certified state.

## Baseline semantics

- Certified audited baseline:
  `17535ec4847eea2300693b7ed23e3037e073db19`.
- Phase 4 closure commit: the commit containing this certification record. It
  does not replace the certified audited baseline.
