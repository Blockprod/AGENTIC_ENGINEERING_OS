# Single-Role Codex Execution

## Portée

P4.8 compose une seule exécution de rôle à partir des frontières P4 existantes :

```text
RoleHandoff + état autoritatif + artefacts amont exacts
→ CodexExecutionRequest dérivée
→ ContextBuilder
→ PromptCompiler
→ ExecutionState P4.7
→ CodexRuntimePort
→ CodexResultIntake
→ RoleResult canonique validé
```

`SingleRoleCodexExecutor.execute()` supporte uniquement Architect, Implementer,
Tester, Reviewer et Certifier. Il ne route aucun rôle suivant et ne constitue
ni un scheduler, ni un Orchestrator Codex, ni une intégration VS Code.

## API et dérivation d'autorité

Le caller fournit un `RoleHandoff`, une identité de request et un
`SingleRoleArtifacts` fermé. Il ne peut pas fournir un
`CodexExecutionRequest`, un scope, un worktree, un commit attendu, un sandbox
ou un contrat de résultat arbitraires.

Le coordinator relit `MissionState`, `ProjectState`, Git et le registre des
worktrees. Il dérive la User Story, le scope, la tâche, les vérifications, le
contrat de rôle, le schéma de sortie et, pour Implementer, l'unique assignment
`ACTIVE` correspondant exactement à mission, story, génération et baseline.
Les artefacts amont sont un set exact : aucun manque ni artefact supplémentaire
n'est accepté.

Le schéma de résultat est résolu depuis les ressources du package installé,
indépendamment du cwd et de la présence d'un dossier `schemas/` dans le
repository cible.

## Politiques fermées par rôle

| Rôle | Artefacts amont | CWD | Sandbox |
|---|---|---|---|
| Architect | aucun | repository primaire | `read-only` |
| Implementer | aucun | worktree assigné exact | `workspace-write` |
| Tester | ImplementerResult | repository primaire | `workspace-write` |
| Reviewer | ImplementerResult + TesterResult | repository primaire | `read-only` |
| Certifier | chaîne Architect→Reviewer + dossier repository | repository primaire | `read-only` |

Le Tester peut modifier uniquement ses fichiers de tests autorisés. Architect,
Reviewer et Certifier ne peuvent produire aucun effet Git. Il n'existe aucune
élévation automatique de sandbox ou d'approbation ; la policy reste `never`.

Le dossier Certifier inclut uniquement les Evidence liées à la User Story, à
ses Acceptance Criteria ou référencées par ses Gates, ainsi que les Gates de la
story. Cette closure bornée évite à la fois l'omission des Evidence AC et la
fuite cross-subject.

## Effets Git et validation

P4.5 observe désormais la liste canonique des chemins Git modifiés avant et
après l'exécution. P4.6 exige un état initial propre, une observation complète
et, pour Implementer/Tester, l'égalité exacte entre chemins physiques et
chemins déclarés dans le RoleResult. Les validateurs de rôle appliquent ensuite
le scope et les interdictions existantes. Une mutation non déclarée, hors
scope, read-only ou ambiguë interdit `VALIDATED`.

Cette évolution porte le ledger P4.7 en version `1.1`. Les anciens records
`1.0` ne sont pas devinés ou migrés implicitement.

## Restart et résultat

Le coordinator utilise exclusivement P4.7 :

- `SAFE_NOT_STARTED` lance Codex une fois ;
- `INTAKE_REPLAY_AVAILABLE` rejoue seulement P4.6 ;
- `VALIDATED_NO_RERUN` réhydrate et revalide le résultat sans subprocess ;
- tout autre état retourne une exécution incomplète/bloquée ou propage l'erreur
  applicative déterministe.

Pour Implementer, le Context Builder accepte qu'un assignment physiquement
exact et resumable porte les effets Git d'une tentative antérieure. Cette
tolérance n'autorise jamais un nouveau lancement : P4.7 exige toujours un état
Git initial exactement propre pour `SAFE_NOT_STARTED`. Elle permet seulement
de revalider un résultat terminé ou de rapporter l'état de recovery sans retry.

Un résultat accepté signifie seulement qu'un RoleResult canonique a passé
P4.6. Son verdict peut lui-même être `BLOCKED` ou demander remédiation. Aucun
retry aveugle, rollback Git ou progression métier automatique n'est effectué.

## Frontière d'autorité

Le coordinator n'appelle jamais `EvidenceRecorder`, `GateEvaluator`,
`CertificationService`, `HumanApprovalService`, `StateTransitionService` ou
`ControlLoop`. Il ne modifie ni `ProjectState`, `MissionState` ni
`WorktreeRegistry`.

Le `SingleRoleExecutionOutcome` contient seulement l'identité d'exécution, le
statut P4.7, l'éventuel RoleResult validé et des blockers opérationnels. Il ne
contient aucune Evidence, Gate, Certification ou autorisation de transition.

## Limites

La suite standard utilise un subprocess fake et reste offline. Le canary Codex
réel demeure opt-in. Le parallélisme Codex P4.9, l'E2E VS Code P4.10,
l'autonomie et les boucles de retry restent hors scope.
