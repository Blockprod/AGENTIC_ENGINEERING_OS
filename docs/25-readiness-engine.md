# Readiness Engine

## Objectif et API

`ReadinessEngine.evaluate(dag_snapshot, project_state) -> ReadinessSnapshot`
répond uniquement à la question : quelles User Stories sont logiquement
éligibles maintenant ? Le résultat est un diagnostic immuable et n'accorde
aucune autorité de transition.

Avant toute évaluation, le moteur valide la structure du snapshot et le compare
à la projection canonique reconstruite par `DAGValidator` depuis le
`ProjectState`. Toute différence de nœud, edge, statut, dépendance, priorité ou
risque bloque fail-closed.

## Classifications et politique d'état

- `READY` : état propre `PLANNED` ou `READY`, toutes les dépendances
  `CERTIFIED`, et approbation requise déjà appliquée.
- `WAITING_DEPENDENCIES` : candidat dont au moins une dépendance n'est pas
  `CERTIFIED`.
- `BLOCKED` : statut `PROPOSED`, `BLOCKED`, `REJECTED` ou
  `REMEDIATION_REQUIRED`, ou approbation humaine obligatoire non appliquée.
- `INELIGIBLE` : statut déjà engagé dans le pipeline, de `IN_PROGRESS` à
  `CERTIFICATION`; le nœud n'est pas candidat à une nouvelle entrée
  d'exécution.
- `TERMINAL` : statut `CERTIFIED` ou `CANCELLED`.

Cette politique ne reproduit pas les transitions de la machine d'état.
`ReadinessClassification.READY` ne modifie jamais `UserStory.status` et ne
constitue pas une autorisation de le faire. Un root reste soumis à sa politique
d'état et à l'approbation applicable.

## Dépendances et approbation humaine

Une dépendance est satisfaite uniquement si son statut autoritatif est
`CERTIFIED`. Tous les autres statuts, y compris `CANCELLED`, `REJECTED` et
`BLOCKED`, restent non satisfaisants.

Lorsque `human_approval.required` vaut `true`, l'approbation doit déjà être
appliquée dans la User Story autoritative : `approved=true`, identité Human,
horodatage et référence d'Evidence présents. Le moteur ne lit aucune Evidence,
n'appelle pas `HumanApprovalService` et n'invente aucune approbation.

## Déterminisme et frontières

Les diagnostics et leurs listes de dépendances suivent l'ordre lexical des IDs.
Le DAG, le ProjectState, les User Stories, leurs statuts, dépendances et
approbations ne sont jamais mutés, y compris après une erreur.

La priorité et le risque ne changent pas la classification. P3.3 ne produit ni
Wave, batch, groupe parallèle, conflit d'exécution, worktree, scheduling ou
transition. Le Wave Planner décrit dans `docs/26-wave-planner.md` consomme ces
diagnostics sans que le ReadinessEngine décide de leur regroupement ou de leur
ordre d'exécution.
