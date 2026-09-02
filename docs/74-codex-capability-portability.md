# Codex Capability Portability

## Scope and authority

P7.5 makes the local `codex exec` transport contract explicit and portable
between executable installations. A capability assessment is a technical fact;
it cannot create Evidence, approve a Gate, transition state, or certify work.
`CODEX EXECUTES. CONTROL PLANE DECIDES.` No provider, plugin runtime, remote
Codex integration, UI automation, or autonomous resume is introduced.

## BEFORE matrix

| Capability | Required / optional | Discovered status before P7.5 | Runtime dependency | Previous failure behavior |
|---|---|---|---|---|
| Non-interactive `exec` | Required | `SUPPORTED` | every role | spawn failed only after binding checks |
| Prompt through stdin | Required | `SUPPORTED` | prompt confidentiality and size | no independent admission |
| Explicit cwd | Required | `SUPPORTED` | repository/worktree binding | cwd binding blocked, option availability was assumed |
| JSONL | Required | `SUPPORTED` | transport event intake | malformed output blocked after launch |
| Output schema | Required when configured | `SUPPORTED` | structured RoleResult | option availability was assumed |
| Read-only sandbox | Required by non-mutating role | `SUPPORTED` | least privilege | option availability was assumed |
| Workspace-write sandbox | Required by mutating role | `SUPPORTED` | scoped mutation | option availability was assumed |
| Approval `never` | Required | `SUPPORTED` | non-interactive execution | option availability was assumed |
| Exit/stdout/stderr observation | Required | `SUPPORTED` | factual transport outcome | parent adapter captured it |
| Parent timeout/cancellation | Required | `SUPPORTED` | bounded execution | parent adapter killed the direct child |
| Session/thread identity | Optional | `SUPPORTED` in the P4 canary | traceability | missing identity did not authorize success |
| Resume interface | Optional | `SUPPORTED` | possible session continuation | not used by restart safety |
| Reliable side-effect recovery | Optional | `UNKNOWN` | unsafe retry avoidance | P4.7 reconstructed and failed closed |
| Environment control | Required | `SUPPORTED` | bounded secret exposure | closed allowlist enforced |
| Independent process parallelism | Optional | `UNKNOWN` | P4.9 concurrent transport | fake subprocesses only; real capacity not claimed |

The gap was not the subprocess mechanics: required CLI options were not
reassessed as a coherent, executable-bound set before every launch.

## Closed model and V1 admission

`CodexCapability` contains only the sixteen named capabilities above.
`CodexCapabilityStatus` contains only `SUPPORTED`, `UNSUPPORTED`, and
`UNKNOWN`. `CodexCapabilityAssessment` covers the complete enum in canonical
order and binds findings to canonical executable path, SHA-256, exact observed
version, discovery provenance, platform, observation time, and the exact tested
parallelism when applicable. There are no optimistic defaults.

Every sequential launch requires non-interactive exec, stdin, cwd, JSONL,
approval `never`, exit/stream observation, parent timeout/cancellation, bounded
environment, and the selected sandbox. Output schema support becomes required
when a schema is requested. `UNSUPPORTED`, `UNKNOWN`, missing assessment, or an
identity mismatch returns a not-started observation before `Popen`.

Resume and real parallelism are optional for a sequential V1 role. P4.9 uses
an explicit conservative policy: absent, stale, forged, `UNKNOWN`, or
insufficiently tested parallel capability serializes the group to one worker.
An authentic assessment may authorize no more than its tested concurrency;
the configured range 1–8 remains product policy, not a Codex capacity claim.

## Discovery, binding, and drift

Static discovery resolves an explicit executable or bounded `PATH` result and
then freshly checks canonical path, file presence, SHA-256, and `--version`.
Only after those checks does it inspect bounded `--help`, `exec --help`, and
`exec resume --help` output. Malformed, timed-out, or non-zero observation is
`UNKNOWN`, never support. Commands use argv, `shell=False`, explicit cwd,
bounded output/environment, and do not print environment values.

Help findings may be reused only under the exact immutable
path/digest/version/launcher identity. Path, bytes, version, options, or file
availability drift forces reassessment; stale findings cannot authorize a new
identity. A project-local executable is refused unless a deliberately named
test-only injection boundary is enabled.

Safe active probes are used only where static help is insufficient. They run in
disposable Git repositories, use explicit cwd, read-only sandbox, approval
`never`, structured output and bounded prompts, and compare Git before/after.
They neither dump credentials nor weaken Windows security.

## P7.5 real observations

Observed executable:
`C:\Users\averr\.vscode\extensions\openai.chatgpt-26.825.41651-win32-x64\bin\windows-x86_64\codex.exe`.
Observed version: `codex-cli 0.151.0-alpha.7.1`. Observed SHA-256:
`9e2bf0ef4243c335ec60400260d8330dc309352b9cf08fad758496b47f1c136e`.

One real sequential structured execution passed in a disposable clean Git
repository. It returned the exact schema-conforming marker, emitted a thread
identity, used explicit cwd/read-only/never, and left Git unchanged. Therefore
session/thread identity is `SUPPORTED` for this assessed executable.

Two real independent `codex exec` processes then ran with distinct request
identities in separate disposable repositories. Their measured process
lifetimes overlapped, both returned valid structured markers, and both
repositories remained clean. Consequently
`INDEPENDENT_PROCESS_PARALLELISM = SUPPORTED` at tested concurrency `2` for
this executable identity. This proves transport-level concurrency only: it
does not prove capacity 8, arbitrary scale, semantic independence, or safe
same-worktree concurrency.

`RESUME_INTERFACE_PRESENT = SUPPORTED` from static CLI observation.
`RELIABLE_SIDE_EFFECT_RECOVERY = UNKNOWN`; no dangerous partial-effect probe
was attempted. P4.7 restart-safe reconstruction remains authoritative.

## Security, cost, and limitations

Capability assessments carry a process-local discovery attestation so a plain
constructed `SUPPORTED` object cannot authorize parallel dispatch. This is an
in-process integrity boundary, not a cryptographic defense against a hostile
process controlling code and repository. Capability facts never become
business authority.

Normal executions recheck file identity and version. Static help is cached
under the immutable identity so Codex is not actively probed for every role.
Real canaries remain explicit opt-in tests and P7.5 used only three bounded
Codex executions. No full test suite was run.
