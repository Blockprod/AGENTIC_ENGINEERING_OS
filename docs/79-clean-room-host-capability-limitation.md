# P7.10 Host Capability Limitation

## Product and host boundary

The product defines explicit operational contracts for `REPOSITORY_READ`,
`WORKSPACE_EDIT`, `COMMAND_EXECUTION`, `STRUCTURED_RESULT`, and
`GIT_OBSERVATION`. A contract does not prove that its capability is available
on a particular host.

For the certifying host and Codex executable assessed by P7.10-R4A/R4B:

| Capability | Observed status |
| --- | --- |
| `REPOSITORY_READ` | `PROVEN` |
| `STRUCTURED_RESULT` | `PROVEN` |
| `WORKSPACE_EDIT` | `BLOCKED_BY_HOST_POLICY` |
| `COMMAND_EXECUTION` | `BLOCKED_BY_HOST_POLICY` |

The Architect real boundary passed. The direct Python argv command was also
blocked. Implementer and Tester were therefore refused before launch, without
a process identifier or start time. No security control was weakened.

## P7.10 consequence

The product correctly refuses a role when a required operational capability is
not positively proven. This is capability-aware fail-closed behavior, not proof
that the unavailable capability is supported and not evidence that the product
is broken.

P7.1 nevertheless requires the installed clean-room artifact to complete a
supported sequential mission using real Codex. Because the mutating
Implementer/Tester path is unavailable on this certifying host, P7.10 remains
environmentally blocked. This observation does not redefine or satisfy the V1
clean-room contract.

No further attempt on this host will use PowerShell, `cmd.exe`, a direct Python
command, a wrapper shell, `danger-full-access`, security-policy changes, or
executable renaming or copying to circumvent the host policy.
