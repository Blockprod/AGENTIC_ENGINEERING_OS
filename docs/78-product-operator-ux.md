# Product UX and Operator Acceptance

## Entrypoint and operator contract

The canonical V1 invocation is:

```text
<environment-python> -m agentic_engineering_os <command>
```

`agentic-os` is an optional convenience shim. Windows App Control / Code
Integrity may refuse unsigned generated shims; the product does not recommend
disabling that policy. Start with `python -m agentic_engineering_os --help`.

Every command returns a deterministic envelope containing `command`, `status`,
`mode`, `next_action`, `confirmations`, `authority_notice`, and the complete
service `result`. Compact `--json` and pretty human output preserve the same
information. Guidance is a suggestion, never Control Plane authorization.

Exit codes are:

- `0`: the command completed with an acceptable result;
- `1`: technical or unexpected product error;
- `2`: blocked, refused, incomplete, unknown, or operator attention required.

An exit code is not a Gate or Certification result.

## BEFORE UX matrix

| Operator goal | Command | Required input | Previous output | Next action clear? | Failure actionable? | Authority risk |
|---|---|---|---|---|---|---|
| discover product | `--help` | none | command names; P5 commands lacked summaries | partial | not applicable | shim looked primary |
| understand repository | `inspect` | repository | full RepositoryProfile | no uniform next action | findings present but next step implicit | low |
| know adoption state | `status` | repository | status plus preparation | state clear, action implicit | findings nested | low |
| prepare adoption | `plan` | repository/config | exact plan | apply mechanics in fields | blockers nested | dry-run not prominent |
| adopt | `init` | config; confirmations if required | preparation/result | `--apply` documented | confirmation syntax spread across output/help | mutation mode could be clearer |
| assess/migrate versions | `upgrade` | repository; confirmations if required | exact P5.9 plan/result | edge visible, next step implicit | blockers present | dry-run default not prominent |
| evaluate Health | `health` | adopted repository/scope | Health result | remediation implicit | source reasons present | HEALTHY could be overread |
| inspect metrics | `metrics` | adopted repository/scope | metrics snapshot | next step implicit | completeness visible | no direct authority |
| inspect incidents | `incidents` | adopted repository/scope | incident records | recovery step implicit | incident state visible | observation could be overread as recovery |
| aggregate diagnostics | `diagnose` | adopted repository/scope | Health/governance/budget/incidents | attention visible, action implicit | detailed facts present | ALLOW already qualified in result |

The underlying P5/P6 structures were already strict and informative. P7.9
therefore adds only a common operator projection and discoverability text; it
does not rewrite service results.

## Command discovery and mutation boundary

The root help names and describes all commands. `inspect`, `status`, `plan`,
`health`, `metrics`, `incidents`, and `diagnose` are explicitly read-only.
`init` and `upgrade` are dry-run by default. Their `--apply` help is labelled
`MUTATING`; no universal `--yes` exists.

The output `mode` is one of `READ_ONLY`, `DRY_RUN`, `APPLY_ATTEMPT`,
`APPLY_REFUSED`, or `APPLY_RESULT` on the applicable paths. A suggestion never
executes an action.

## Status and next-action model

The projection covers the major operator states without creating an action
engine:

| Status | Deterministic safe next action |
|---|---|
| `NEEDS_CONFIGURATION` | select/create ProjectConfiguration, then dry-run `plan` |
| `NEEDS_HUMAN_CONFIRMATION` | review every exact operation and confirmation, then explicitly apply |
| `READY_TO_APPLY` | review the dry-run, then rerun the same command with `--apply` |
| `ADOPTED` | use read-only `diagnose`; no adoption action needed |
| `PARTIAL_OR_INCONSISTENT` | do not auto-apply; inspect findings/diagnostics |
| `UPGRADE_REQUIRED` | run upgrade without `--apply` to inspect registered support |
| `BLOCKED` / `REFUSED` | correct reported codes, blockers, and bindings, then repeat dry-run |
| `UNKNOWN` / `DEGRADED` | inspect diagnostics/incidents and resolve missing facts |
| `FROZEN` / `RECOVERY_REQUIRED` | start no new work; request explicit operator recovery procedure |

`next_action` is deterministic guidance derived from the observed status. It
does not claim that the suggested action will pass its own preflight.

