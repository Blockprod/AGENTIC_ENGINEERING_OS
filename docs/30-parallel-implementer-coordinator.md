# Parallel Implementer Coordinator

## Frontière et inputs

`ParallelImplementerCoordinator` transforme une `ParallelCoordinationInput`
explicite en un `ParallelExecutionPlan`. L'input lie `mission_id`,
`workflow_generation`, `wave_index`, `WavePlan`, `ConflictAnalysis`,
`ProjectState`, `MissionState` et `baseline_commit`. Le coordinateur reconstruit
le DAG, la readiness, les Waves et les conflits par les composants canoniques.
Une divergence, une mission inactive/bloquée, une génération incohérente ou
une baseline différente de `MissionState.observed_commit` bloque.

Seule la première Wave du plan canonique courant peut être coordonnée. Une Wave
future n'est jamais lancée par anticipation. La sérialisation entre groupes
d'une même Wave est opérationnelle et ne crée ni dépendance ni edge DAG.

## Grouping déterministe

Les membres conservent l'ordre du `WavePlan` (priorité croissante puis ID). Le
coordinateur applique un first-fit greedy : chaque membre rejoint le premier
groupe où toutes ses paires sont `SAFE`, sinon un nouveau groupe est créé.
`CONFLICT`, `UNKNOWN` et une paire absente interdisent la cohabitation.

Chaque membre apparaît exactement une fois, les groupes sont indexés de zéro
et leur ordre interne reste celui de la Wave. La stratégie est correcte,
déterministe et fail-closed ; elle ne prétend pas minimiser le nombre de
groupes ni résoudre optimalement un graph coloring.

Le plan immuable contient la mission, la génération, la baseline, la Wave, les
groupes et un fingerprint SHA-256 du contexte canonique. Ce fingerprint détecte
la staleness mais ne constitue pas une autorité. Le plan reste reconstructible
et n'est pas persisté.

## Préparation et worktrees

`prepare_group(plan, group_index, coordination_input=...)` recalcule d'abord le
plan canonique, exige la baseline toujours observée au HEAD du primary, puis
délègue chaque ressource à `WorktreeManager.plan_assignment()` et
`WorktreeManager.activate()`. Un groupe suivant attend que tous les groupes
précédents aient des assignments `COMPLETED` avec `result_commit`.

La préparation est séquentielle, sans transaction distribuée : chaque
assignment est vérifiée `ACTIVE` avant que le groupe soit déclaré préparé. Si
une activation échoue, l'opération globale échoue et aucune préparation
partielle ne devient un succès. Les worktrees et records déjà créés sont
conservés et observables ; aucun cleanup forcé n'est effectué.

`PreparedParallelGroup` expose les IDs de stories et assignments, paths,
branches, baseline, génération et un contexte par Implementer. Chaque contexte
porte un `RoleHandoff` vers `IMPLEMENTER`. L'isolation n'élargit jamais le
`UserStory.scope` et aucun modèle, thread, processus Codex ou VS Code n'est
lancé.

`validate_prepared_group(...)` reconstruit le plan et le groupe canoniques,
relit le registre et réconcilie chaque assignment `ACTIVE` avec son worktree.
Cette vérification est read-only : elle ne crée, n'active et ne complète aucune
ressource. P4.9 l'utilise avant tout lancement concurrent.

## Soumission et completion

La planification porte des stories `PLANNED/READY`, alors que le contrat
Implementer exige `IN_PROGRESS`. P3.8 ne fabrique pas cette transition : après
la transition contrôlée externe, l'opérateur construit `ImplementerInput`
depuis le handoff préparé.

`submit_result(...)` lie ensuite le résultat à l'assignment, la story, la
mission, la génération, la baseline, la branche et le worktree préparés. Il
réutilise `ImplementerResultValidator`; seul un résultat `READY_FOR_TEST`
valide peut continuer. Le coordinateur résout aussi l'`execution_id` dans le
ledger P4 du worktree et exige un record terminal `VALIDATED`, son résultat
canonique, son fingerprint et ses observations Git liés exactement au contexte.
Il émet alors une capability privée vers
`WorktreeManager.commit_validated_implementation()`, puis appelle
`WorktreeManager.complete()`. Le SHA n'est jamais fourni librement comme
autorité au coordinateur.

`complete_group(...)` exige exactement un résultat validé par assignment et
des records persistants `COMPLETED` dont les commits correspondent. Un résultat
absent, stale, cross-story, issu d'une autre assignment, dirty ou sans commit
bloque. `fail_member(...)` délègue `ACTIVE -> FAILED` au manager et conserve les
diagnostics et ressources.

## Restart et limites

Après restart, un nouveau coordinateur recharge le registre via
`WorktreeManager`, reconstruit le plan et reprend les assignments `ACTIVE`
exactement réconciliées. Les `COMPLETED` restent observables et autorisent le
passage conservateur au groupe suivant. Une préparation partielle reste donc
visible sans faux succès et sans dépendance à l'objet Python précédent.
Un commit créé avant perte du retour ou avant persistance `COMPLETED` est
reconnu seulement par son binding complet ; une reprise identique ne crée pas
de second commit.

P3.8 s'arrête aux branches Implementer complétées. Il ne merge, cherry-pick,
rebase ou supprime aucune branche ; il ne lance ni Tester combiné, Integration
Gate, certification, scheduler générique ou primitive de concurrence. Le flux
conceptuel est : plan parallèle, préparation d'un groupe, intervention Codex
dans chaque workspace, soumission des résultats, completion des assignments,
puis future P3.9.
