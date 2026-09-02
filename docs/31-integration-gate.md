# Integration Gate

## Objectif et inputs

`IntegrationGate.evaluate(IntegrationGateContext)` décide si les résultats
d'un groupe parallèle `COMPLETED` sont admissibles pour l'évaluation du futur
Merge Coordinator. Le contexte référence l'input canonique P3.8, le
`ParallelExecutionPlan`, le `ParallelGroupResult` et le `MissionState` courant.
Le gate reconstruit le plan et la `ConflictAnalysis`; un agrégat fourni par
l'appelant ne devient jamais autoritatif par lui-même.

Le résultat Phase 3 est distinct du modèle `Gate` du Control Plane. Un `PASS`
ne certifie aucune User Story, n'approuve aucun merge et ne termine aucune
mission.

## Groupe, membres et réalité Git

Chaque membre doit correspondre exactement à l'ordre du
`ParallelExecutionGroup` et posséder un `WorktreeAssignment` persistant
`COMPLETED`, de même mission, génération et baseline. Son `result_commit` doit
correspondre au registre, au `ParallelMemberResult`, au HEAD propre du worktree
et au tip de la branche dédiée. L'`ImplementerInput` et l'`ImplementerResult`
sont revalidés ; seul `READY_FOR_TEST` est admissible.

`WorktreeManager.inspect_all()` et `inspect()` comparent registre, worktrees,
branches, commits et propreté. Une réalité indisponible ou contradictoire reste
`UNKNOWN`; aucune réparation ni adoption implicite n'est tentée.

## Fichiers modifiés, scope et collisions

Git détermine les ajouts, copies, suppressions, modifications et renames entre
`baseline_commit..result_commit` avec `diff --name-status -z --find-renames`.
La liste normalisée doit correspondre exactement à
`ImplementerResult.files_changed`. Les fichiers réels sont ensuite contrôlés
contre `UserStory.scope`, avec priorité de `forbidden_paths`.

Deux membres modifiant le même chemin produisent toujours `FAIL`, même si Git
pourrait fusionner des zones différentes du fichier. Des fichiers distincts
dans un même dossier ne sont pas automatiquement conflictuels. Pour un rename,
la politique V1 est conservatrice : ancien et nouveau chemins sont tous deux
contrôlés, déclarés et comparés aux autres branches. Aucun moteur de rename
supplémentaire n'est implémenté.

La `ConflictAnalysis` est recalculée depuis le ProjectState pertinent. Toute
paire `CONFLICT`, `UNKNOWN` ou absente interdit `PASS`, même si un ancien plan
la déclarait compatible.

## Preflight Git et ordre

L'ordre d'intégration est exactement l'ordre des membres du groupe P3.8. Pour
plusieurs résultats, P3.9 exécute `git merge-tree --write-tree` pour chaque
paire dans cet ordre canonique, avec la baseline explicite comme merge-base.
Les objets temporaires sont redirigés vers un répertoire éphémère hors du
repository puis supprimés. Aucune ref, branche, index ou worktree n'est muté.

Cette stratégie détecte les conflits Git pairwise évidents et complète la
politique stricte de collision de chemins. Elle ne prétend pas démontrer la
commutativité de tous les ordres possibles. Le futur Merge Coordinator devra
réutiliser cet ordre ou revalider l'état.

## PASS, FAIL et UNKNOWN

- `PASS` exige groupe complet, assignments et résultats exacts, génération et
  baseline courantes, diffs déclarés exacts, scopes respectés, absence de
  collision, paires toujours `SAFE`, preflights Git réussis et aucune condition
  inconnue.
- `FAIL` représente une violation démontrée : membre incomplet, baseline ou
  génération divergente, assignment/branche driftée, diff déclaré inexact,
  scope dépassé, collision, conflit de plan ou conflit `merge-tree`.
- `UNKNOWN` représente une observation obligatoire indisponible ou une erreur
  technique qui ne démontre pas une incompatibilité métier. Il ne vaut jamais
  `PASS`.

Un primary HEAD différent de la baseline produit `FAIL` et exige une nouvelle
stratégie explicite. Une génération stale ne passe pas. Les findings sont triés
déterministement et le même état reconstruit après restart produit le même
résultat.

## Frontières

L'évaluation est read-only : aucun merge, cherry-pick, rebase, checkout,
cleanup, transition, Evidence, certification ou invocation Tester/Reviewer
n'est effectué. Les worktrees et branches restent disponibles pour diagnostic
et pour P3.10. P3.9 n'exécute pas de suite globale combinée et ne construit pas
d'environnement d'intégration physique.

La composition de mission persiste uniquement le fingerprint d'un résultat
Gate PASS. Avant merge, le Gate est réévalué normalement. Après une perte de
retour post-merge, une reconstruction historique privée reste propriété du
Gate : elle revalide tous les inputs et son fingerprint, tandis que le
MergeCoordinator doit encore prouver que le primary est exactement le merge
attendu. Cette voie ne permet pas de convertir FAIL ou UNKNOWN en PASS.