## Failures and Human confirmation

Expected failures emit structured codes/details without a normal stack trace.
The unchanged nested result retains detailed findings and blockers. The common
next action tells the operator to resolve facts rather than bypass policy.
Invalid configuration, dirty Git, stale plans, unsupported migrations,
corrupt/foreign state, unavailable Codex capabilities, Health unknown,
resource exhaustion, frozen maintenance and recovery-required conditions all
remain fail-closed.

For every Human-required operation, `confirmations` projects:

- the exact confirmation ID;
- operation type and target path;
- why Human authority is required;
- the consequence, limited to that controlled apply attempt;
- exact `--confirm <ID> --confirmed-by Human/<identity>` syntax.

Each ID is separate. Silence and Codex identities never satisfy Human
authority.

## Authority language

The common envelope states `OPERATOR_GUIDANCE_ONLY_NOT_AUTHORIZATION`.
The following distinctions remain explicit:

```text
READY != CERTIFIED
HEALTHY != CERTIFIED
Governance ALLOW != CONTROL PLANE AUTHORIZATION
MIGRATION_AVAILABLE != MIGRATED
RECOVERY_REQUIRED != RECOVERED
```

The UX does not label readiness as approval, Health as certification, or
absence of detected conflict as universal safety.

## Operator acceptance matrix

| Scenario | Command | Output | Exit | Next action | Expected | Observed | Result |
|---|---|---|---:|---|---|---|---|
| A fresh repository | inspect/status/init | `SUPPORTED`, `NEEDS_CONFIGURATION`, dry-run `READY_TO_APPLY` | `0/2/0` | configure, review, explicit apply | guided adoption without checkout knowledge | installed journey matched | PASS |
| B already adopted | status | `ADOPTED` | `0` | optional read-only diagnose | adoption is unambiguous | installed journey matched | PASS |
| C needs configuration | status | actionable envelope | `2` | create/select config then plan | configuration gap is explicit | dedicated and impacted tests matched | PASS |
| D Human required | plan/init | distinct IDs, targets and syntax | `0` dry-run | Human reviews exact operations | no implicit or grouped confirmation | dedicated and impacted tests matched | PASS |
| E unsupported migration | upgrade | `BLOCKED` with blocker | `2` | inspect supported edges; no generic migration | unsupported edge stays blocked | impacted test matched | PASS |
| F corrupt state | status | `PARTIAL_OR_INCONSISTENT` | `2` | inspect findings; no auto-apply | corruption stays fail-closed | impacted test matched | PASS |
| G Health unknown | health/diagnose | attention with source reasons | `2` | inspect missing facts and incidents | UNKNOWN is not healthy | impacted test matched | PASS |
| H resource exceeded | diagnose | budget attention | `2` | resolve limit/usage; no bypass | limit blocks acceptable result | diagnostic contract and projection matched | PASS |
| I frozen | diagnose | maintenance facts | `2` | refuse new work; operator procedure | no new work or auto-recovery | diagnostic contract and projection matched | PASS |
| J recovery required | diagnose/incidents | recovery facts | `2` | explicit operator recovery only | required is not recovered | diagnostic contract and projection matched | PASS |
| K Codex unavailable/capability missing | diagnose | capability facts | `2` | inspect binding; do not disable WDAC | unavailable stays non-authoritative | diagnostic contract and projection matched | PASS |
| L successful diagnostics | health/metrics/diagnose | complete/healthy result | `0` | no operator attention indicated | success grants no extra authority | impacted tests matched | PASS |

Installed first-run and existing-repository journeys exercise help, inspect,
status, init dry-run/apply, adopted status, and diagnose without checkout
knowledge. Existing user files are preserved. The blocked diagnostic journey
ends with an exact attention status and safe next action; P7.9 performs no
automatic recovery.

## Security and scope

Adversarial acceptance confirms: `--yes` is absent; fake Codex Human is
refused; shell/secret inputs are not executed or echoed; traversal, foreign
repositories and stale inputs remain blocked; expected failures show no stack
trace; unsupported state is not advertised as automatically recoverable; and
no wording advises disabling WDAC/App Control.

P7.9 adds no dashboard, wizard, renderer framework, remote UI, autonomous
action, recovery, authority, P7.10 behavior, or change to persisted contracts.
