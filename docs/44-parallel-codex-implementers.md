# Parallel Codex Implementers

## Portée

P4.9 relie un `PreparedParallelGroup` P3 déjà canonique à plusieurs exécutions
P4.8 indépendantes. Il ne planifie pas les groupes : `ConflictAnalyzer` et
`ParallelImplementerCoordinator` restent les seules autorités de composition.

```text
ParallelExecutionPlan + PreparedParallelGroup + état courant
→ revalidation P3 SAFE/assignments/worktrees
→ SingleRoleCodexExecutor par membre
→ ImplementerResult validé par membre
→ résultat de groupe ordonné
```

## Autorité du groupe

Avant lancement, `validate_prepared_group()` reconstruit le plan et exige le
groupe, la mission, la génération, la baseline, les membres et les assignments
`ACTIVE` exacts. Chaque worktree est réconcilié avec Git. P4.9 vérifie aussi que
les stories courantes sont `IN_PROGRESS` et que leurs scopes et dépendances
n'ont pas divergé du contexte SAFE préparé.

Un groupe forgé, stale, incomplet, dupliqué, `CONFLICT`, `UNKNOWN`, cross-
generation, lié à une autre baseline ou à des worktrees échangés est refusé
avant la création des threads.

## Concurrence et isolation

`ParallelCodexImplementerExecutor.execute_group()` utilise un
`ThreadPoolExecutor` standard-library. La limite configurée est comprise entre
1 et 8 et le nombre réel de workers est plafonné au nombre de membres.

Chaque membre possède un request ID dérivé distinct, son handoff, son
assignment, son cwd et un `SingleRoleCodexExecutor`. L'infrastructure crée un
ledger P4.7 dans chaque worktree ; il n'existe aucun ledger de groupe et aucune
écriture concurrente dans un store partagé.

La mission P3 autoritative reste `ORCHESTRATOR`. Une projection read-only,
bornée au handoff préparé et revalidée à chaque lecture, fournit à P4.8 la vue
Implementer nécessaire sans persister ni muter `MissionState`.

## Résultats et échecs

Les résultats sont restitués dans l'ordre canonique du groupe, indépendamment
de l'ordre de fin. Chaque membre conserve son request ID, execution ID,
`SingleRoleExecutionOutcome`, éventuel `ImplementerResult` et ses blockers.

Le groupe vaut `READY_FOR_P3_HANDOFF` uniquement si tous les membres ont un
`ImplementerResult` canonique `READY_FOR_TEST`. Un timeout, résultat malformed,
tool failure, incohérence Git, échec de persistance ou résultat cross-member
laisse le groupe `INCOMPLETE` sans effacer les succès indépendants.
Une cancellation peut cibler un assignment exact ; elle n'interrompt pas les
autres membres du groupe.

## Restart et frontière P3

Chaque reprise repasse par P3 puis P4.8/P4.7. Un membre `VALIDATED_NO_RERUN` est
revalidé sans subprocess ; `INTAKE_REPLAY_AVAILABLE` rejoue seulement P4.6 ;
un état recovery ou stale reste bloqué. Aucun cleanup automatique n'est fait.

P4.9 retourne les résultats Implementer validés mais ne les soumet pas
automatiquement à `ParallelImplementerCoordinator.submit_result()`. Il ne crée
aucun commit, ne complète aucun assignment, n'exécute ni Integration Gate, ni
merge, Tester, Reviewer, Certifier ou transition Control Plane.

## Limites

Les tests standard prouvent la concurrence de plusieurs subprocess fake avec
une barrière inter-processus, des cwd et stores distincts. Le vrai Codex reste
opt-in et sa capacité parallèle de production demeure `UNKNOWN`. P4.9 ne
contient ni scheduler générique, ni orchestration distribuée, ni intégration
VS Code P4.10.
